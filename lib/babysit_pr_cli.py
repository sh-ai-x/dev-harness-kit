"""babysit_pr_cli.py -- CLI primitives for /dev-kit:babysit-pr (issue #324).

Exposes the `--operator-is-only-human` opt-out for single-operator
repositories. The default behavior is preserved (the skill exits 0 with
the human-review hand-off when `gh pr view --json reviewDecision`
returns ``""``). When the operator runs with the flag, the helper:

  1. reads `.github/CODEOWNERS`,
  2. consults a caller-supplied list of `gh api
     /repos/{owner}/{repo}/collaborators?per_page=100` handles,
  3. refuses (exit 1) if any other owner exists,
  4. otherwise posts the `/ownership-confirmed` audit comment and hands
     off -- it never merges the PR itself. Merging into `main` is
     always a human action (`gh pr merge`, run outside automation).

All side effects are funnelled through tiny shims (`_write_stdout`,
`_write_stderr`, `_post_pr_comment`) so unit tests in
`tests/test_babysit_pr_cli.py` can pin the I/O contract without mocking
`subprocess`. The skill body replaces each shim with the real-world
implementation; tests pin the pure arguments.

Pure functions (no I/O randomness, no global state) mean every branch
of `run_babysit_once` is reproducible in CI without network access.

Exit codes:

  EXIT_OK                  0   -- hand-off OK (default human-gate or
                                  single-owner ownership confirmed --
                                  ready for a human to merge manually).
  EXIT_MULTI_OWNER         1   -- alternate owners found; bypass
                                  refused (matches issue spec).
  EXIT_RATIONALE_REQUIRED  2   -- `--operator-is-only-human` set but
                                  `--rationale` was empty or missing;
                                  the audit comment is incomplete.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from babysit_pr_loop import (  # noqa: E402
    STATE_FILE,
    LoopState,
    load_state,
    new_state,
    observe,
    record_outcome,
    save_state,
)

PathLike = str | Path

# MUST-L3 pytest tail line: the iteration records the command's quoted
# `N passed in Ns` (or `N failed in Ns`) line so a subsequent audit can
# verify the gate actually exercised the suite, not a no-op echo.
# Matches `pytest -q` / `pytest -v` / `pytest --tb=short` output. Other
# test runners (Go, JS) are out of scope for this default; operators
# pass `--local-test-cmd` with a runner that emits a recognizable tail.
PYTEST_TAIL_LINE_RE: str = (
    r"\b\d+ (?:passed|failed)"
    r"(?:, \d+ (?:skipped|xfailed|xpassed))?"
    r" in [\d.]+s\b"
)

# Shell metacharacters in `--local-test-cmd` that suggest the operator
# is using a compound expression rather than a single test command.
# Caller-side lint only -- the orchestrator does NOT block on this,
# because the operator owns the shell. The warning is a one-line hint
# that the trust boundary is wider than the documented example.
_SHELL_METACHARS: frozenset = frozenset(";|&><`$()")

# Exit codes for `run_babysit_once`. Stable across the project so the
# babysit-pr skill can switch on them deterministically.
EXIT_OK: int = 0
EXIT_MULTI_OWNER: int = 1
EXIT_RATIONALE_REQUIRED: int = 2
EXIT_OWNERSHIP_UNKNOWN: int = 4

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
        "--pr",
        type=int,
        metavar="N",
        default=None,
        help="Explicit PR number to babysit instead of current-branch discovery.",
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
    # Hidden power-user override for the pre-push pytest gate. The gate
    # itself is unconditional in /dev-kit:babysit-pr-local; this flag only
    # names the command for non-pytest projects. Suppressed from --help so
    # the operator-facing surface stays 0-arg.
    parser.add_argument(
        "--local-test-cmd",
        default="pytest -q",
        metavar="CMD",
        help=argparse.SUPPRESS,
    )
    # Hidden routing flag for /dev-kit:babysit-pr-local (skill-only no-flag
    # UX). Suppressed from --help so operators never see it; the
    # babysit-pr-local skill body sets it before invoking the parser so
    # the human-gate flow's default-exit-0 path still fires when no flag
    # is set. `is_local_mode()` below is the canonical pre-scan reader.
    parser.add_argument(
        "--local-mode",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    ns = parser.parse_args(list(argv))

    # The rationale-required check lives in `run_babysit_once` rather
    # than here so that `parse_babysit_args(["--operator-is-only-human"])`
    # still returns a usable Namespace (useful for `--help` smoke tests
    # and for callers that want to handle the empty-rationale case at the
    # orchestrator layer). Refusal raises SystemExit from the parser
    # only when the args fail to parse at all (unknown flag, etc.).
    return ns


def is_local_mode(argv: Sequence[str]) -> bool:
    """Return True iff argv contains the hidden `--local-mode` flag.

    The flag is hidden from `parse_babysit_args --help` via
    `argparse.SUPPRESS`, so operators never see it (L5 compliance for
    the skill-only no-flag UX). The babysit-pr-local skill body calls
    this helper at preflight to route to the local-mode algorithm
    BEFORE invoking the parser — operators always run
    `/dev-kit:babysit-pr-local` with no arguments, so the helper is
    effectively a constant `True` for the new skill; it exists to
    (a) keep the routing decision testable in isolation, (b) let other
    callers (e.g. `tests/test_babysit_pr_local_cli.py`) pin the
    contract without re-parsing, and (c) document the contract.

    Pure function. No side effects, no I/O.
    """
    return "--local-mode" in list(argv)


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
    #
    # `encoding="utf-8"` raises UnicodeDecodeError on invalid bytes
    # instead of silently substituting replacement characters.
    # UnicodeDecodeError is NOT an OSError subclass, so without the
    # explicit except below it would escape the orchestrator's fail-
    # closed handler. Treat it the same as an IO failure so the
    # bypass refuses on malformed CODEOWNERS too.
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise OSError(f"CODEOWNERS at {path} is not valid UTF-8: {exc}") from exc

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


def format_ownership_confirmed_comment(operator: str, rationale: str, now_iso: str) -> str:
    """Build the audit comment body posted to the PR.

    Records that the single-operator ownership check passed. This is
    an audit note, not a merge authorization: the PR is NOT auto-merged
    -- a human must run `gh pr merge` themselves.

    Pinned by `tests/test_babysit_pr_cli.py::TestFormatOwnershipConfirmedComment`:

        /ownership-confirmed by operator=<handle> at <ISO-8601>; rationale=<text>

    Semicolons inside `rationale` are preserved verbatim; the comment
    reader splits only on the first two `;` / `=` separators.
    """
    op = operator.lstrip("@")
    return f"/ownership-confirmed by operator={op} at {now_iso}; rationale={rationale}"


# ---------------------------------------------------------------------------
# Local-verify enforcement for the /dev-kit:babysit-pr-local pre-push
# pytest gate.
#
# The pure helper below runs the operator's command, parses the
# pytest-style tail line for MUST-L3 evidence, and returns a structured
# verdict. The babysit-pr-local skill §Algorithm step 7.5 invokes it
# unconditionally; the iteration refuses to advance to commit/push unless
# passed=True.
#
# Threat model: operator-local. The operator already has shell access,
# so `bash -c "$cmd"` is the documented execution mode. Shell-meta lint
# is a caller-side warning, not a block -- the operator owns the
# boundary.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocalVerifyResult:
    """Structured result of `run_local_verify`.

    Fields:
      passed: True iff the command exited 0 AND a pytest tail line was
              found in the combined stdout+stderr. Either failure on
              its own is enough to refuse the gate (MUST-L3).
      exit_code: Process exit code, or None on timeout.
      tail_line: The matched pytest tail line (e.g. "47 passed in 1.23s"),
                 or None if no match.
      stdout: Captured stdout (text, may be empty).
      stderr: Captured stderr (text, may be empty).
      reason: One-line human-readable explanation of the verdict
              ("ok", "exit 1", "timeout after 600s", "missing tail line").
      timed_out: True if the command exceeded `exec_timeout_seconds`.
    """

    passed: bool
    exit_code: int | None
    tail_line: str | None
    stdout: str
    stderr: str
    reason: str
    timed_out: bool = False


def lint_local_test_cmd(cmd: str) -> list[str]:
    """Return a list of shell-metachar warnings for `cmd`.

    The orchestrator does not block on this -- the operator owns the
    shell -- but a one-line warning makes the trust boundary visible.
    Empty input returns no warnings.
    """
    if not cmd:
        return []
    return [f"shell meta '{ch}' in --local-test-cmd" for ch in _SHELL_METACHARS if ch in cmd]


def run_local_verify(
    *,
    cmd: str,
    cwd: PathLike,
    exec_timeout_seconds: int = 600,
    tail_line_re: str = PYTEST_TAIL_LINE_RE,
) -> LocalVerifyResult:
    """Execute `cmd` inside `cwd` and enforce MUST-L3 evidence.

    Behaviour:
      1. Run `bash -c "$cmd"` with `cwd` as the worktree root.
      2. On non-zero exit -> return `passed=False` (reason includes the
         exit code; tail_line is whatever matched, may be None).
      3. On timeout -> return `passed=False`, `timed_out=True`.
      4. On exit 0 + no pytest tail line -> return `passed=False` with
         reason "exit 0 but no pytest-tail line found (MUST-L3
         enforcement)". A green command that doesn't actually run the
         suite MUST NOT pass the gate -- the iteration log would lie
         about its evidence.
      5. On exit 0 + tail line found -> return `passed=True`.

    Pure-ish: only side effect is the subprocess execution. No network,
    no global state, no `gh` calls. Safe to call repeatedly in tests.
    """
    if not cmd:
        return LocalVerifyResult(
            passed=False,
            exit_code=None,
            tail_line=None,
            stdout="",
            stderr="",
            reason="empty --local-test-cmd",
        )

    try:
        completed = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=exec_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return LocalVerifyResult(
            passed=False,
            exit_code=None,
            tail_line=None,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout.decode() if exc.stdout else ""),
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr.decode() if exc.stderr else ""),
            reason=f"timeout after {exec_timeout_seconds}s",
            timed_out=True,
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = stdout + ("\n" + stderr if stderr else "")
    match = re.search(tail_line_re, combined)
    tail_line = match.group(0) if match else None

    if completed.returncode != 0:
        return LocalVerifyResult(
            passed=False,
            exit_code=completed.returncode,
            tail_line=tail_line,
            stdout=stdout,
            stderr=stderr,
            reason=f"exit {completed.returncode}",
        )

    if tail_line is None:
        return LocalVerifyResult(
            passed=False,
            exit_code=completed.returncode,
            tail_line=None,
            stdout=stdout,
            stderr=stderr,
            reason="exit 0 but no pytest-tail line found (MUST-L3 enforcement)",
        )

    return LocalVerifyResult(
        passed=True,
        exit_code=completed.returncode,
        tail_line=tail_line,
        stdout=stdout,
        stderr=stderr,
        reason="ok",
    )


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------
def _load_or_create_loop_state(
    parent_pr: int,
    *,
    current_pr: int | None = None,
    state_path: PathLike = STATE_FILE,
) -> LoopState:
    """Load the durable controller state, or initialize it for this PR."""
    state = load_state(state_path)
    if state is None:
        return new_state(parent_pr, current_pr=current_pr)
    if state.parent_pr != parent_pr:
        raise ValueError(
            f"state belongs to PR #{state.parent_pr}, not PR #{parent_pr}"
        )
    return state


def persist_loop_snapshot(
    *,
    parent_pr: int,
    head_sha: str,
    review_verdict: str | None,
    checks: Iterable[dict[str, object]],
    now_epoch: float,
    now_iso: str,
    current_pr: int | None = None,
    failure_signature: str = "",
    github_tracker_issue: int | None = None,
    linear_issue: str = "",
    state_path: PathLike = STATE_FILE,
) -> LoopState:
    """Persist one fresh GitHub snapshot and return its resumable phase.

    This is the production seam used by the babysit-pr orchestration layer:
    every fresh snapshot is observed and atomically saved before the next
    action is chosen.
    """
    state = _load_or_create_loop_state(
        parent_pr, current_pr=current_pr, state_path=state_path
    )
    if github_tracker_issue is not None or linear_issue:
        state = replace(
            state,
            github_tracker_issue=github_tracker_issue or state.github_tracker_issue,
            linear_issue=linear_issue or state.linear_issue,
        )
    state = observe(
        state,
        head_sha=head_sha,
        review_verdict=review_verdict,
        checks=checks,
        now_epoch=now_epoch,
        now_iso=now_iso,
        failure_signature=failure_signature,
    )
    save_state(state, state_path)
    return state


def persist_loop_outcome(
    *,
    parent_pr: int,
    outcome: str,
    now_iso: str,
    current_pr: int | None = None,
    github_tracker_issue: int | None = None,
    linear_issue: str = "",
    state_path: PathLike = STATE_FILE,
) -> LoopState:
    """Persist repair verification evidence and the next strategy."""
    state = _load_or_create_loop_state(
        parent_pr, current_pr=current_pr, state_path=state_path
    )
    if github_tracker_issue is not None or linear_issue:
        state = replace(
            state,
            github_tracker_issue=github_tracker_issue or state.github_tracker_issue,
            linear_issue=linear_issue or state.linear_issue,
        )
    state = record_outcome(state, outcome=outcome, now_iso=now_iso)
    save_state(state, state_path)
    return state


def run_babysit_once(
    *,
    argv: Sequence[str],
    operator_handle: str,
    codeowners_path: PathLike,
    collaborator_handles: Iterable[str],
    collaborator_lookup_ok: bool,
    pr_number: int,
    now_iso: str = DEFAULT_NOW_ISO,
) -> int:
    """Single-shot babysit-pr orchestrator (pure; side effects via shims).

    The flow:

      1. Parse argv. No flag -> human-gate hand-off (legacy behavior).
      2. Flag set -> require non-empty rationale; verify the
         collaborators API succeeded; read CODEOWNERS.
      3. If any alternate exists OR the CODEOWNERS file could not be
         read OR the collaborators lookup failed -> print the
         alternates (or the IO/API failure) + remediation pointer
         and return the appropriate failure exit code. Fail-closed:
         any outage or glitch MUST NOT authorize the bypass.
      4. Positive-ownership confirmation: the operator must appear
         in the parsed CODEOWNERS list. An empty / unrelated
         CODEOWNERS refuses the bypass.
      5. Otherwise post the `/ownership-confirmed` audit comment and
         return `EXIT_OK`. The PR is never merged automatically --
         auto-merge into `main` is disabled by policy; a human runs
         `gh pr merge` themselves.

    The function never touches `gh` itself; the `_post_*` / `_write_*`
    shims are the only external surface. Tests replace the shims; the
    skill wires them to subprocess + print.
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
            "(the bypass writes it into the /ownership-confirmed audit comment)"
        )
        return EXIT_RATIONALE_REQUIRED

    # Collaborators lookup must succeed before the bypass authorizes
    # anything. An outage, 404, rate limit, permission failure, or
    # empty page must NOT be interpreted as 'no alternates known' --
    # that was the previous fail-open behaviour. The skill runs
    # `gh api ... -q .[].login` with check=True and reports the
    # status here. Missing the positive signal requires refusing.
    if not collaborator_lookup_ok:
        _write_stdout(
            "Refusing --operator-is-only-human: the collaborators API "
            "call did not return a confirmed success (outage, 404, "
            "rate limit, permission, or empty stdout). Without "
            "positive confirmation of who has push access, the bypass "
            "refuses to authorize auto-merge. Falling back to the "
            "human-gate path (REVIEW_REQUIRED -> waiting for human "
            "review)."
        )
        return EXIT_OWNERSHIP_UNKNOWN

    # Fail-closed: an unreadable CODEOWNERS file is treated as evidence
    # of *unknown* ownership, which the bypass must NOT authorize. The
    # previous behaviour swallowed the IO error and returned `[]`, which
    # the caller could interpret as "no alternate owners" and authorize
    # the auto-merge -- that was a security-sensitive bypass. Now any
    # OSError (missing file, permission denied, is-a-directory, encoding
    # error) refuses the bypass with `EXIT_OWNERSHIP_UNKNOWN` and prints
    # the underlying error so the operator can fix the IO and retry.
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
        return EXIT_OWNERSHIP_UNKNOWN
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
    # bypass to authorize. An empty CODEOWNERS + collaborators-ok list
    # is *unknown* ownership, not *single* ownership -- the two cases
    # are not equivalent. Refusing absent-operator closes the gap
    # where a multi-operator repo with an accidentally-narrow
    # CODEOWNERS could otherwise authorize the auto-merge.
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
        return EXIT_OWNERSHIP_UNKNOWN

    # Single-operator ownership confirmed. Auto-merge into main is
    # disabled by policy -- post the audit comment recording the
    # confirmation and hand off; a human merges the PR themselves.
    target_pr_number = args.pr if args.pr is not None else pr_number
    body = format_ownership_confirmed_comment(
        operator=operator_handle,
        rationale=args.rationale.strip(),
        now_iso=now_iso,
    )
    _post_pr_comment(target_pr_number, body)
    _write_stdout(
        f"Single-operator ownership confirmed for PR #{target_pr_number}. "
        f"Audit comment posted. Auto-merge is disabled by policy -- "
        f"merge this PR manually with `gh pr merge`."
    )
    return EXIT_OK
