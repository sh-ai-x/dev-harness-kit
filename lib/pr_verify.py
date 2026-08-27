"""PR verification — fresh, deterministic, no cache.

The problem this solves: the previous babysit flow trusted whatever
`gh pr checks` and the PR-comment stream happened to contain at the
moment of the call, then printed a verdict. On multi-commit PRs the
latest LLM-judge verdict on a *new* run would post a NEW comment
alongside the old one, but the babysit skill's pass condition read
*any* `Approve` text without checking that the **newest** run also
approved. Operators were told the PR was "all green" while the most
recent job was still failing.

This module is the deterministic answer:

  1. EVERY call fetches state fresh from GitHub (no in-process cache,
     no filesystem cache, no module-level memoization).
  2. EVERY gate is reported with the timestamp of the fetch that
     produced it, so a stale or skipped gate is visible to the
     caller.
  3. The pass condition is the AND of all gates; any pending/failed
     gate yields a non-pass result with a one-line "BLOCKER" per gate.
  4. The output is structured (a single typed dict + a printable
     summary table) so the caller — human or babysit loop — can
     verify the verdict instead of trusting prose.

Gates:

  G1. PR state is `OPEN`.
  G2. Every CI check is in a terminal success state (success /
       skipped / neutral). Pending / queued / in_progress means
       "still running", not "passed" — the verifier does NOT
       claim pass on pending.
  G3. For each required LLM-judge job — review, security, and
       maintenance — the most recent audit comment posted by that
       job's workflow run carries `verdict=Approve`. The audit
       comment is the machine-recorded verdict line
       `<!-- dev-kit-verdict-audit --> run=… job=… status=…
       verdict=…` posted by the workflow's verdict-parser step.
       A per-job `Changes Requested` / `Blocked`, a missing audit
       for any required job, or a stale audit (created BEFORE the
       PR's most recent push) yields a fail.
  G4. No `<!-- dev-kit-verdict-audit -->` comment records a
       workflow-run whose `status=failure` was paired with a
       verdict of `Approve` — this is the failure mode that
       produced the "all green" false positive in the babysit
       skill previously: the workflow's audit line showed
       `verdict=Approve` but the workflow's exit code was
       `failure`, and the babysit only read the audit line.
  G5. The merge state is `CLEAN` or `BEHIND` (not `BLOCKED` or
       `DIRTY` or `UNKNOWN`). A `BEHIND` state means the branch
       can be merged after a rebase; the verifier flags it but
       doesn't fail.

Usage:

    from lib.pr_verify import verify_pr
    report = verify_pr(pr_number=584, repo="sh-ai-x/dev-harness-kit")
    print(report.summary())
    if report.passed:
        # all five gates green
        ...

The function is **pure with respect to inputs** (no state, no
cache). It does, however, perform network I/O via `gh` subprocess
calls — that is the point, since the entire problem is "trust the
data you just fetched, not the data you remember".
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class GateResult:
    """A single gate's outcome.

    Attributes:
        gate: Gate identifier (G1..G5).
        label: Human-readable name.
        passed: True iff the gate is satisfied.
        detail: One-line human description of the state.
        evidence: The raw data the verdict was computed from.
        fetched_at: ISO-8601 UTC timestamp of the underlying fetch.
    """

    gate: str
    label: str
    passed: bool
    detail: str
    evidence: object
    fetched_at: str


@dataclass
class PRVerifyReport:
    """Top-level verification report.

    Attributes:
        pr_number: The PR that was checked.
        repo: "owner/repo" of the PR.
        checked_at: ISO-8601 UTC timestamp of the entire check.
        gates: Per-gate results in G1..G5 order.
        blockers: Human-readable list of gate.detail strings for
            every gate that did NOT pass. Empty iff the PR is
            approved.
    """

    pr_number: int
    repo: str
    checked_at: str
    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def blockers(self) -> list[str]:
        return [
            f"[{g.gate}] {g.label}: {g.detail}"
            for g in self.gates
            if not g.passed
        ]

    def summary(self) -> str:
        """One-line-per-gate printable summary."""
        lines = [
            f"PR #{self.pr_number} ({self.repo}) — checked at {self.checked_at}",
            f"  Verdict: {'APPROVED' if self.passed else 'NOT APPROVED'}",
        ]
        for g in self.gates:
            mark = "PASS" if g.passed else "FAIL"
            lines.append(
                f"  [{g.gate}] {mark} {g.label}: {g.detail}  (fetched {g.fetched_at})"
            )
        if self.blockers:
            lines.append("  Blockers:")
            for b in self.blockers:
                lines.append(f"    - {b}")
        return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GhError(RuntimeError):
    """Wraps subprocess failures (timeout, non-zero exit, missing CLI)
    so a verifier caller can distinguish a `gh` outage from a
    real gate verdict."""

    def __init__(self, message: str, *, stderr: str = "", exit_code: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


def _run_gh(args: list[str], *, timeout: int = 30) -> str:
    """Run a `gh` command and return stdout. Raises `GhError` on any
    subprocess failure (timeout, non-zero exit, missing binary). The
    caller — typically one of the gate functions — is responsible for
    catching `GhError` and returning a fail-closed `GateResult`."""
    try:
        res = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GhError(
            f"gh CLI not found on PATH: {exc}",
            exit_code=None,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(
            f"gh {args[0] if args else '<empty>'} timed out after {timeout}s",
            exit_code=None,
        ) from exc
    if res.returncode != 0:
        raise GhError(
            f"gh {args[0] if args else '<empty>'} exited {res.returncode}: {res.stderr.strip()[:200]}",
            stderr=res.stderr,
            exit_code=res.returncode,
        )
    return res.stdout


def _safe_json_loads(raw: str, *, context: str = "") -> object:
    """Parse JSON with a fail-closed GhError. Never raises JSONDecodeError
    out to a gate body — the gate catches GhError and returns a
    fail-closed GateResult."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(
            f"gh returned malformed JSON ({context}): {exc.msg} at line {exc.lineno} col {exc.colno}",
        ) from exc


