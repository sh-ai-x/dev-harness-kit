"""babysit_pr_cli.py -- CLI primitives for /dev-kit:babysit-pr (issue #324).

Exposes the `--operator-is-only-human` opt-out for single-operator
repositories. The default behavior is preserved (the skill exits 0 with
the human-review hand-off when `gh pr view --json reviewDecision`
returns ``""``). When the operator runs with the flag, the helper:

  1. reads `.github/CODEOWNERS`,
  2. consults a caller-supplied list of `gh api
     /repos/{owner}/{repo}/collaborators?per_page=100` handles,
  3. refuses (exit 1) if any other owner exists,
  4. otherwise posts the `/bot-approve` audit comment and schedules
     `gh pr merge --auto --squash`.

All side effects are funnelled through tiny shims (`_write_stdout`,
`_write_stderr`, `_post_pr_comment`, `_run_pr_merge`) so unit tests in
`tests/test_babysit_pr_cli.py` can pin the I/O contract without mocking
`subprocess`. The skill body replaces each shim with the real-world
implementation; tests pin the pure arguments.

Pure functions (no I/O randomness, no global state) mean every branch
of `run_babysit_once` is reproducible in CI without network access.

Exit codes:

  EXIT_OK                  0   -- hand-off OK (default human-gate or
                                  single-owner bypass approved).
  EXIT_MULTI_OWNER         1   -- alternate owners found; bypass
                                  refused (matches issue spec).
  EXIT_RATIONALE_REQUIRED  2   -- `--operator-is-only-human` set but
                                  `--rationale` was empty or missing;
                                  the audit comment is incomplete.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable, Sequence

PathLike = str | Path

# Exit codes for `run_babysit_once`. Stable across the project so the
# babysit-pr skill can switch on them deterministically.
EXIT_OK: int = 0
EXIT_MULTI_OWNER: int = 1
EXIT_RATIONALE_REQUIRED: int = 2

# Placeholder for `now_iso` when the caller does not pin one. The real
# skill passes its own ISO-8601 stamp so the audit comment is
# reproducible. Using a fixed string here only matters for the default
# argv tests; the production skill always supplies `now_iso` from
# `gh pr view --json createdAt` or `date -Iseconds`.
DEFAULT_NOW_ISO: str = "1970-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Side-effect shims -- replaced by the real skill (and by tests).
# Each shim's signature is the only contract the orchestrator relies on.
# ---------------------------------------------------------------------------
def _write_stdout(s: str) -> None:
    """Default stdout emitter. Skill replaces this with `print(..., flush=True)`."""
    raise RuntimeError(
        "_write_stdout shim not installed; "
        "lib/babysit_pr_cli.run_babysit_once must run inside the skill"
    )


def _write_stderr(s: str) -> None:
    """Default stderr emitter. Skill replaces this with `print(..., file=sys.stderr, flush=True)`."""
    raise RuntimeError(
        "_write_stderr shim not installed; "
        "lib/babysit_pr_cli.run_babysit_once must run inside the skill"
    )


def _post_pr_comment(pr_number: int, body: str) -> None:
    """Default audit-comment emitter: a guard that refuses to silently
    swallow. The skill wires this to `gh pr comment <n> --body <body>`.
    """
    raise RuntimeError(
        "_post_pr_comment shim not installed; "
        "lib/babysit_pr_cli.run_babysit_once must run inside the skill"
    )


def _run_pr_merge(pr_number: int, argv: Sequence[str]) -> None:
    """Default merge emitter: a guard that refuses to silently swallow.
    The skill wires this to `subprocess.run(['gh', *argv])` where argv
    is `['pr', 'merge', '<n>', '--auto', '--squash']`.
    """
    raise RuntimeError(
        "_run_pr_merge shim not installed; "
        "lib/babysit_pr_cli.run_babysit_once must run inside the skill"
    )


# ---------------------------------------------------------------------------
# Pure CLI helpers.
# ---------------------------------------------------------------------------
def parse_babysit_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the babysit-pr CLI flags.

    The orchestrator's two-mode contract (default human-gate vs
    `--operator-is-only-human` bypass) lives here. Unknown flags raise
    SystemExit via argparse -- the babysit-pr skill surfaces the parser
    error to stderr.

    When `--operator-is-only-human` is set but `--rationale` is empty,
    the parser refuses with a SystemExit and a usage hint. The rationale
    is part of the audit-comment contract; allowing an empty rationale
    would silently weaken the trail.
    """
    parser = argparse.ArgumentParser(
        prog="babysit-pr",
        description=(
            "Iterate a PR to green. The default behavior waits for human "
            "review; pass --operator-is-only-human to opt out of the gate "
            "when the operator is the only reviewer in CODEOWNERS / has the "
            "only push collaborator entry."
        ),
    )
    parser.add_argument(
        "--operator-is-only-human",
        action="store_true",
        help=(
            "Bypass the human-review gate. Refuses with exit 1 if "
            "CODEOWNERS or gh collaborators list shows any alternate "
            "owner. Requires --rationale."
        ),
    )
    parser.add_argument(
        "--rationale",
        default="",
        help=(
            "Audit-trail justification for the bypass. Required when "
            "--operator-is-only-human is set; quoted into the PR comment."
        ),
    )
    ns = parser.parse_args(list(argv))

    # The rationale-required check lives in `run_babysit_once` rather
    # than here so that `parse_babysit_args(["--operator-is-only-human"])`
    # still returns a usable Namespace (useful for `--help` smoke tests
    # and for callers that want to handle the empty-rationale case at the
    # orchestrator layer). Refusal raises SystemExit from the parser
    # only when the args fail to parse at all (unknown flag, etc.).
    return ns


