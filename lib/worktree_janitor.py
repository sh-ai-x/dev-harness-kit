"""worktree_janitor — non-interactive worktree cleanup.

Companion to `bin/worktree-prune.sh` (which is interactive). The janitor
identifies stale worktrees whose branch has no open PR and is not under
babysit-pr retention, then emits a JSON candidate table for the shell
wrapper at `bin/worktree-janitor.sh` to render and dispatch.

Two git calls total (matches `lib/worktree_prune.py` perf budget):
  1. `git worktree list --porcelain`             — path + branch per row
  2. `git for-each-ref refs/heads/`              — branch-tip epoch per branch

Per-row open-PR detection is delegated to `gh pr view <branch>` (matches
`lib/ci_doctor.py:_fetch_open_pr_state` pattern; non-zero exit means no
open PR — silent skip). Babysit retention honors the marker written by
`lib/babysit_pr_retention.write_retention` (PR #689 Phase 2 contract).

CLI:
  python3 -m lib.worktree_janitor --repo PATH [--age-days N]
        [--except-self PATH] [--json]

Output: JSON to stdout, shape:
  {"rows": [{"path", "branch", "epoch", "age_days"}, ...], "total": N}
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_AGE_DAYS = 14
BABYSIT_MARKER = ".dev-kit/babysit-retention.json"


class Row:
    """Plain immutable row — `@dataclass` triggers a Python 3.14 bug under
    dynamic module loading (cls.__module__ returns None, dataclasses chokes)."""

    __slots__ = ("path", "branch", "epoch", "age_days")

    def __init__(self, path: str, branch: str, epoch: int, age_days: float):
        self.path = path
        self.branch = branch
        self.epoch = epoch
        self.age_days = age_days

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "branch": self.branch,
            "epoch": self.epoch,
            "age_days": self.age_days,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"Row({self.branch!r}, age={self.age_days:.1f}d)"


def _branch_epoch_map(repo: Path) -> dict[str, int]:
    """Return {branch: epoch_seconds} for every ref under refs/heads/.

    One git call. Detached refs (no `refs/heads/` prefix) are excluded.
    """
    cp = subprocess.run(
        ["git", "for-each-ref", "--format=%(committerdate:unix) %(refname:short)",
         "refs/heads/"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    out: dict[str, int] = {}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            out[parts[1]] = int(parts[0])
        except ValueError:
            continue
    return out


def _gh_open_pr_for(branch: str) -> bool:
    """Return True iff `gh pr view <branch>` resolves to an open PR.

    Returns False when gh is missing, not authed, or no PR exists for the
    branch (all three look like "no open PR" to the janitor).
    """
    if not shutil.which("gh"):
        return False
    cp = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "number",
         "--jq", ".number // empty"],
        capture_output=True, text=True, check=False,
    )
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _babysit_retained(repo: Path, branch: str) -> bool:
    """Honor `.dev-kit/babysit-retention.json` (PR #689 Phase 2).

    Import is lazy so a fresh checkout without `lib/babysit_pr_retention.py`
    on the path still works (the janitor just won't retain anything in
    that case).
    """
    marker = repo / BABYSIT_MARKER
    if not marker.exists():
        return False
    try:
        sys.path.insert(0, str(repo))
        from babysit_pr_retention import is_retained  # type: ignore
        return bool(is_retained(branch))
    except Exception:
        return False
    finally:
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass


def _self_path(except_self: str | None) -> str | None:
    """Resolve `--except-self` to the absolute worktree path to skip."""
    if not except_self:
        return None
    try:
        return str(Path(except_self).resolve())
    except OSError:
        return None


def collect(repo: Path, *, age_days: float, except_self: str | None) -> list[Row]:
    """Walk worktrees and return the removal candidates."""
    cp = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    if cp.returncode != 0:
        return []

    epochs = _branch_epoch_map(repo)
    skip_path = _self_path(except_self)
    now = int(subprocess.run(
        ["date", "+%s"], capture_output=True, text=True, check=True,
    ).stdout.strip())

    # First pass: parse `worktree list --porcelain` into (path, branch) pairs.
    # Porcelain format alternates `worktree <path>` with optional `HEAD <sha>`
    # and `branch refs/heads/<name>` (or `detached`).
    current: dict[str, str] = {}
    blocks: list[dict[str, str]] = []
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                blocks.append(current)
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                current["branch"] = ref[len("refs/heads/"):]
        elif line.strip() == "detached":
            current["detached"] = "1"
    if current:
        blocks.append(current)

    rows: list[Row] = []
    for blk in blocks:
        path = blk.get("path", "")
        branch = blk.get("branch", "")
        if not path or not branch or blk.get("detached"):
            continue
        # Skip the main checkout (always `refs/heads/main`-tied).
        if branch == "main":
            continue
        if skip_path and Path(path).resolve() == Path(skip_path).resolve():
            continue
        epoch = epochs.get(branch)
        if epoch is None:
            continue
        age = max(0.0, (now - epoch) / 86400.0)
        if age < age_days:
            continue
        if _gh_open_pr_for(branch):
            continue
        if _babysit_retained(repo, branch):
            continue
        rows.append(Row(path=path, branch=branch, epoch=epoch, age_days=age))

    rows.sort(key=lambda r: r.epoch)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lib.worktree_janitor")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--age-days", type=float,
                        default=float(os.environ.get("JANITOR_AGE_DAYS",
                                                     DEFAULT_AGE_DAYS)))
    parser.add_argument("--except-self", default=None)
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)

    rows = collect(ns.repo,
                   age_days=ns.age_days,
                   except_self=ns.except_self)

    if ns.json:
        payload = {"rows": [r.to_dict() for r in rows], "total": len(rows)}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        print(f"# worktree candidates (age >= {ns.age_days:.0f}d, no open PR, "
              f"not babysit-retained): {len(rows)}", file=sys.stderr)
        for r in rows:
            print(f"{r.age_days:6.1f}d  {r.branch:<48}  {r.path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