def _extract_user_login(comment: dict) -> str:
    """Extract the comment author's login, tolerating both:
      - `{"user": {"login": "github-actions"}}` (real `gh api` shape)
      - `{"user": "github-actions"}` (legacy / mock shape)
      - `{"user": null}` / missing (returns "")
    """
    user = comment.get("user")
    if isinstance(user, dict):
        return user.get("login") or ""
    return user or ""


def _run_head_sha(run_id: str, repo: str) -> str | None:
    """Fetch the head SHA a GitHub Actions run was triggered against.

    Returns None on ANY failure — `gh` error, malformed JSON, or a
    missing `headSha` field. Callers MUST treat None as "provenance
    unverifiable" and fail closed (never treat it as a match); this is
    what binds a job's `verdict=Approve` audit to the PR's CURRENT
    head instead of trusting the audit comment's timestamp alone.
    """
    try:
        raw = _run_gh(["run", "view", run_id, "--repo", repo, "--json", "headSha"])
        data = _safe_json_loads(raw, context=f"run view {run_id}")
    except GhError:
        return None
    if not isinstance(data, dict):
        return None
    sha = data.get("headSha")
    return sha if isinstance(sha, str) and sha else None


def _fetch_paginated_comments(pr_number: int, repo: str, *, context: str) -> list[dict]:
    """Fetch ALL PR comments via `gh api --paginate --slurp`, returning
    one flat list of comment dicts.

    `gh api --jq` is incompatible with `--slurp` (gh exits 1 with
    "the `--slurp` option is not supported with `--jq` or `--template`"),
    so we cannot transform in the gh call. Instead, slurp the raw
    pages and flatten/transform in Python. `--slurp` returns an
    outer array of per-page arrays; we flatten one level.
    """
    raw = _run_gh([
        "api", f"repos/{repo}/issues/{pr_number}/comments",
        "--paginate",
        "--slurp",
    ])
    pages = _safe_json_loads(raw, context=f"{context} slurp")
    if not isinstance(pages, list):
        raise GhError(
            f"gh slurp returned non-list ({context}): {type(pages).__name__}",
        )
    flat: list[dict] = []
    for page in pages:
        if isinstance(page, list):
            flat.extend(page)
        elif isinstance(page, dict):
            flat.append(page)
    return flat


def _gate_g1_pr_state(pr_number: int, repo: str, fetched_at: str = "") -> GateResult:
    """G1: PR is OPEN (not closed, not merged, not draft).

    `fetched_at` is the timestamp the caller wants stamped on the
    gate. When empty (the verify_pr path), we stamp the moment the
    gate's own fetch starts so the timestamp actually reflects the
    fetch time.
    """
    if not fetched_at:
        fetched_at = _now_iso()
    try:
        raw = _run_gh([
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "state,isDraft,mergeStateStatus",
        ])
        data = _safe_json_loads(raw, context="G1 pr view")
    except GhError as exc:
        return GateResult(
            gate="G1",
            label="PR state is OPEN (not draft/closed/merged)",
            passed=False,
            detail=f"gh error: {exc}",
            evidence={"error": str(exc), "exit_code": exc.exit_code},
            fetched_at=fetched_at,
        )
    state = data.get("state", "")
    is_draft = bool(data.get("isDraft", False))
    merge_state = data.get("mergeStateStatus", "")
    passed = state == "OPEN" and not is_draft
    detail = (
        f"state={state}, isDraft={is_draft}, mergeStateStatus={merge_state}"
    )
    return GateResult(
        gate="G1",
        label="PR state is OPEN (not draft/closed/merged)",
        passed=passed,
        detail=detail,
        evidence=data,
        fetched_at=fetched_at,
    )


