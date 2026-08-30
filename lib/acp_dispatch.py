"""acp_dispatch.py — M-tier dispatch helper (closes #282).

The orchestrator (M) decomposes a round into N parallel PRs and dispatches
one T (task sub-agent) per PR. The dispatch is the contract between M and
T: it carries seven mandatory placeholders (task, branch, worktree path,
cwd, plugin-version target, lock file, parent-session cwd) and a hand-off
note summarizing the T's read-only scope.

This module owns:

  * `ACPDispatcher` — fills the seven placeholders in the canonical
    template at `skills/_acp/sub-agent-prompt.md`, runs a parallel
    `git worktree add` loop (mirroring `hooks/worktree-auto-cut.sh:247-269`),
    and writes each dispatch envelope to disk.
  * `DispatchResult` — value object returned per PR; the M reads these
    to write `handoffs.md` and to track per-T progress.
  * `parse_pr_spec` — CLI shim that turns `"PR-3:l6-alpha"` strings into
    `(branch_slug, task_summary)` tuples for the dispatcher's input list.

Reuses (do not duplicate):
  * `skills/_acp/sub-agent-prompt.md` (canonical template)
  * `hooks/worktree-auto-cut.sh:247-269` (cut + boot + envelope pattern)
  * `hooks/lib/worktree-detect.sh` (`worktree_detect` helper)
  * `hooks/acp-tier-assert.sh`, `hooks/acp-cwd-discipline.sh`
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from git_worktree import cut_worktree  # noqa: E402 — canonical helper (issue #310)

# ---------------------------------------------------------------------------
# Constants — single source of truth, mirror docs/architecture/acp-harness.md §3.2.
# ---------------------------------------------------------------------------

# Seven mandatory placeholders in the canonical template. The test in
# tests/test_acp_dispatch.py refuses any dispatch with a missing value
# for one of these keys.
SEVEN_PLACEHOLDERS: tuple[str, ...] = (
    "<TASK>",
    "<BRANCH>",
    "<WORKTREE_PATH>",
    "<CWD>",
    "<PLUGIN_VERSION_TARGET>",
    "<LOCK_FILE>",
    "<PARENT_SESSION_CWD>",
)

# Default template path (relative to repo root).
DEFAULT_TEMPLATE_PATH = Path("skills/_acp/sub-agent-prompt.md")



# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DispatchSpec:
    """One PR's dispatch inputs.

    Attributes
    ----------
    pr_index : int
        1-based merge-order position of the PR within the round. Used
        by `bin/version-slot compute` to allocate a non-colliding plugin
        version (`docs/architecture/acp-harness.md` §4).
    branch : str
        Target feature branch in `<type>/<slug>` form
        (`rules/git-workflow.md`). Example: `feat/acp-dispatch`.
    task : str
        The literal `<TASK>` body that goes into the dispatch envelope.
        Markdown is allowed and expected (checkboxes, code fences, etc.).
    """

    pr_index: int
    branch: str
    task: str


@dataclasses.dataclass(frozen=True)
class DispatchResult:
    """One PR's dispatch outcome, returned by `ACPDispatcher.dispatch`.

    Attributes
    ----------
    spec : DispatchSpec
        The input spec echoed back so the M can correlate results.
    worktree_path : Path
        Absolute path to the worktree the T will use. Equal to
        `<WORKTREE_PATH>` in the envelope.
    envelope_path : Path
        Absolute path to the dispatch envelope file written under
        `<round_dir>/dispatches/<branch>.md`.
    envelope : str
        The rendered envelope body (the literal text the T receives).
        Useful for logging; the T itself reads from `envelope_path`.
    dry_run : bool
        True when the dispatcher was invoked with `--dry-run` and no
        filesystem mutation happened.
    """

    spec: DispatchSpec
    worktree_path: Path
    envelope_path: Path
    envelope: str
    dry_run: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_pr_spec(spec: str) -> tuple[int, str, str]:
    """Parse a `"PR-<index>:<slug>"` CLI string into `(index, branch, slug)`.

    The dispatcher's `dispatch()` method takes
    `list[tuple[int, str, str]]` so the documented `PR-<index>` is
    preserved verbatim. Renumbering by input order would corrupt the
    merge-order / version-slot hand-off metadata whenever specs are
    decomposed or passed out of order (the documented example is
    `"PR-3:l6-alpha,PR-2:launcher"`, where the first result MUST come
    back as PR-3, not PR-1).

    Returns
    -------
    (pr_index, branch, slug) : tuple[int, str, str]
        `pr_index` is the 1-based `PR-<index>` integer; the M's
        `bin/version-slot compute <PR_INDEX>` reads this directly.
        `branch` is `feat/<slug>` (default type — the M can rename
        before commit per `rules/git-workflow.md`).
        `slug` is the kebab-case identifier used to name the
        dispatch envelope file and to derive the worktree dir.
    """
    if ":" not in spec:
        raise ValueError(f"PR spec must be 'PR-<index>:<slug>' (got {spec!r})")
    head, slug = spec.split(":", 1)
    if not head.startswith("PR-"):
        raise ValueError(f"PR spec must start with 'PR-' (got {head!r})")
    index_str = head[3:]
    if not index_str.isdigit():
        raise ValueError(
            f"PR spec must have a numeric index after 'PR-' (got {head!r})"
        )
    pr_index = int(index_str)
    if pr_index < 1:
        raise ValueError(
            f"PR index must be 1-based (got {pr_index!r})"
        )
    if not re.match(r"^[a-z0-9-]{2,40}$", slug):
        raise ValueError(
            f"PR slug must be kebab-case 2-40 chars per rules/git-workflow.md "
            f"(got {slug!r})"
        )
    return pr_index, f"feat/{slug}", slug




def _read_template(template_path: Path) -> str:
    """Read the canonical ACP template. Raises FileNotFoundError with the
    resolved path so the M can correct a misconfigured repo quickly.
    """
    if not template_path.is_file():
        raise FileNotFoundError(
            f"ACP template not found at {template_path}. "
            f"Expected at skills/_acp/sub-agent-prompt.md relative to repo root."
        )
    return template_path.read_text(encoding="utf-8")


def _fill_placeholders(template: str, values: dict[str, str]) -> str:
    """Replace every `<KEY>` placeholder in `template` with its `values` entry.

    All seven placeholders MUST be supplied; missing keys raise
    ValueError so a misconfigured dispatch fails fast at M rather
    than silently shipping a malformed envelope to T.

    The replacement is a literal substring swap (not regex) so any
    special characters in `values` are passed through verbatim. The
    dispatcher never re-parses the rendered envelope.
    """
    missing = [p for p in SEVEN_PLACEHOLDERS if values.get(p.strip("<>")) in (None, "")]
    if missing:
        raise ValueError(
            f"dispatch is missing mandatory placeholder(s): {', '.join(missing)}. "
            f"See docs/architecture/acp-harness.md §3.2 — tests/test_acp_hand_off.py refuses any "
            f"dispatch prompt missing one."
        )
    rendered = template
    for placeholder in SEVEN_PLACEHOLDERS:
        key = placeholder.strip("<>")
        rendered = rendered.replace(placeholder, values[key])
    return rendered


# ---------------------------------------------------------------------------
# ACPDispatcher
# ---------------------------------------------------------------------------


class ACPDispatcher:
    """M-tier dispatcher. Fills placeholders + runs the parallel-cut loop.

    Parameters
    ----------
    repo_root : Path
        Absolute path of the orch-worktree (the M's checkout). All
        worktrees are cut below `<repo_root>/.worktrees/<slug>`.
    round_slug : str
        Short kebab-case descriptor for the round (e.g. `thin-harness`).
        The round dir lives at `<repo_root>/.dev-kit/round-<round_slug>/`.
    parent_session_cwd : Path
        Absolute path of the parent session's cwd. Embedded into every
        dispatch as `<PARENT_SESSION_CWD>` so the T can detect the
        parent-cwd misfire (`docs/architecture/acp-harness.md` §1 #1).
    plugin_version_target : str | None
        Pre-computed slot version (e.g. `0.3.84`). When None, the M
        should compute via `bin/version-slot compute <PR_INDEX>` before
        dispatching.
    template_path : Path | None
        Override the canonical template path. Defaults to
        `skills/_acp/sub-agent-prompt.md` relative to `repo_root`.
    dry_run : bool
        When True, no `git worktree add` runs and no envelope file is
        written — only the rendered envelopes are returned so the M can
        preview. Used by `--dry-run` CLI flag.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        round_slug: str,
        parent_session_cwd: Path,
        plugin_version_target: str | None = None,
        template_path: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.round_slug = round_slug
        self.parent_session_cwd = Path(parent_session_cwd).resolve()
        self.plugin_version_target = plugin_version_target
        self.template_path = (
            Path(template_path).resolve()
            if template_path
            else (self.repo_root / DEFAULT_TEMPLATE_PATH).resolve()
        )
        self.dry_run = dry_run
        self._template: str | None = None

    # -- public API --------------------------------------------------------

    def dispatch(
        self,
        round: str,  # noqa: A002 — match the dispatch contract signature
        prs: list[tuple[int, str, str]],
    ) -> list[DispatchResult]:
        """Read the canonical template, fill the 7 placeholders, and cut worktrees.

        Parameters
        ----------
        round : str
            The round descriptor. Mirrors `round_slug`; kept in the
            signature so the CLI and the library share one entry point.
        prs : list[tuple[int, str, str]]
            `[(pr_index, branch, task_slug), ...]` — one tuple per PR
            to dispatch. `pr_index` is the 1-based `PR-<index>` integer
            preserved verbatim from `parse_pr_spec`; the dispatcher
            never renumbers it, so the M's merge-order / version-slot
            metadata survives reordering or decomposition.
            `branch` is the full `<type>/<slug>` form, `task_slug` is
            the short slug used to name the dispatch envelope file and
            to derive the worktree dir.

        Returns
        -------
        list[DispatchResult]
            One entry per PR, in input order. `dry_run=True` produces
            results with `worktree_path` set to the intended path but
            no filesystem state changed.
        """
        if round != self.round_slug:
            raise ValueError(
                f"dispatch() round={round!r} does not match dispatcher "
                f"round_slug={self.round_slug!r}"
            )
        template = self._get_template()
        results: list[DispatchResult] = []
        round_dir = self.repo_root / ".dev-kit" / f"round-{self.round_slug}"
        version_target = self.plugin_version_target or self._resolve_default_version()
        if not version_target:
            raise RuntimeError(
                "plugin_version_target is required when no fallback can be "
                "derived from .claude-plugin/plugin.json. Pre-compute the "
                "slot via `bin/version-slot compute <PR_INDEX>` and pass "
                "it via --plugin-version-target, or land a plugin.json "
                "manifest at the repo root before dispatching."
            )
        for pr_index, branch, task_slug in prs:
            spec = DispatchSpec(pr_index=pr_index, branch=branch, task=task_slug)
            worktree_path = self.repo_root / ".worktrees" / task_slug
            envelope_path = round_dir / "dispatches" / f"{branch.replace('/', '-')}.md"
            values = {
                "TASK": self._build_task_body(spec),
                "BRANCH": branch,
                "WORKTREE_PATH": str(worktree_path),
                "CWD": str(worktree_path),
                "PLUGIN_VERSION_TARGET": version_target,
                "LOCK_FILE": str(round_dir / "locks" / f"{branch.split('/')[-1]}.lock"),
                "PARENT_SESSION_CWD": str(self.parent_session_cwd),
            }
            envelope = _fill_placeholders(template, values)
            if not self.dry_run:
                self._cut_worktree(branch, worktree_path)
                envelope_path.parent.mkdir(parents=True, exist_ok=True)
                envelope_path.write_text(envelope, encoding="utf-8")
            results.append(
                DispatchResult(
                    spec=spec,
                    worktree_path=worktree_path,
                    envelope_path=envelope_path,
                    envelope=envelope,
                    dry_run=self.dry_run,
                )
            )
        return results

    # -- internals ---------------------------------------------------------

    def _get_template(self) -> str:
        if self._template is None:
            self._template = _read_template(self.template_path)
        return self._template

    def _build_task_body(self, spec: DispatchSpec) -> str:
        """Compose the `<TASK>` body that lands inside the dispatch.

        The body is a markdown checklist mirroring the format used in
        `.dev-kit/round-thin-harness/dispatches/T*.md` so the T has the
        same shape across hand-written and M-generated envelopes.
        """
        return (
            f"(M-generated dispatch — round `{self.round_slug}`, "
            f"PR index {spec.pr_index})\n\n"
            f"{spec.task}\n\n"
            f"See the canonical ACP contract:\n"
            f"- docs/architecture/acp-harness.md §2 (tier-cognition)\n"
            f"- docs/architecture/acp-harness.md §3 (this template)\n"
            f"- rules/git-workflow.md (worktree + branch protocol)\n"
        )

    def _resolve_default_version(self) -> str | None:
        """Read the plugin version from the orch-worktree's plugin.json.

        Used as the fallback for `<PLUGIN_VERSION_TARGET>` when the
        caller did not supply one explicitly. Reading the local
        manifest keeps the fallback in lockstep with the branch the
        M is actually dispatching from — no more hard-coded "0.3.75"
        drifting behind origin/main or behind the local bump.

        Returns None when the manifest is missing or unparseable so
        `dispatch()` can fail closed rather than embed a stale default.
        """
        candidate = self.repo_root / ".claude-plugin" / "plugin.json"
        if not candidate.is_file():
            return None
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not version:
            return None
        return version

    def _branch_exists(self, branch: str) -> bool:
        """Return True when `branch` already exists as a local ref.

        Kept as a thin wrapper around the canonical helper so the
        regression test in ``tests/test_acp_dispatch.py`` (which
        pre-creates the branch on origin/main) still verifies the
        safe-mode contract end-to-end. The actual ``git worktree
        add`` invocation now routes through ``lib.git_worktree.
        cut_worktree`` (issue #310) — same semantics, one place to
        evolve them.
        """
        from git_worktree import branch_exists as _gw_branch_exists  # local: avoid module-load churn
        return _gw_branch_exists(self.repo_root, branch)

    def _cut_worktree(self, branch: str, worktree_path: Path) -> None:
        """Run `git worktree add -b <branch> <path> origin/main`.

        Thin shim over ``lib.git_worktree.cut_worktree`` so the safe
        contract (fail closed when dir exists, fail closed when branch
        exists, preserve pre-existing branches on failure) is shared
        with future callers. ``reset_branch=False`` matches the
        historical ``-b`` behavior — pre-existing branches survive a
        failed cut.

        The helper raises ``subprocess.CalledProcessError`` on a failed
        ``git worktree add``; we wrap that in ``RuntimeError`` with the
        git stderr verbatim so the dispatcher's existing contract
        (caller catches ``RuntimeError`` and surfaces the message to the
        M) stays intact. ``FileExistsError`` propagates verbatim.
        """
        try:
            cut_worktree(
                repo_root=self.repo_root,
                branch=branch,
                worktree_path=worktree_path,
                reset_branch=False,
                overwrite_worktree=False,
            )
        except FileExistsError:
            raise
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "")
            stdout = (exc.stdout or "")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"git worktree add failed for branch {branch!r}: "
                f"{(stderr or stdout or 'unknown error').strip()}"
            ) from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acp_dispatch.py",
        description="M-tier ACP dispatcher (closes #282).",
    )
    parser.add_argument(
        "--round",
        required=True,
        help="round descriptor (matches <orch_worktree>/.dev-kit/round-<round>/)",
    )
    parser.add_argument(
        "--prs",
        required=True,
        help=(
            "comma-separated PR specs of the form 'PR-<index>:<slug>'. "
            "Example: 'PR-3:l6-alpha,PR-2:launcher'"
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=os.getcwd(),
        help="absolute path to the orch-worktree (default: cwd)",
    )
    parser.add_argument(
        "--parent-session-cwd",
        default=os.getcwd(),
        help="absolute path of the parent session's cwd",
    )
    parser.add_argument(
        "--plugin-version-target",
        default=None,
        help="pre-computed plugin version slot (e.g. 0.3.84). "
             "Falls back to the version in .claude-plugin/plugin.json "
             "when omitted; the dispatcher fails closed if neither is "
             "available so a stale default never reaches a T.",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="override the canonical template path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render envelopes without cutting worktrees or writing files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON line per dispatched PR (machine-readable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    prs: list[tuple[str, str]] = []
    for raw in args.prs.split(","):
        raw = raw.strip()
        if not raw:
            continue
        prs.append(parse_pr_spec(raw))
    dispatcher = ACPDispatcher(
        repo_root=Path(args.repo_root),
        round_slug=args.round,
        parent_session_cwd=Path(args.parent_session_cwd),
        plugin_version_target=args.plugin_version_target,
        template_path=Path(args.template) if args.template else None,
        dry_run=args.dry_run,
    )
    results = dispatcher.dispatch(round=args.round, prs=prs)
    if args.json:
        for result in results:
            payload = {
                "branch": result.spec.branch,
                "pr_index": result.spec.pr_index,
                "worktree_path": str(result.worktree_path),
                "envelope_path": str(result.envelope_path),
                "dry_run": result.dry_run,
            }
            print(json.dumps(payload))
    else:
        for result in results:
            tag = "[DRY-RUN] " if result.dry_run else ""
            print(
                f"{tag}PR-{result.spec.pr_index} {result.spec.branch}: "
                f"worktree={result.worktree_path} envelope={result.envelope_path}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(main())
