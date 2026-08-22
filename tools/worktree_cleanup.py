#!/usr/bin/env python3
"""Cleanup-safe archival for `git worktree remove` (issue #689 Phase 2).

Decouples the worktree lifecycle from telemetry retention: when an operator
removes a worktree, this module first copies any in-worktree ``logs/`` tree
to a durable location (AGENT_LOG_ROOT when configured, otherwise the main
checkout's ``logs/.archive/<branch>/<ts>/``), then returns so the caller can
run ``git worktree remove`` itself.

Design contract (mirrors issue #689 §Phase 2 acceptance criteria):

  * **Idempotent** — running twice produces the same end state; we copy
    files but never move them, so a second invocation is a no-op.
  * **Fail-safe** — archival errors are reported in the returned dict;
    the function never raises. The caller decides whether to block
    ``git worktree remove`` (default: warn, don't block, so a misconfigured
    AGENT_LOG_ROOT cannot strand a worktree).
  * **Opt-in strict mode** — ``strict=True`` flips the policy to
    "block removal if archival fails", for CI / automation use.
  * **No secrets** — only files under ``<worktree>/logs/`` are touched;
    no transcript paths, env vars, or session metadata are read.

CLI:

  python3 tools/worktree_cleanup.py <worktree_path> [--main-root DIR]
                                       [--strict] [--dry-run] [--json]

The shell wrapper ``bin/worktree-remove-safe.sh`` invokes this with the
correct arguments before running ``git worktree remove``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# Mirror the AGENT_LOG_ROOT contract in tools/save_log.py so a worktree
# removal and the next session's Stop-hook write land in the same place.
def _resolve_archive_root(main_root: str) -> tuple[Path, bool]:
    """Return (archive_root, external) per the AGENT_LOG_ROOT env var.

    When AGENT_LOG_ROOT is set, archive_root is <AGENT_LOG_ROOT>/<repo>/.archive
    and external=True. Otherwise archive_root is <main_root>/logs/.archive and
    external=False. The .archive sibling keeps the analyzer (which only walks
    `logs/<tool>/<branch>/`) from re-scanning archived telemetry.
    """
    configured = os.environ.get("AGENT_LOG_ROOT", "").strip()
    repo_label = Path(main_root).name
    if configured:
        return Path(configured).expanduser() / repo_label / ".archive", True
    return Path(main_root) / "logs" / ".archive", False


def _safe_copy_tree(src: Path, dst: Path) -> int:
    """Copy ``src`` into ``dst`` recursively without following symlinks.

    Returns the number of files copied. Existing destination files are
    overwritten (the second invocation is the idempotent replay case).
    """
    count = 0
    for entry in src.rglob("*"):
        if entry.is_symlink():
            continue  # never chase symlinks during archival
        rel = entry.relative_to(src)
        target = dst / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        count += 1
    return count


def detect_branch(cwd: str) -> str:
    """Mirror save_log.detect_branch() so archive paths are stable across
    the Stop-hook write and the worktree-remove path.

    Falls back to ``"no-git"`` if git is unavailable or the directory is not
    a checkout; archival is best-effort, not a hard gate on removal.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0:
            ref = out.stdout.strip()
            if ref == "HEAD":
                sha = subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=2, check=False,
                ).stdout.strip()
                return f"detached-{sha}" if sha else "detached"
            return ref
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "no-git"


def find_main_repo_root(worktree_path: str) -> Optional[str]:
    """Return the absolute main-checkout path if ``worktree_path`` is a
    worktree of one; otherwise None.

    A worktree's ``.git`` is a file containing
    ``gitdir: <main>/.git/worktrees/<name>``. Walking up from the .git
    file's parent gives the main checkout. No subprocess, safe to call
    from any wrapper script.
    """
    git_marker = Path(worktree_path) / ".git"
    if not git_marker.is_file():
        return None
    try:
        text = git_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (Path(worktree_path) / gitdir).resolve()
    # <gitdir>/.. = <main>/.git/worktrees ; <main>/.git/worktrees/../..
    parts = gitdir.resolve().parts
    try:
        idx = parts.index("worktrees")
        return str(Path(*parts[:idx]).parent)
    except ValueError:
        return None


def archive_worktree_logs(
    worktree_path: str,
    *,
    main_root: Optional[str] = None,
    dry_run: bool = False,
    strict: bool = False,
) -> dict:
    """Archive ``<worktree>/logs/`` to a durable location before removal.

    Returns a dict:

      {
        "status": "ok" | "skipped" | "error",
        "branch": str,
        "worktree_logs": str | None,        # absolute path or None if absent
        "archive_target": str | None,       # absolute path of destination root
        "files_copied": int,
        "external_root": bool,              # True when AGENT_LOG_ROOT is set
        "error": str | None,                # human-readable on error
      }

    Idempotent: a second invocation with the same worktree_path produces
    the same archive_target but ``files_copied == 0`` because the source
    tree was already removed by ``git worktree remove``. Pre-removal
    callers can run this repeatedly without harm.

    Fail-safe: returns the dict even on OS errors; never raises.
    """
    result: dict = {
        "status": "ok",
        "branch": "no-git",
        "worktree_logs": None,
        "archive_target": None,
        "files_copied": 0,
        "external_root": False,
        "error": None,
    }
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        result["status"] = "skipped"
        result["error"] = f"worktree path is not a directory: {wt}"
        return result
    src = wt / "logs"
    if not src.is_dir():
        # No logs to archive is the steady-state case for a fresh worktree
        # that never ran a session. Skipping is the correct, low-noise
        # outcome — don't make the operator wonder why a removal worked.
        result["status"] = "skipped"
        return result
    result["worktree_logs"] = str(src)

    main = main_root or find_main_repo_root(str(wt))
    if not main:
        # Standalone directory, not a worktree of any repo. Archive locally
        # so the caller can still recover the logs by reading the result.
        result["status"] = "error" if strict else "skipped"
        result["error"] = "worktree is not a registered git worktree (no .git file)"
        return result

    result["branch"] = detect_branch(str(wt))
    archive_root, external = _resolve_archive_root(main)
    result["external_root"] = external
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = archive_root / result["branch"] / ts
    result["archive_target"] = str(target)

    if dry_run:
        return result

    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = _safe_copy_tree(src, target)
        result["files_copied"] = copied
    except OSError as exc:
        result["status"] = "error"
        result["error"] = f"archive copy failed: {exc}"

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "worktree_path",
        help="absolute path to the worktree directory about to be removed",
    )
    parser.add_argument(
        "--main-root",
        default=None,
        help="main checkout path (auto-detected from <worktree>/.git if omitted)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero on archival failure (default: warn, exit 0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute the archive target without copying any files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON to stdout (default: human-readable summary)",
    )
    args = parser.parse_args(argv)

    result = archive_worktree_logs(
        args.worktree_path,
        main_root=args.main_root,
        dry_run=args.dry_run,
        strict=args.strict,
    )

    if args.json:
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    else:
        branch = result["branch"]
        wt_logs = result["worktree_logs"]
        target = result["archive_target"]
        copied = result["files_copied"]
        status = result["status"]
        if wt_logs is None:
            print(f"[{status}] no logs/ to archive under {args.worktree_path}")
        elif status == "ok":
            print(
                f"[ok] archived {copied} file(s) from {wt_logs} → {target} "
                f"(branch={branch}, external_root={result['external_root']})"
            )
        else:
            print(f"[{status}] {result['error']}")

    if result["status"] == "error" and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