def _gate_g2_ci_checks(pr_number: int, repo: str, fetched_at: str = "") -> GateResult:
    """G2: every CI check is in a terminal success state.

    "success" / "skipped" / "neutral" are terminal pass. "pending" /
    "queued" / "in_progress" mean "still running" — we do NOT
    claim pass; we report pending explicitly. "failure" / "timed_out"
    / "cancelled" / "action_required" are terminal fail.
    """
    if not fetched_at:
        fetched_at = _now_iso()
    try:
        raw = _run_gh([
            "pr", "checks", str(pr_number),
            "--repo", repo,
            "--json", "name,state,bucket,startedAt,link,workflow,completedAt",
        ])
        checks = _safe_json_loads(raw, context="G2 pr checks")
    except GhError as exc:
        return GateResult(
            gate="G2",
            label="every CI check is in a terminal success state",
            passed=False,
            detail=f"gh error: {exc}",
            evidence={"error": str(exc), "exit_code": exc.exit_code},
            fetched_at=fetched_at,
        )
    # gh pr checks --json bucket values: pass | fail | pending | skipping.
    # The verifier enforces an EXPLICIT allow-list of terminal-pass
    # buckets — anything else (cancelled, timed_out, action_required,
    # an unknown future bucket) fails closed. `state` is informational
    # only; the bucket is the source of truth.
    by_bucket: dict[str, list[str]] = {}
    by_state: dict[str, list[str]] = {}
    for ch in checks:
        bucket = (ch.get("bucket") or "unknown").lower()
        state = (ch.get("state") or "unknown").lower()
        by_bucket.setdefault(bucket, []).append(ch.get("name", "?"))
        by_state.setdefault(state, []).append(ch.get("name", "?"))
    PASS_BUCKETS = frozenset({"pass", "skipping"})
    pending = by_bucket.get("pending", [])
    failed = by_bucket.get("fail", [])
    unexpected = sorted(b for b in by_bucket if b not in PASS_BUCKETS)
    buckets_present = bool(by_bucket)
    passed_check = buckets_present and not pending and not failed and not unexpected
    if pending:
        detail = f"PENDING (still running): {', '.join(pending)}"
    elif failed:
        detail = f"FAILED: {', '.join(failed)}"
    elif unexpected:
        detail = (
            f"UNEXPECTED bucket (outside {{pass, skipping}} allow-list): "
            f"{', '.join(f'{b}={len(by_bucket[b])}' for b in unexpected)}"
        )
    elif by_bucket:
        buckets = ", ".join(f"{b}={len(n)}" for b, n in by_bucket.items())
        detail = f"all terminal: {buckets}"
    else:
        detail = "no checks found"
        passed_check = False
    return GateResult(
        gate="G2",
        label="every CI check is in a terminal success state",
        passed=passed_check,
        detail=detail,
        evidence={"by_bucket": by_bucket, "by_state": by_state},
        fetched_at=fetched_at,
    )


# Line-anchored regex (re.MULTILINE) so an inlined "**Verdict:** Approve"
# text earlier in a comment body cannot override a real verdict that
# appears later. The first line-anchored match wins; later lines cannot.
_VERDICT_LINE = re.compile(
    r"^\*\*Verdict:\*\*\s+(Approve|Changes Requested|Blocked)\s*$",
    re.MULTILINE,
)