def parse_codeowners(path: PathLike) -> list[str]:
    """Parse a CODEOWNERS file into a sorted, deduplicated list of handles.

    CODEOWNERS rules look like:

        *                 @alice @my-org/devs
        /rules/           @bob
        hooks/*.sh        someone@example.com

    This helper strips comments (lines beginning with `#`), splits each
    rule body on whitespace, drops the leading `@` (and any
    `user@domain`-shaped tokens the spec also allows -- those are not
    review gates we care about), and returns the unique sorted set.

    Code path uses only stdlib so the helper is importable everywhere.

    Fail-closed contract: any IO error reading the file (missing,
    unreadable, permission-denied, is-a-directory) raises so the
    caller cannot interpret the absence of data as "no alternate
    owners". An outage or permission glitch must not authorize the
    auto-merge bypass. The orchestrator in `run_babysit_once`
    catches the failure and treats it as multi-owner.

    An empty file (file exists but no rules) is a legitimate "no
    owners configured" state and returns ``[]`` -- only the read
    itself is held to the fail-closed contract.
    """
    p = Path(path)
    # No `try/except` here -- let any OSError propagate. The orchestrator
    # catches it and refuses the bypass.
    text = p.read_text(encoding="utf-8")

    handles: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]  # drop trailing comments
        for token in line.split():
            if not token:
                continue
            if "@" not in token:
                # A path or glob; ignore.
                continue
            stripped = token.lstrip("@")
            if not stripped:
                continue
            # CODEOWNERS permits `user@domain` (email) handles. They are
            # not actionable reviewer handles for the bypass gate, so
            # we strip them.
            if re.match(r"^[^@\s]+@[^@\s]+$", stripped) and "/" not in stripped:
                continue
            handles.add(stripped)
    return sorted(handles)


