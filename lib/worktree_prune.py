"""Worktree prune: enumerate, age-sort, and report candidates.

Companion to ``bin/worktree-prune.sh``. The shell script owns CLI parsing,
interactive prompts, and the actual ``git worktree remove`` dispatch; this
module owns the deterministic work — parsing ``git worktree list --porcelain``,
mapping each worktree's branch to a committer epoch, and emitting a
plain-text table sorted oldest-first.

Kept in ``lib/`` (not ``tools/``) because the only caller is the bin
script + the pytest suite, and ``lib/`` is the canonical home for
importable Python helpers that don't run as standalone CLIs.

Single-pass design: the module reads ``git worktree list --porcelain``
directly and runs ``git for-each-ref`` for the branch→epoch map. Both
are invoked from the test fixtures too (with isolated repos), so the
public surface is ``collect(repo_root) -> list[Row]`` where ``Row`` is a
plain dataclass.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Row:
    """One worktree, ready for display.

    ``path`` and ``branch`` are absolute and un-prefixed. ``epoch`` is the
    Unix timestamp of the branch tip's committer date (0 if the branch has
    been deleted but the worktree is still registered — vanishingly rare
    in practice; the rare case is preserved for diagnostic visibility).
    """

    path: str
    branch: str
    epoch: int
    sha: str

    def age_days(self, now_epoch: int) -> int:
        if not self.epoch or self.epoch > now_epoch:
            return 0
        return (now_epoch - self.epoch) // 86400


def _run(cmd: list[str], cwd: str) -> str:
    """Run a git command, return stdout. Raises on non-zero exit."""
    out = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    )
    return out.stdout


def _porcelain_blocks(stdout: str) -> Iterator[dict[str, str]]:
    """Yield one dict per ``git worktree list --porcelain`` block.

    Each block is a list of ``Key value`` lines followed by a blank
    line. Detached-HEAD worktrees emit ``detached`` instead of
    ``branch refs/heads/<name>``; the dict then has ``detached=True``
    and no ``branch`` key.
    """
    block: dict[str, str] = {}
    for raw in stdout.splitlines():
        if raw == "":
            if block:
                yield block
                block = {}
            continue
        key, _, value = raw.partition(" ")
        if key == "detached":
            block["detached"] = "true"
        else:
            # Strip ``refs/heads/`` so the key matches the short name
            # produced by ``git for-each-ref --format=%(refname:short)``;
            # the branch→epoch map is keyed on the short form, so the
            # lookup fails if we keep the full ref here.
            if key == "branch" and value.startswith("refs/heads/"):
                value = value[len("refs/heads/"):]
            block[key] = value
    if block:
        yield block


def _branch_epoch_map(repo_root: str) -> dict[str, int]:
    """Single ``git for-each-ref`` call → ``{branch: epoch}``.

    Falls back to an empty dict if the call fails (e.g. corrupt refs);
    callers treat ``epoch=0`` as "unknown" and still emit the row.
    """
    try:
        out = _run(
            [
                "git", "-C", repo_root, "for-each-ref",
                "--format=%(committerdate:unix) %(refname:short)",
                "refs/heads/",
            ],
            cwd=repo_root,
        )
    except subprocess.CalledProcessError:
        return {}
    result: dict[str, int] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        epoch_s, _, branch = line.partition(" ")
        if not branch:
            continue
        try:
            result[branch] = int(epoch_s)
        except ValueError:
            continue
    return result


def _resolve_main(repo_root: str) -> str:
    """Canonical absolute path of the repo toplevel — used to exclude the
    main checkout from the candidate list."""
    return str(Path(_run(["git", "-C", repo_root, "rev-parse", "--show-toplevel"], cwd=repo_root).strip()).resolve())


def collect(repo_root: str) -> list[Row]:
    """Return removable worktree rows, oldest first.

    Excludes the main checkout (the row whose path == ``repo_root``) and
    detached-HEAD worktrees (no branch tip to compare; janitor agent
    classifies those separately). Worktrees in the legacy
    ``.claude/worktrees/`` and ``.codex/worktrees/`` roots ARE included
    — the script's job is "show me all candidates", the caller decides
    which root to act on.
    """
    porcelain = _run(["git", "-C", repo_root, "worktree", "list", "--porcelain"], cwd=repo_root)
    epoch_map = _branch_epoch_map(repo_root)
    main_abs = _resolve_main(repo_root)

    rows: list[Row] = []
    for block in _porcelain_blocks(porcelain):
        path = block.get("worktree", "")
        branch = block.get("branch", "")
        sha = block.get("HEAD", "")
        if not path or not branch or block.get("detached"):
            continue
        try:
            path_abs = str(Path(path).resolve())
        except OSError:
            path_abs = path
        if path_abs == main_abs:
            continue
        epoch = epoch_map.get(branch, 0)
        rows.append(Row(path=path, branch=branch, epoch=epoch, sha=sha))

    rows.sort(key=lambda r: r.epoch)
    return rows


def render_table(rows: list[Row], now_epoch: int) -> str:
    """Plain-text table, one row per worktree, oldest first.

    Format is intentionally minimal — no ANSI, no box-drawing — so the
    output is grep/cut-friendly for downstream tooling.
    """
    if not rows:
        return ""

    def _truncate(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 3] + "..."

    lines: list[str] = []
    lines.append(f"{'#':>4}  {'AGE(d)':>6}  {'BRANCH':<30}  PATH")
    lines.append(f"{'----':>4}  {'------':>6}  {'-' * 30}  {'-' * 4}")
    for i, row in enumerate(rows, start=1):
        age = row.age_days(now_epoch)
        branch = _truncate(row.branch, 30)
        lines.append(f"{i:>4}  {age:>6}  {branch:<30}  {row.path}")
    return "\n".join(lines)


def render_head_table(rows: list[Row], now_epoch: int, head: int) -> str:
    """Same format as ``render_table`` but only the first ``head`` rows.

    Used by the shell script's "Will remove" preview so the selected
    slice reuses the audit-table format instead of re-implementing it
    in a second Python heredoc.
    """
    return render_table(rows[:head], now_epoch)


def main() -> None:
    """CLI mode — used by bin/worktree-prune.sh.

    Three modes, mutually exclusive (default is JSON):
    * ``--table`` — human-readable fixed-width table prefixed by a
      ``Worktrees registered: N`` summary line that the shell script
      greps for the total count. No ANSI, grep/cut-friendly. Accepts
      ``--head N`` to render only the first N rows (the "Will remove"
      preview reuses the same format).
    * ``--count`` — just the integer N. Cheap, used for the shell
      short-circuit when the user passes 0.
    * (default) — JSON list of Row dicts for downstream tooling.
    """
    import argparse
    import time as _time

    p = argparse.ArgumentParser(description="List worktree prune candidates")
    p.add_argument("--repo", required=True, help="Path to the repo root")
    p.add_argument("--table", action="store_true",
                   help="Print the human-readable table with a summary header")
    p.add_argument("--count", action="store_true",
                   help="Print only the integer count of candidates")
    p.add_argument("--head", type=int, default=None,
                   help="In --table mode, render only the first N rows")
    args = p.parse_args()

    rows = collect(args.repo)
    if args.count:
        print(len(rows))
        return
    if args.table:
        now = int(_time.time())
        print(f"Worktrees registered: {len(rows)} (excluding main checkout)")
        print()
        # --head trims the table contents (preview) without changing the
        # summary line — the operator still sees the full candidate
        # count, just the rendered preview is sliced.
        if args.head is not None:
            rows = rows[: args.head]
        print(render_table(rows, now))
        return
    print(json.dumps([asdict(r) for r in rows]))


if __name__ == "__main__":
    main()