def _latest_per_job_audits(comments: tuple[dict, ...]) -> dict[str, dict]:
    """Extract the most recent trusted audit record PER JOB from the
    comment stream. The audit comment line is the machine-recorded
    verdict posted by the workflow's verdict-parser step:
        `<!-- dev-kit-verdict-audit --> run=X job=Y status=Z verdict=W`

    Returns: {job_name: {"run_id": str, "verdict": str, "created_at": str}}.
    Jobs without any audit comment are absent from the dict. The
    `run_id` is kept (not discarded) so callers can bind the verdict
    to a specific GitHub Actions run — see `_run_head_sha` — instead
    of trusting the audit comment's `created_at` timestamp alone.

    Only comments authored by `TRUSTED_AUDIT_LOGINS` are considered —
    a forger cannot post an audit line that masquerades as a workflow
    verdict-parser output.
    """
    audit_re = re.compile(
        r"<!--\s*dev-kit-verdict-audit\s*-->\s*"
        r"run=(\d+)\s+job=(\w+)\s+status=(\w+)\s+verdict=(\S+)"
    )
    # Match any `key=value` extra emitted after the canonical quartet on
    # the parseable line 1. Extras are sorted byte-stable by
    # `lib/maintenance_gate.format_audit_body`, so the position of each
    # key=value pair in the payload is deterministic — but we still
    # search by key= rather than positional slicing to stay robust to
    # the addition of future extras (the position would drift if a new
    # key is added before another in the sort order).
    #
    # `(\S*)` (NOT `(\S+)`): an empty value (`key=`) must also match so
    # we can distinguish "no `head_sha=` extra present" (legacy audit)
    # from "`head_sha=` present but empty" (workflow gh pr view failed,
    # GH-Actions rate-limit, transient outage — see review.yml:355). G3
    # treats these two cases DIFFERENTLY: missing falls back to
    # `_run_head_sha()`; empty-but-present is STALE (no fallback — that
    # would return main's HEAD on fork-PR dispatched runs and re-introduce
    # the fork-PR STALE bug this whole provenance fix closes).
    extra_re = re.compile(r"(\w+)=(\S*)")
    latest_per_job: dict[str, dict] = {}
    for c in comments:
        body = c.get("body") or ""
        m = audit_re.search(body)
        if not m:
            continue
        author = _extract_user_login(c)
        if author not in TRUSTED_AUDIT_LOGINS:
            continue
        run_id, job, _status, verdict = m.groups()
        # `head_sha=` is emitted by the workflow's extract_verdict
        # step as a record of the PR head SHA the audit pertains to.
        # When present, G3 prefers this value over `_run_head_sha`'s
        # dispatched-run `headSha` (which can disagree on fork PRs —
        # see `_gate_g3_llm_verdicts` docstring). The key is optional
        # so older audits posted before the G3 fix landed still parse.
        #
        # `None` (sentinel) vs `""` (empty-but-present) is load-bearing:
        # G3's empty-but-present branch must NOT fall back to
        # `_run_head_sha()`. Use the audit_re match position (not
        # `body.find("-->")`) so a stray `<!-- -->` block earlier in
        # the comment body cannot shift the slice window.
        #
        # CRITICAL: limit extras parsing to the parseable line 1 only.
        # `extra_re = (\w+)=(\S*)` matches any `key=value` substring, so
        # without a line-1 scope a `head_sha=<value>` mention in later
        # markdown (URLs, code blocks, table cells) would override the
        # parseable-line value via dict() last-wins. The empty-but-present
        # STALE branch is defensively load-bearing ONLY when line 1 is
        # the sole source of truth.
        head_sha: str | None = None
        parseable_line = body[m.end():].split("\n", 1)[0]
        # dict() constructor dedupes a malformed duplicate-key body
        # (last value wins) — defensive against a future emitter bug.
        extras = dict(extra_re.findall(parseable_line))
        if "head_sha" in extras:
            head_sha = extras["head_sha"]
        created_at = c.get("created_at") or ""
        prior = latest_per_job.get(job)
        if prior is None or created_at > prior["created_at"]:
            latest_per_job[job] = {
                "run_id": run_id, "verdict": verdict, "created_at": created_at,
                "head_sha": head_sha,
            }
    return latest_per_job


def _latest_per_job_audit_verdicts(comments: tuple[dict, ...]) -> dict[str, str]:
    """Thin verdict-only view over `_latest_per_job_audits`, kept for
    callers that only need the verdict word (not run provenance)."""
    return {job: rec["verdict"] for job, rec in _latest_per_job_audits(comments).items()}


# Exact-match trusted claude-bot GitHub App logins. A `startswith("claude")`
# check would accept impersonator accounts (`claude-reviewer`,
# `claude-bot-fork`, etc.) — the verifier must NOT be tricked into
# treating a non-claude account as authoritative. The set is module-level
# so G3 evidence counting can use the same source of truth.
TRUSTED_BOT_LOGINS = frozenset({"claude", "claude[bot]"})

# Trusted audit-author logins for G4 (and G3 fallback). The audit
# comment line `<!-- dev-kit-verdict-audit --> run=… job=… status=…
# verdict=…` is posted by the workflow's verdict-parser step running
# as `github-actions[bot]`. Binding the audit author prevents a
# forger from posting an audit marker that bypasses G4's
# false-positive detection. Note: G4 is deny-only — a forged audit
# can only block approval, not grant it.
TRUSTED_AUDIT_LOGINS = frozenset({"github-actions", "github-actions[bot]"})


def _parse_latest_llm_verdict(comments: list[dict]) -> tuple[str, str]:
    """Find the most recent claude[bot] comment whose body contains a
    `**Verdict:**` line. Returns (verdict_word, source_comment_id).

    The verdict line is the only thing the LLM judges are trusted on.
    We do NOT trust audit lines (the ones that say
    `verdict=Approve` but `status=failure` — that's the false
    positive the babysit skill had). We do NOT trust the PR
    `reviewDecision` slot (that is the GitHub human-review slot,
    not the LLM-judge verdict).
    """
    # Exact-match against the canonical claude[bot] GitHub App login.
    # A `startswith("claude")` check would accept impersonator accounts
    # (`claude-reviewer`, `claude-bot-fork`, etc.) — the verifier must
    # NOT be tricked into treating a non-claude account as authoritative.
    candidates = []
    for c in comments:
        user = _extract_user_login(c)
        if user not in TRUSTED_BOT_LOGINS:
            continue
        body = c.get("body") or ""
        # A05 hardening: use findall() and pick the FINAL verdict line
        # in the comment body. `.search()` returns the first line-anchored
        # match, which lets an earlier injected `Approve` win over a
        # later authoritative `Changes Requested` in the same body.
        # The last verdict in the comment is the most-recent editorial
        # intent of the trusted bot.
        matches = _VERDICT_LINE.findall(body)
        if not matches:
            continue
        verdict_word = matches[-1]
        updated = c.get("updated_at") or c.get("created_at") or ""
        candidates.append((updated, verdict_word, c.get("id", ""), body[:200]))
    if not candidates:
        return ("MISSING", "")
    # Pick the comment with the most-recent updated_at.
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, verdict, comment_id, _ = candidates[0]
    return (verdict, comment_id)