def has_alternate_owners(
    operator_handle: str,
    codeowner_handles: Iterable[str],
    collaborator_handles: Iterable[str] = (),
) -> tuple[bool, list[str]]:
    """Detect whether any alternate owner is configured for this PR.

    Returns ``(has_alternate, alternates)`` where ``alternates`` is the
    sorted list of handles distinct from `operator_handle` that appear
    in CODEOWNERS or in the caller-supplied collaborator list.

    Policy (mirrors `lib/babysit_pr_reliability.py`):

      * Operator missing from BOTH inputs -- no alternates are known;
        bypass is permitted (caller treats this as single-operator).
      * Operator present in CODEOWNERS + any other handle -- bypass
        refused; the alternate list comes from the union of CODEOWNERS
        minus operator.
      * CODEOWNERS only knows the operator but the collaborator list has
        other pushers -- bypass refused; the alternate list comes from
        the collaborator list minus operator.

    The helper stays pure (no `gh api` call inside) so the
    orchestrator's behavior is reproducible from fixtures.
    """
    op = operator_handle.lstrip("@")
    co_set = {h.lstrip("@") for h in codeowner_handles}
    collab_set = {h.lstrip("@") for h in collaborator_handles}
    alternates = sorted((co_set | collab_set) - {op} - {""})
    return (len(alternates) > 0, alternates)