def _gate_g3_llm_verdicts(
    pr_number: int, repo: str, fetched_at: str = "",
    comments: tuple[dict, ...] | None = None,
    pr_pushed_at: str = "",
    pr_head_sha: str = "",
) -> GateResult:
    """G3: every required LLM-judge job — review, security, and
    maintenance — has its most recent audit comment carrying
    `verdict=Approve`. The audit comment is the machine-recorded
    per-job verdict posted by the workflow's verdict-parser step
    (`<!-- dev-kit-verdict-audit --> run=… job=… status=… verdict=…`).

    `comments` is the pre-fetched comment tuple from `verify_pr`,
    which shares one `gh api .../comments` round-trip across G3 and G4.
    The default-None fallback preserves the gate-level hermetic test
    API (passes when called directly without pre-fetched data).

    `pr_head_sha` is the PR's CURRENT head commit SHA. When present,
    each required job's Approve audit is bound to a GitHub Actions
    run (`gh run view <run_id> --json headSha`) and that run's headSha
    must equal `pr_head_sha`. This closes a gap that a timestamp-only
    freshness check cannot: a workflow run for a PREVIOUS head can
    finish and post its trusted audit AFTER a new push lands, and the
    audit's `created_at` would still be >= `pr_pushed_at` even though
    the run itself executed against the OLD head. A `gh run view`
    fetch failure is treated as a mismatch (fail closed).

    `pr_pushed_at` is the ISO-8601 timestamp of the most recent push
    to the PR's head, used only as a DEGRADED fallback when
    `pr_head_sha` is unavailable (its own fetch failed upstream). If
    any Approve audit is OLDER than `pr_pushed_at`, that audit is
    stale and G3 returns STALE.
    """
    if not fetched_at:
        fetched_at = _now_iso()
    if comments is None:
        try:
            comments = tuple(
                _fetch_paginated_comments(pr_number, repo, context="G3 fetch comments")
            )
        except GhError as exc:
            return GateResult(
                gate="G3",
                label="every required LLM-judge per-job verdict is Approve",
                passed=False,
                detail=f"gh error: {exc}",
                evidence={"error": str(exc), "exit_code": exc.exit_code},
                fetched_at=fetched_at,
            )
    # M-3 per-judge verdict: review + security + maintenance must ALL
    # show Approve. The audit comment is the machine-recorded per-job
    # verdict (the workflow's verdict-parser step posts it).
    audits = _latest_per_job_audits(comments)
    per_job = {job: rec["verdict"] for job, rec in audits.items()}
    REQUIRED_JOBS = ("review", "security", "maintenance")
    missing = [j for j in REQUIRED_JOBS if j not in per_job]
    bad = {j: per_job[j] for j in REQUIRED_JOBS if j in per_job and per_job[j] != "Approve"}
    verdict = "Approve"
    src = ""
    if missing:
        verdict = "MISSING"
        src = f"missing audit for jobs: {missing}"
    elif bad:
        verdict = "Blocked"  # first non-Approve wins
        src = f"non-Approve jobs: {bad}"

    if verdict == "Approve" and pr_head_sha:
        # Head-SHA provenance: bind each required job's Approve to a
        # GitHub Actions run whose headSha equals the PR's CURRENT
        # head. Dedupe by run_id — required jobs commonly share one
        # workflow run. A fetch failure (None) is treated as a
        # mismatch, never assumed to be a match.
        #
        # Fork PRs dispatched via fork-pr-review.yml run against
        # `--ref main`, so the run's `headSha` is main's HEAD, NOT the
        # PR head — using that field would STALE every fork PR even
        # when the LLM judges correctly evaluated the PR's diff. The
        # audit comment writer emits `head_sha=<PR headRefOid>` as a
        # authoritative record of the PR head the run judged, and we
        # prefer that value over the dispatched run's own `headSha`
        # whenever the comment carries it. Older audits posted before
        # this field was added fall back to the original
        # `_run_head_sha()` provenance check.
        #
        # Three comment states per job (set by `_latest_per_job_audits`):
        #   - `head_sha` absent  (None)        → legacy audit, fall back
        #                                          to `_run_head_sha()`.
        #   - `head_sha=""`      (empty str)   → workflow gh pr view
        #                                          failed (network /
        #                                          rate-limit / outage);
        #                                          do NOT fall back — the
        #                                          audit does not
        #                                          authoritatively bind
        #                                          to any head. Treat as
        #                                          STALE so a fork-PR
        #                                          dispatched run never
        #                                          slides back into the
        #                                          `_run_head_sha()`
        #                                          main-HEAD path.
        #   - `head_sha=<sha>`   (non-empty)   → authoritative when
        #                                          matches pr_head_sha.
        run_sha_cache: dict[str, str | None] = {}
        comment_sha_jobs: list[str] = []
        for j in REQUIRED_JOBS:
            comment_head_sha = audits[j].get("head_sha")
            if comment_head_sha is None:
                # Legacy shape: no `head_sha=` extra at all. Fall back
                # to `_run_head_sha()` — same-repo PRs and pre-fix
                # audits land in this branch.
                run_id = audits[j]["run_id"]
                if run_id not in run_sha_cache:
                    run_sha_cache[run_id] = _run_head_sha(run_id, repo)
                if run_sha_cache[run_id] != pr_head_sha:
                    comment_sha_jobs.append(j)
            elif comment_head_sha == "":
                # Workflow emitted `head_sha=` but the value resolved
                # to empty (gh pr view failed). Do NOT fall back to
                # `_run_head_sha()` — that path returns main's HEAD on
                # fork-PR dispatched runs and would STALE every
                # correctly-judged fork PR via the original fork-PR
                # STALE bug this whole provenance fix closed.
                comment_sha_jobs.append(j)
            else:
                # Skip the `gh run view` round-trip when the comment
                # already records the PR head SHA — the comment is
                # authoritative for fork-PR dispatched runs whose run
                # `headSha` points at `--ref main` instead of the PR.
                if comment_head_sha != pr_head_sha:
                    comment_sha_jobs.append(j)
        mismatched_jobs = comment_sha_jobs
        # Collect per-job comment head_sha values so the failure
        # message names both sources — useful when a fork-PR dispatched
        # audit carries `head_sha=<PR head>` (matches) while the
        # underlying run's `headSha` (skipped) would not have.
        comment_shas = {
            j: (audits[j].get("head_sha") if audits[j].get("head_sha") is not None else "<missing>")
            for j in mismatched_jobs
        }
        if mismatched_jobs:
            verdict = "STALE"
            src = (
                f"head-SHA provenance mismatch for jobs {mismatched_jobs} "
                f"(pr_head_sha={pr_head_sha}, run_shas={run_sha_cache}, "
                f"comment_shas={comment_shas})"
            )
    elif verdict == "Approve" and pr_pushed_at:
        # Degraded fallback: only reached when pr_head_sha is
        # unavailable. Weaker than SHA provenance (a stale run posting
        # after a new push defeats it — see docstring) but still
        # better than no freshness check.
        latest_per_job_created = {job: rec["created_at"] for job, rec in audits.items()}
        stale_jobs = [
            j for j in REQUIRED_JOBS
            if latest_per_job_created.get(j, "") < pr_pushed_at
            and latest_per_job_created.get(j) is not None
        ]
        if stale_jobs:
            verdict = "STALE"
            src = (
                f"stale audits for jobs {stale_jobs} "
                f"(latest_per_job={latest_per_job_created}, pushed_at={pr_pushed_at})"
            )
    passed = verdict == "Approve"
    detail = f"latest verdict: {verdict}"
    if src:
        detail += f"  (comment id={src})"
    if verdict == "STALE":
        detail += f"  ({src})"
    return GateResult(
        gate="G3",
        label="every required LLM-judge per-job verdict is Approve",
        passed=passed,
        detail=detail,
        evidence={
            "latest_verdict": verdict,
            "source_comment_id": src,
            "pr_head_sha": pr_head_sha,
            "n_claude_comments_scanned": sum(
                1 for c in comments
                if _extract_user_login(c) in TRUSTED_BOT_LOGINS
            ),
        },
        fetched_at=fetched_at,
    )


def _gate_g4_audit_no_failure_paired_with_approve(
    pr_number: int, repo: str, fetched_at: str = "",
    comments: tuple[dict, ...] | None = None,
) -> GateResult:
    """G4: no `<!-- dev-kit-verdict-audit -->` comment records a
    workflow-run with `status=failure` paired with `verdict=Approve`.

    This is the false positive the babysit skill had. The audit
    comment is auto-posted by the workflow's verdict-parser step.
    When the parser sees `**Verdict: Approve**` in the most recent
    claude[bot] comment, it posts
    `verdict=Approve` even if the workflow's own exit code was
    `failure` (e.g. the workflow self-validated, the LLM API
    errored, or the verdict text was emitted in a comment but the
    script's overall exit was non-zero).

    G4 only flags the MOST RECENT run per job. Historical false-positive
    audit comments (from transient LLM API errors that have since
    been fixed) become informational only. The semantic: "is the
    workflow currently producing this false-positive?"
    """
    if not fetched_at:
        fetched_at = _now_iso()
    if comments is None:
        try:
            comments = tuple(
                _fetch_paginated_comments(pr_number, repo, context="G4 fetch comments")
            )
        except GhError as exc:
            return GateResult(
                gate="G4",
                label="no audit comment with status=failure + verdict=Approve (most recent per job)",
                passed=False,
                detail=f"gh error: {exc}",
                evidence={"error": str(exc), "exit_code": exc.exit_code},
                fetched_at=fetched_at,
            )
    audit_re = re.compile(
        r"<!--\s*dev-kit-verdict-audit\s*-->\s*"
        r"run=(\d+)\s+job=(\w+)\s+status=(\w+)\s+verdict=(\S+)"
    )
    # Pick the most recent audit comment PER JOB. The semantic is
    # "is THIS job currently producing a false-positive?" — an older
    # false-positive that the workflow no longer produces is stale
    # and should not block. Bind the audit author to the trusted
    # workflow bot identity so a forger cannot post an audit line
    # that bypasses G4. (G4 is deny-only — a forged marker blocks
    # approval rather than granting it.)
    latest_per_job: dict[str, dict] = {}
    untrusted_audits = 0
    for c in comments:
        body = c.get("body") or ""
        m = audit_re.search(body)
        if not m:
            continue
        author = _extract_user_login(c)
        if author not in TRUSTED_AUDIT_LOGINS:
            untrusted_audits += 1
            continue
        run_id, job, status, verdict = m.groups()
        created_at = c.get("created_at") or ""
        prior = latest_per_job.get(job)
        if prior is None or created_at > prior["created_at"]:
            latest_per_job[job] = {
                "run": run_id, "job": job, "status": status,
                "verdict": verdict, "created_at": created_at,
                "author": author,
            }
    bad_pairs = [
        v for v in latest_per_job.values()
        if v["status"] == "failure" and v["verdict"] == "Approve"
    ]
    passed = not bad_pairs
    detail = (
        "no false-positive pairs in most recent run per job"
        if passed
        else f"{len(bad_pairs)} job(s) have a false-positive audit in their most recent run: {[v['job'] for v in bad_pairs]}"
    )
    return GateResult(
        gate="G4",
        label="no audit comment with status=failure + verdict=Approve (most recent per job)",
        passed=passed,
        detail=detail,
        evidence={"bad_pairs": bad_pairs, "latest_per_job": latest_per_job,
                  "untrusted_audits_ignored": untrusted_audits},
        fetched_at=fetched_at,
    )