def format_bot_approve_comment(operator: str, rationale: str, now_iso: str) -> str:
    """Build the audit comment body posted to the PR.

    The shape is locked by `docs/hook-coverage-gaps.md` and pinned by
    `tests/test_babysit_pr_cli.py::TestFormatBotApproveComment`:

        /bot-approve by operator=<handle> at <ISO-8601>; rationale=<text>

    Semicolons inside `rationale` are preserved verbatim; the comment
    reader splits only on the first two `;` / `=` separators.
    """
    op = operator.lstrip("@")
    return f"/bot-approve by operator={op} at {now_iso}; rationale={rationale}"


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------
def run_babysit_once(
    *,
    argv: Sequence[str],
    operator_handle: str,
    codeowners_path: PathLike,
    collaborator_handles: Iterable[str],
    pr_number: int,
    now_iso: str = DEFAULT_NOW_ISO,
) -> int:
    """Single-shot babysit-pr orchestrator (pure; side effects via shims).

    The flow:

      1. Parse argv. No flag -> human-gate hand-off (legacy behavior).
      2. Flag set -> require non-empty rationale; read CODEOWNERS;
         collect alternates.
      3. If any alternate exists OR the ownership sources could not be
         read -> print the alternates (or the IO failure) + remediation
         pointer and return `EXIT_MULTI_OWNER`. Fail-closed: an outage
         or permission glitch MUST NOT authorize the bypass.
      4. Otherwise post the audit comment and schedule
         `gh pr merge --auto --squash`; return `EXIT_OK`.

    The function never touches `gh` itself; the four `_post_*` / `_run_*`
    / `_write_*` shims are the only external surface. Tests replace the
    shims; the skill wires them to subprocess + print.
    """
    args = parse_babysit_args(argv)

    if not args.operator_is_only_human:
        # Default flow: defer to the human-review gate. This matches
        # the pre-flag behavior of /dev-kit:babysit-pr exactly.
        _write_stdout(
            "REVIEW_REQUIRED -> human-gate. Re-run with "
            "--operator-is-only-human --rationale <text> to opt out "
            "(single-operator repos only)."
        )
        return EXIT_OK

    if not args.rationale.strip():
        # The audit comment requires a non-empty rationale. argparse
        # also catches this, but the orchestrator is defensive in case
        # parse_babysit_args is bypassed in tests / scripted callers.
        _write_stderr(
            "--rationale is required with --operator-is-only-human "
            "(the bypass writes it into the /bot-approve audit comment)"
        )
        return EXIT_RATIONALE_REQUIRED

    # Fail-closed: an unreadable CODEOWNERS file is treated as evidence
    # of *unknown* ownership, which the bypass must NOT authorize. The
    # previous behaviour swallowed the IO error and returned `[]`, which
    # the caller could interpret as "no alternate owners" and authorize
    # the auto-merge -- that was a security-sensitive bypass. Now any
    # OSError (missing file, permission denied, is-a-directory, encoding
    # error) refuses the bypass with `EXIT_MULTI_OWNER` and prints the
    # underlying error so the operator can fix the IO and retry.
    try:
        codeowners = parse_codeowners(codeowners_path)
    except OSError as exc:
        _write_stdout(
            "Refusing --operator-is-only-human: could not read "
            f"CODEOWNERS at {codeowners_path} ({exc!r}). Treat this as "
            "unknown ownership -- the bypass refuses to authorize "
            "auto-merge when ownership cannot be confirmed. "
            "Falling back to the human-gate path (REVIEW_REQUIRED -> "
            "waiting for human review)."
        )
        return EXIT_MULTI_OWNER
    has_alternate, alternates = has_alternate_owners(
        operator_handle=operator_handle,
        codeowner_handles=codeowners,
        collaborator_handles=collaborator_handles,
    )

    if has_alternate:
        # Refuse the bypass. The exit code is 1 per the issue spec so
        # CI sees the failure but the script does not crash. The
        # alt-owner list + human-gate pointer are printed so the
        # operator knows exactly why the bypass was declined.
        names = ", ".join(alternates)
        _write_stdout(
            "Refusing --operator-is-only-human: alternate owner(s) found: "
            f"{names}.\n"
            "These users/teams can supply the missing review. Falling "
            "back to the human-gate path (REVIEW_REQUIRED -> waiting for "
            "human review)."
        )
        return EXIT_MULTI_OWNER

    # Positive-ownership confirmation: even with no alternates found,
    # the operator must be explicitly listed in CODEOWNERS for the
    # bypass to authorize. An empty CODEOWNERS + empty collaborators
    # list is *unknown* ownership, not *single* ownership -- the two
    # cases are not equivalent. Refusing absent-operator closes the
    # gap where an outage on the collaborators endpoint (or an empty
    # but readable CODEOWNERS file) could otherwise authorize the
    # auto-merge during a multi-operator incident.
    op_normalized = operator_handle.lstrip("@")
    if op_normalized not in codeowners:
        _write_stdout(
            f"Refusing --operator-is-only-human: operator "
            f"{op_normalized!r} is not listed in CODEOWNERS at "
            f"{codeowners_path}. Without positive proof of "
            "single-operator ownership, the bypass refuses to "
            "authorize auto-merge. Falling back to the human-gate "
            "path (REVIEW_REQUIRED -> waiting for human review)."
        )
        return EXIT_MULTI_OWNER

    # Single-operator: post the audit comment + schedule auto-merge.
    body = format_bot_approve_comment(
        operator=operator_handle,
        rationale=args.rationale.strip(),
        now_iso=now_iso,
    )
    _post_pr_comment(pr_number, body)
    try:
        _run_pr_merge(pr_number, ["pr", "merge", str(pr_number), "--auto", "--squash"])
    except (SystemExit, Exception) as exc:
        # `check=True` on the merge subprocess raises
        # CalledProcessError on failure (protected branch, stale HEAD,
        # merge-queue state, etc.). The audit comment is already
        # posted at this point, so the operator sees the partial
        # trail. Convert the crash to EXIT_MULTI_OWNER so the
        # orchestrator falls back to the human-gate path without
        # surfacing an unhandled traceback.
        _write_stderr(
            f"gh pr merge --auto --squash failed after the audit "
            f"comment was posted ({exc!r}). The /bot-approve comment "
            f"remains in the PR thread for the audit trail. Falling "
            f"back to the human-gate path; re-investigate the merge "
            f"failure manually."
        )
        return EXIT_MULTI_OWNER
    _write_stdout(
        f"Single-operator bypass approved. "
        f"Audit comment posted; gh pr merge --auto --squash scheduled for PR #{pr_number}."
    )
    return EXIT_OK