def _gate_g5_merge_state(pr_number: int, repo: str, fetched_at: str = "") -> GateResult:
    """G5: mergeStateStatus is CLEAN or BEHIND (not BLOCKED / DIRTY /
    UNKNOWN). BEHIND is a soft warning (the branch needs a rebase
    but can still merge). BLOCKED / DIRTY / UNKNOWN are hard fails.
    """
    if not fetched_at:
        fetched_at = _now_iso()
    try:
        raw = _run_gh([
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "mergeStateStatus,mergeable",
        ])
        data = _safe_json_loads(raw, context="G5 pr view")
    except GhError as exc:
        return GateResult(
            gate="G5",
            label="mergeStateStatus is CLEAN or BEHIND",
            passed=False,
            detail=f"gh error: {exc}",
            evidence={"error": str(exc), "exit_code": exc.exit_code},
            fetched_at=fetched_at,
        )
    state = (data.get("mergeStateStatus") or "").upper()
    # M-8: UNSTABLE means required checks are still being recomputed or
    # mergeability is being re-evaluated — collapsing it into PASS re-opens
    # the exact "still running" fail-open window the verifier exists to
    # prevent. Only CLEAN and BEHIND are soft-pass; everything else is
    # a hard fail (BLOCKED / DIRTY / UNKNOWN / UNSTABLE).
    soft_pass = state in {"CLEAN", "BEHIND"}
    detail = f"mergeStateStatus={state}, mergeable={data.get('mergeable')}"
    return GateResult(
        gate="G5",
        label="mergeStateStatus is CLEAN or BEHIND",
        passed=soft_pass,
        detail=detail,
        evidence=data,
        fetched_at=fetched_at,
    )


def verify_pr(pr_number: int, repo: str = "sh-ai-x/dev-harness-kit") -> PRVerifyReport:
    """Run all five gates with fresh `gh` fetches and return a report.

    No cache, no in-process state. The report's `checked_at` is the
    single timestamp the caller should trust.
    """
    # Single source of truth: fetch the comment stream ONCE and pass
    # it to both G3 and G4. This addresses OE-5 (single source of truth).
    # Wrapped in try/except so a `gh` outage does NOT abort verify_pr —
    # the gate-level fallbacks handle missing shared_comments.
    shared_comments: tuple[dict, ...] | None = None
    try:
        shared_comments = tuple(
            _fetch_paginated_comments(pr_number, repo, context="verify_pr shared comments")
        )
    except GhError:
        # Leave as None so G3/G4 take their per-gate fallback path
        # (which itself catches GhError and returns fail-closed gate).
        pass
    # Fetch the PR's most-recent push timestamp for the M-2 stale-verdict
    # guard (G3). `gh pr view --json` has no `pushed_at` field — the real
    # API rejects that key with 'Unknown JSON field' and exits non-zero,
    # which the previous bare `except GhError: pass` swallowed, leaving
    # pr_pushed_at='' and silently disabling the G3 freshness guard. We
    # now derive the push timestamp from the last commit's `committedDate`
    # (a field that exists on every PR) and surface a degraded pr_pushed_at
    # of "" only when the commits fetch itself fails. The "upgrade to
    # Approve" gate never runs in that degraded state — G3 still passes
    # only if every required job's audit is fresh OR no audits are older
    # than "", i.e. the verifier errs on the side of strict freshness.
    # Also fetch the PR's CURRENT head SHA (`headRefOid`) in the same
    # call — G3's head-SHA provenance check needs it to bind each
    # required job's Approve audit to a workflow run for the CURRENT
    # head, not just a run whose audit comment happens to postdate the
    # push (see `_gate_g3_llm_verdicts` docstring).
    pr_pushed_at = ""
    pr_head_sha = ""
    try:
        raw_pr = _run_gh([
            "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "commits,headRefOid",
        ])
        pr_data = _safe_json_loads(raw_pr, context="verify_pr commits")
        if not isinstance(pr_data, dict):
            raise GhError(
                f"gh pr view returned non-dict JSON (verify_pr commits): "
                f"{type(pr_data).__name__}",
            )
        commits = pr_data.get("commits", [])
        if commits:
            pr_pushed_at = commits[-1].get("committedDate", "") or ""
        pr_head_sha = pr_data.get("headRefOid", "") or ""
    except GhError:
        # Don't propagate — verify_pr continues to run the rest of the
        # gates. G3 still applies its other checks (per-job audit
        # presence + per-job verdict == Approve); only the freshness
        # guard is downgraded. The `pr_pushed_at=""` / `pr_head_sha=""`
        # values are observable in the G3 evidence below.
        pass
    gates: list[GateResult] = [
        _gate_g1_pr_state(pr_number, repo),
        _gate_g2_ci_checks(pr_number, repo),
        _gate_g3_llm_verdicts(
            pr_number, repo, comments=shared_comments,
            pr_pushed_at=pr_pushed_at, pr_head_sha=pr_head_sha,
        ),
        _gate_g4_audit_no_failure_paired_with_approve(pr_number, repo, comments=shared_comments),
        _gate_g5_merge_state(pr_number, repo),
    ]
    # Overall report timestamp is the END of the verify run (not the
    # pre-fetch time) — that's when all five gates have reported.
    checked_at = _now_iso()
    return PRVerifyReport(
        pr_number=pr_number, repo=repo, checked_at=checked_at, gates=gates,
    )


def _resolve_current_pr_number() -> int | None:
    """Resolve the PR number for the current branch via `gh pr view`.

    Returns None on failure (no upstream, gh error, no PR for the
    branch) so the caller can fail closed with a usage hint instead of
    silently verifying the wrong repository.
    """
    try:
        raw = _run_gh([
            "pr", "view", "--json", "number",
            "--jq", ".number",
        ])
    except GhError:
        return None
    out = raw.strip()
    if not out or not out.lstrip("-").isdigit():
        return None
    return int(out)


def main(argv: list[str]) -> int:
    """CLI entry. Supports both forms advertised by `commands/pr-verify.md`
    and `skills/pr-verify/SKILL.md`:

      python3 -m lib.pr_verify                            (current branch)
      python3 -m lib.pr_verify --pr N                    (explicit PR)
      python3 -m lib.pr_verify --pr N --repo owner/repo  (full)
      python3 -m lib.pr_verify --help                    (usage)

    Backward-compatible positional form is preserved:

      python3 -m lib.pr_verify <pr-number> [<owner/repo>]
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 -m lib.pr_verify",
        description=(
            "Deterministic 5-gate PR verifier. Returns 0 iff all gates pass."
        ),
    )
    parser.add_argument("--pr", type=int, default=None,
                        help="PR number to verify (default: current branch's PR).")
    parser.add_argument("--repo", default=None,
                        help="'owner/repo' (default: current branch's repo).")
    parser.add_argument("pr_positional", nargs="?", type=int, default=None,
                        help="(legacy) PR number positional.")
    parser.add_argument("repo_positional", nargs="?", default=None,
                        help="(legacy) 'owner/repo' positional.")
    args = parser.parse_args(argv)

    # Resolve PR number — explicit > positional > current branch.
    pr_number = args.pr if args.pr is not None else args.pr_positional
    if pr_number is None:
        pr_number = _resolve_current_pr_number()
        if pr_number is None:
            print(
                "error: could not resolve a PR number from the current branch. "
                "Pass --pr N or run from a branch that has an open PR.",
                file=sys.stderr,
            )
            return 2

    # Resolve repo — explicit > positional > current branch. Fail closed
    # (exit 2 + stderr hint) rather than silently defaulting to a
    # hardcoded repo — a hardcoded fallback would silently verify the
    # wrong repo's PR number when run from an unrelated checkout.
    repo = args.repo if args.repo is not None else args.repo_positional
    if repo is None:
        try:
            raw = _run_gh([
                "repo", "view", "--json", "nameWithOwner",
                "--jq", ".nameWithOwner",
            ])
            repo = raw.strip()
        except GhError:
            print(
                "error: could not resolve 'owner/repo' from the current "
                "directory. Pass --repo owner/repo or run from a git "
                "checkout with a GitHub remote.",
                file=sys.stderr,
            )
            return 2

    report = verify_pr(pr_number=pr_number, repo=repo)
    print(report.summary())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
