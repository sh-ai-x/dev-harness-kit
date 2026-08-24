#!/usr/bin/env python3
"""ci_triage.py — CI failure triage engine backing /dev-kit:ci-triage.

Resolves a target set of commits, matches each against its GitHub Actions
runs (by full 40-char SHA — `gh run list --commit` silently returns []
on a short SHA), fetches failure detail for runs that did not pass, and
dedupes against a persisted case store (.dev-kit/ci-triage-log.json) keyed
by a stable failure signature. Repeat occurrences of an already-judged
failure are recorded without re-analysis.

This module is deterministic plumbing only. It never writes a case's
cause/evidence/repro/regression_test/proposal content itself — that
judgment is produced by the invoking model and persisted via
`record_judgment` / the `record` CLI subcommand, which validates the
cause pair against CAUSE_TAXONOMY and rejects an empty repro or
regression_test before accepting it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STORE_PATH_DEFAULT = ".dev-kit/ci-triage-log.json"
SCHEMA_VERSION = 3

# A workflow's stale trigger-registration is the canonical
# harness/state-contamination signature: GitHub's server-side cache of
# "which `on:` events fire this workflow" predates the file's last
# commit, so push (or other) events keep queuing phantom runs against a
# trigger the file no longer declares. The fix is a toggle
# disable→enable to force the cache to re-read the current file. The
# proposal field on a judged case is the canonical place this pattern
# is recorded (e.g. "gh api -X PUT .../actions/workflows/<id>/disable
# && gh api -X PUT .../actions/workflows/<id>/enable"), so we match on
# it rather than guessing from the cause pair alone.
_API_TOGGLE_RE = re.compile(r"actions/workflows/(\d+)/(disable|enable)")

# Failure taxonomy (primary cause -> allowed secondary causes). A case's
# classification is the *first* structured field a reader needs — model
# failure (the agent had the right info/tools and still judged wrong),
# context failure (the agent acted reasonably on wrong/missing/stale
# information), or harness failure (the control system around the agent —
# CI, tool schemas, retries, permissions, eval env — is what broke).
# Pure infra failures with no agent in the loop (e.g. a stale GitHub
# Actions trigger registration) are `harness` by definition: there was no
# model reasoning step to blame, and no retrieved/injected context either.
CAUSE_TAXONOMY: dict[str, tuple[str, ...]] = {
    "model": (
        "reasoning-error",              # clear reasoning mistake despite correct info/tools
        "instruction-noncompliance",    # explicit instruction was ignored
        "tool-arg-hallucination",       # wrong tool args even given correct oracle context
        "over-automation",              # acted destructively/broadly without approval
        "search-miscalibration",        # searched when it shouldn't have, or skipped a needed search
        "uncertainty-expression-failure",  # should have said "I don't know" and didn't
    ),
    "context": (
        "retrieval-miss",               # RAG/search didn't surface the relevant doc
        "stale-context",                # answered from outdated information
        "bad-memory-injection",         # persisted memory injected a wrong fact/preference
        "context-window-loss",          # key info fell out of the context window
        "ambiguous-tool-output",        # tool result lacked confidence/source/timestamp
        "context-conflict",             # multiple sources disagreed with no resolution policy
    ),
    "harness": (
        "ambiguous-tool-schema",        # unclear schema led to a wrong call
        "excessive-permission",         # call was right, granted scope was too broad
        "retry-repeats-mistake",        # retry looped without new evidence
        "evaluator-misjudgment",        # the grader/judge scored incorrectly
        "eval-env-drift",               # eval environment diverged from production
        "state-contamination",         # cross-run/persisted state was stale or leaked
        "incomplete-trace",             # logging insufficient to root-cause the failure
        "guardrail-miscalibrated",      # guardrail too strict or too permissive
        "timeout-rate-limit",           # failed on budget/limits, not logic
    ),
}

# gh run/job conclusions that are not failures for triage purposes.
OK_CONCLUSIONS = {"success", "skipped", "neutral", None}

_MARKER_RE = re.compile(r"^X This run likely failed because of (.+)$", re.MULTILINE)


class GitError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout


def full_sha(ref: str) -> str:
    return _run(["git", "rev-parse", ref]).strip()


def resolve_commits(commits: Optional[list[str]], count: Optional[int]) -> list[str]:
    """Full SHAs for the target set. Explicit `commits` win over `count`."""
    if commits:
        return [full_sha(c) for c in commits]
    n = count or 10
    out = _run(["git", "log", f"-{n}", "--format=%H"])
    return [line for line in out.splitlines() if line]


def commit_meta(sha: str) -> dict:
    # rstrip only the trailing newline: %x1f-separated fields can end
    # empty (a root commit has no parents), and str.strip() treats \x1f
    # itself as whitespace, which would eat the last field's separator.
    raw = _run(["git", "log", "-1", "--format=%s%x1f%ae%x1f%P", sha]).rstrip("\n")
    subject, author_email, parents = raw.split("\x1f")
    return {
        "sha": sha,
        "subject": subject,
        "author_email": author_email,
        "parents": parents.split() if parents else [],
    }


def is_bot_commit(meta: dict) -> bool:
    return "github-actions[bot]" in meta["author_email"]


def runs_for_commit(sha: str) -> list[dict]:
    """Workflow runs whose head_sha matches `sha`.

    MUST be the full 40-char SHA: `gh run list --commit` silently returns
    an empty list on a short SHA instead of erroring, which looks
    identical to "no runs for this commit" unless callers guard for it.
    """
    if len(sha) != 40:
        raise ValueError(f"runs_for_commit requires a full 40-char SHA, got {sha!r}")
    out = _run([
        "gh", "run", "list", "--commit", sha, "--limit", "20",
        "--json", "databaseId,name,status,conclusion,event,headBranch,createdAt,url",
    ])
    return json.loads(out)


def failure_signals(run_id: int) -> list[dict]:
    """Best-effort failure detail for a non-passing run — one entry per
    failing job, since a single run can fail more than one job (e.g. both
    `review` and `security` failing at the same step name in one PR Review
    run). Returning only the first failing job would silently drop the
    others from triage.

    Two shapes:
    - zero-job run (e.g. a stale workflow-trigger registration): no job
      was ever scheduled, so `gh run view` prints a one-line diagnostic
      instead of step logs. Returns a single entry.
    - real job failure(s): each failing job/step is identified via the
      Jobs API; `--log-failed` output is a single stream for the whole
      run with each line prefixed `<job name>\\t<step>\\t...`, so it is
      split back out per job by that prefix rather than naively tailed
      (a blind tail can land in a different job's section than the one
      being reported, misattributing the evidence).
    """
    jobs_raw = _run(["gh", "api", f"repos/:owner/:repo/actions/runs/{run_id}/jobs"])
    jobs = json.loads(jobs_raw).get("jobs", [])
    failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

    if not failed_jobs:
        view = _run(["gh", "run", "view", str(run_id)])
        m = _MARKER_RE.search(view)
        marker = m.group(1).strip(". ") if m else "unknown zero-job failure"
        return [{
            "job_name": None,
            "step_name": None,
            "marker": marker,
            "detail": view.strip(),
        }]

    try:
        full_log = _run(["gh", "run", "view", str(run_id), "--log-failed"])
    except GitError:
        full_log = ""

    signals = []
    for job in failed_jobs:
        job_name = job.get("name")
        failed_step = next(
            (s for s in job.get("steps", []) if s.get("conclusion") == "failure"),
            None,
        )
        step_name = failed_step["name"] if failed_step else None
        job_lines = [
            ln for ln in full_log.splitlines() if ln.startswith(f"{job_name}\t")
        ] if job_name and full_log else []
        # GitHub Actions logs put the real failure in a `##[error]`
        # annotation, then often follow it with a large "Post job
        # cleanup" section (git config teardown, etc.). A blind tail
        # slice lands in that boilerplate and truncates the actual
        # error away, so `##[error]` lines are preferred when present.
        error_lines = [ln for ln in job_lines if "##[error]" in ln]
        if error_lines:
            detail = "\n".join(error_lines)[-4000:]
        elif job_lines:
            detail = "\n".join(job_lines)[-4000:]
        else:
            detail = "(no log available)"
        signals.append({
            "job_name": job_name,
            "step_name": step_name,
            "marker": step_name or job_name or "unknown job failure",
            "detail": detail,
        })
    return signals


def signature(workflow_name: str, signal: dict) -> str:
    """Stable dedup key: same workflow + same failing job/step (or the
    same zero-job marker text) collapses repeat occurrences into one case."""
    key = f"{workflow_name}::{signal.get('job_name') or '__workflow_level__'}::{signal.get('marker')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_store(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "cases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_store(path: Path, store: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_case(store: dict, sig: str) -> Optional[dict]:
    return next((c for c in store["cases"] if c["signature"] == sig), None)


def record_occurrence(store: dict, sig: str, workflow_name: str, occurrence: dict) -> dict:
    """Append `occurrence` to the case for `sig`, creating an `unjudged`
    stub case on first sight. Returns the case (store is mutated in place)."""
    case = find_case(store, sig)
    if case is None:
        case = {
            "id": sig,
            "signature": sig,
            "workflow": workflow_name,
            "status": "unjudged",
            "primary_cause": None,
            "secondary_cause": None,
            "evidence": None,
            "repro": None,
            "regression_test": None,
            "proposal": None,
            "hook_proposal": None,
            "first_seen": occurrence,
            "occurrences": [],
        }
        store["cases"].append(case)
    case["occurrences"].append(occurrence)
    return case


def record_judgment(
    store: dict, case_id: str, *, primary_cause: str, secondary_cause: str,
    evidence: str, repro: str, regression_test: str, proposal: str,
    hook_proposal: Optional[str] = None,
) -> dict:
    """Judge an `unjudged` case. The schema is reproduction- and
    regression-prevention-shaped, not analysis-shaped:

    - `primary_cause`/`secondary_cause` — validated against CAUSE_TAXONOMY,
      not a free-text label.
    - `evidence` — the specific log line / API field that proves the
      cause (a citation, not a narrative).
    - `repro` — a concrete, re-runnable recipe (exact commands/inputs)
      that reproduces the failure. Must be re-checkable later to confirm
      the fix actually closed it.
    - `regression_test` — "path::test_name" of an executable test that
      fails before the fix and passes after, or an explicit
      "N/A: <reason>" when a runtime test genuinely isn't feasible.
      Required — this is what turns a case into a regression guard
      instead of a write-up.
    """
    case = next((c for c in store["cases"] if c["id"] == case_id), None)
    if case is None:
        raise KeyError(f"no case with id {case_id!r} — run `scan` first")
    if primary_cause not in CAUSE_TAXONOMY:
        raise ValueError(f"primary_cause must be one of {sorted(CAUSE_TAXONOMY)}, got {primary_cause!r}")
    if secondary_cause not in CAUSE_TAXONOMY[primary_cause]:
        raise ValueError(
            f"secondary_cause must be one of {CAUSE_TAXONOMY[primary_cause]} "
            f"for primary_cause={primary_cause!r}, got {secondary_cause!r}"
        )
    if not regression_test or not regression_test.strip():
        raise ValueError("regression_test is required — use 'N/A: <reason>' if truly not feasible")
    if not repro or not repro.strip():
        raise ValueError("repro is required — a case must be re-runnable, not just described")
    case["status"] = "open"
    case["primary_cause"] = primary_cause
    case["secondary_cause"] = secondary_cause
    case["evidence"] = evidence
    case["repro"] = repro
    case["regression_test"] = regression_test
    case["proposal"] = proposal
    case["hook_proposal"] = hook_proposal
    case["judged_at"] = _now()
    return case


# ---------------------------------------------------------------------------
# Auto-resolution: `process` subcommand.
#
# Closes the loop from "case is judged" to "case is processed" without
# requiring a human to remember to flip the status. The model judges the
# failure once (via `record`); `process` then (a) applies the proposal
# when it matches a known auto-fixable pattern, (b) re-scans a verify
# window to confirm the failure no longer reproduces, and (c) flips
# `open` -> `processed` with a full forensic record of *how* it was
# resolved (which commands ran, the pre/post `updated_at`, the related
# commit + PR if the fix was a code change). A case with fresh
# occurrences after the verify scan is left at `open` with a
# `last_process_attempt` note so the next run can pick it up.
# ---------------------------------------------------------------------------

def _workflow_id_from_proposal(proposal: str) -> Optional[int]:
    """Extract the GitHub workflow ID when `proposal` describes the
    canonical api-toggle fix (disable + enable). Returns None when the
    proposal isn't a recognized auto-fixable pattern — those cases still
    get a verify scan, they just won't have commands auto-run."""
    matches = _API_TOGGLE_RE.findall(proposal or "")
    ids = {wid for wid, _op in matches}
    if len(ids) == 1:
        return int(next(iter(ids)))
    return None


def _gh_workflow_state(workflow_id: int) -> dict:
    """Fetch {state, updated_at} for a workflow — the pre/post
    `updated_at` comparison is the canonical signal that a toggle
    actually refreshed GitHub's trigger cache."""
    out = _run([
        "gh", "api", f"repos/:owner/:repo/actions/workflows/{workflow_id}",
    ])
    data = json.loads(out)
    return {"state": data.get("state"), "updated_at": data.get("updated_at")}


def _apply_api_toggle(workflow_id: int) -> dict:
    """Run disable -> enable and capture {commands_run, verify_pre,
    verify_post, fix_applied_at}. Caller passes the case's proposal
    field; this function just records what was actually executed so
    the log can be audited later. `fix_applied_at` is the moment the
    toggle completed — the verify scan uses it as the "since" cutoff
    so historical failures (which were real at the time but predate
    the fix) don't keep a freshly-resolved case at `open`."""
    pre = _gh_workflow_state(workflow_id)
    cmds = [
        ["gh", "api", "-X", "PUT", f"repos/:owner/:repo/actions/workflows/{workflow_id}/disable"],
        ["gh", "api", "-X", "PUT", f"repos/:owner/:repo/actions/workflows/{workflow_id}/enable"],
    ]
    for c in cmds:
        _run(c)
    post = _gh_workflow_state(workflow_id)
    return {
        "method": "api-toggle",
        "commands_run": [" ".join(c) for c in cmds],
        "verify_pre": pre,
        "verify_post": post,
        "fix_applied_at": _now(),
    }


def _commit_for_fix(workflow_path: str, since_iso: str) -> Optional[dict]:
    """For code-fix resolutions: find the most recent commit on
    `workflow_path` after `since_iso` (case.first_seen.date). Returns
    {sha, subject, pr_number, pr_url} or None when no commit landed.

    The PR lookup uses `gh api repos/:owner/:repo/commits/{sha}/pulls`
    — the only GitHub API that maps a commit to its PR(s) reliably.
    `gh pr list --search <sha>` does NOT search by commit SHA (it
    searches titles/bodies/branches) and silently returns [] for any
    PR whose title doesn't literally contain the SHA, which would
    leave every code-fix audit record with `pr_number: None` —
    breaking the whole "what failed → what fixed it" forensic link.
    The response shape is `[{number, html_url, title}, ...]`; we
    rename `html_url` to `pr_url` to match the rest of the schema."""
    raw = _run([
        "git", "log", f"--since={since_iso}", "--format=%H%x1f%s", "-n", "1", "--", workflow_path,
    ]).strip()
    if not raw:
        return None
    sha, subject = raw.split("\x1f", 1)
    pr_raw = _run([
        "gh", "api", f"repos/:owner/:repo/commits/{sha}/pulls",
        "--jq", ".[0] | {number, html_url, title} // empty",
    ])
    pr = json.loads(pr_raw) if pr_raw.strip() else {}
    return {
        "sha": sha,
        "subject": subject,
        "pr_number": pr.get("number"),
        "pr_url": pr.get("html_url"),
    }


def _signature_present_in_recent_runs(workflow_name: str, since_iso: Optional[str], verify_window: int) -> dict:
    """Light-weight verification: look at recent runs of `workflow_name`
    since `since_iso` (or last `verify_window` runs when since_iso is
    None) and return {ran_at, fresh_failures, last_conclusion,
    last_run_id, last_run_url}.

    The `since_iso` comparison normalizes both sides to second
    precision — GitHub's `createdAt` carries fractional seconds
    (`...38.500Z`), but `_now()` returns `...38Z`. Lexicographically
    `.` < `Z`, so a fractional-second post-fix run would be filtered
    OUT of `fresh_failures` if compared raw. Strip the `.NNN`
    segment from both sides so the cutoff stays on a stable axis."""
    args = [
        "gh", "run", "list", "--workflow", workflow_name,
        "--limit", str(verify_window * 5),
        "--json", "databaseId,conclusion,createdAt,url",
    ]
    raw = _run(args)
    runs = json.loads(raw)
    # Normalize both sides to second precision with no fractional or
    # trailing Z so lex comparison stays on the right axis. GitHub's
    # `createdAt` looks like `2026-07-31T11:16:38.500Z`; `_now()`
    # returns `2026-07-31T11:16:38Z`. Strip the fractional segment
    # AND the trailing Z so both sides reduce to `2026-07-31T11:16:38`.
    cutoff = since_iso.replace("Z", "").split(".")[0] if since_iso else None
    fresh_failures = 0
    last = None
    for r in runs:
        created = (r.get("createdAt") or "").replace("Z", "").split(".")[0]
        if cutoff and created < cutoff:
            continue
        if last is None:
            last = r
        if r.get("conclusion") not in OK_CONCLUSIONS:
            fresh_failures += 1
    return {
        "ran_at": _now(),
        "window_runs": len(runs),
        "fresh_failures": fresh_failures,
        "last_conclusion": (last or {}).get("conclusion"),
        "last_run_id": (last or {}).get("databaseId"),
        "last_run_url": (last or {}).get("url"),
    }


def _resolution_record(case: dict) -> dict:
    """Build the `resolution` block for a case based on its cause +
    proposal. Three methods, derived directly from the proposal text
    (single source of truth — no caller hint required):

    - `api-toggle`: proposal names a `gh api .../actions/workflows/<id>/
      disable` + `enable` pattern. Apply the toggle and record
      commands_run + verify_pre/post.
    - `code-fix`: a commit landed on the workflow file since
      first_seen.date. Look up the linked PR via
      `gh api .../commits/{sha}/pulls`.
    - `manual`: nothing matched. Recorded for forensic completeness
      but the case stays at `open` — a manual case awaiting real
      resolution must not auto-close when the workflow goes quiet
      (empty `gh run list` would otherwise yield fresh_failures == 0
      and the case would flip to `processed` despite no human
      having flagged it resolved)."""
    proposal = case.get("proposal") or ""
    workflow_name = case["workflow"]
    workflow_id = _workflow_id_from_proposal(proposal)

    if workflow_id is not None:
        applied = _apply_api_toggle(workflow_id)
        applied["notes"] = "auto-applied stale workflow registration toggle"
        return applied

    # Code-fix path: look for a commit on the workflow file since the
    # case was first seen.
    first_seen = case.get("first_seen") or {}
    since_iso = first_seen.get("date")
    commit = _commit_for_fix(workflow_name, since_iso) if since_iso else None
    if commit is None:
        return {
            "method": "manual",
            "commands_run": [],
            "notes": "no auto-fix pattern matched and no code-fix commit on the workflow file since first_seen; awaiting manual resolution",
        }
    return {
        "method": "code-fix",
        "commands_run": [],
        "commit": commit,
        "fix_applied_at": _now(),
        "notes": f"code fix landed on {workflow_name} after first_seen={since_iso}",
    }


def process(*, auto_fix: bool, verify_window: int, store_path: Path) -> dict:
    """Auto-resolve `open` cases:

    1. Build a `resolution` block via `_resolution_record(case)` —
       single source of truth derived from the proposal.
    2. If `method == manual`, the case awaits human resolution;
       leave it at `open` and record `last_process_attempt`. A quiet
       workflow is not the same as a resolved one.
    3. For `api-toggle` / `code-fix`, re-scan recent runs and check
       whether the signature still reproduces after `fix_applied_at`.
    4. `fresh_failures == 0` → flip `open` → `processed` with the
       full resolution record and a `post_fix_scan` summary. Else
       leave at `open` with a `last_process_attempt` note.

    Idempotent: already-processed cases are skipped (their existing
    `processed_at` and `resolution` are preserved verbatim, no
    commands are re-run). The save is intentionally once at the end
    of the loop; per-case save is a follow-up if the case count
    grows large enough to make a partial-failure interruption worth
    guarding against.
    """
    store = load_store(store_path)
    summary = {"processed": [], "still_open": [], "skipped_already_processed": []}

    for case in store["cases"]:
        if case["status"] != "open":
            if case["status"] == "processed":
                summary["skipped_already_processed"].append(case["id"])
            continue

        if auto_fix:
            try:
                # Reuse a prior `resolution` block (with its `fix_applied_at`)
                # if the case was previously processed. This makes `process()`
                # idempotent on re-runs against an already-fixed case: the
                # verify scan uses the ORIGINAL fix timestamp so a failure
                # that appeared AFTER that fix can be detected as fresh.
                # Without this, every `process()` call overwrites the
                # resolution and the verify cutoff becomes `now`, which
                # filters every historical failure as "pre-fix" and lets
                # a still-broken case flip to `processed`.
                if not case.get("resolution"):
                    case["resolution"] = _resolution_record(case)
            except GitError as e:
                case["last_process_attempt"] = {
                    "at": _now(),
                    "error": f"auto-fix failed: {e}",
                    "fresh_failures": None,
                    "last_run_url": None,
                }
                summary["still_open"].append({
                    "id": case["id"], "reason": "auto-fix failed",
                    "fresh_failures": None, "last_run_url": None,
                })
                continue
        else:
            case["resolution"] = {
                "method": "manual",
                "commands_run": [],
                "notes": "auto_fix disabled; case awaits manual resolution",
            }

        # Manual cases await human resolution. A quiet workflow
        # shouldn't auto-close them.
        if case["resolution"]["method"] == "manual":
            case["last_process_attempt"] = {
                "at": _now(),
                "fresh_failures": None,
                "last_run_url": None,
                "note": "awaiting manual resolution",
            }
            summary["still_open"].append({
                "id": case["id"], "reason": "awaiting manual resolution",
                "fresh_failures": None, "last_run_url": None,
            })
            continue

        verify = _signature_present_in_recent_runs(
            case["workflow"],
            since_iso=case["resolution"].get("fix_applied_at"),
            verify_window=verify_window,
        )
        case["post_fix_scan"] = {
            **verify,
            "result": "clean" if verify["fresh_failures"] == 0 else "still-failing",
        }

        if verify["fresh_failures"] == 0:
            case["status"] = "processed"
            case["processed_at"] = _now()
            summary["processed"].append({
                "id": case["id"],
                "method": case["resolution"]["method"],
            })
        else:
            case["last_process_attempt"] = {
                "at": _now(),
                "fresh_failures": verify["fresh_failures"],
                "last_run_url": verify["last_run_url"],
            }
            summary["still_open"].append({
                "id": case["id"],
                "reason": f"{verify['fresh_failures']} fresh failure(s) in last {verify['window_runs']} runs after fix_applied_at={case['resolution'].get('fix_applied_at')}",
                "fresh_failures": verify["fresh_failures"],
                "last_run_url": verify["last_run_url"],
            })

    save_store(store_path, store)
    return summary


def scan(*, commits: Optional[list[str]], count: Optional[int], store_path: Path) -> dict:
    """Resolve commits, walk their runs, dedupe against the store.

    Returns {commits: [...], unjudged: [{case, signal}], already_known:
    [case,...]}. `unjudged` entries carry the raw failure `signal` (job/
    step/marker/detail) for the caller to read and judge via
    `record_judgment`; `signal` is never persisted to the store.
    """
    store = load_store(store_path)
    shas = resolve_commits(commits, count)
    commit_rows: list[dict] = []
    unjudged_signals: dict[str, dict] = {}
    touched_sigs: set[str] = set()

    for sha in shas:
        meta = commit_meta(sha)
        runs = runs_for_commit(sha)
        row: dict = {"sha": sha, "subject": meta["subject"], "runs": []}
        if not runs:
            row["note"] = (
                "no direct run (bot-pushed commit — GITHUB_TOKEN pushes don't "
                "trigger new workflow runs; see the parent commit's run instead)"
                if is_bot_commit(meta) else "no direct run found for this commit"
            )
            commit_rows.append(row)
            continue
        for r in runs:
            conclusion = r.get("conclusion")
            entry = {
                "run_id": r["databaseId"], "workflow": r["name"],
                "conclusion": conclusion, "url": r.get("url"),
            }
            if conclusion not in OK_CONCLUSIONS:
                entry["signatures"] = []
                for signal in failure_signals(r["databaseId"]):
                    sig = signature(r["name"], signal)
                    occurrence = {
                        "commit": sha, "run_id": r["databaseId"],
                        "date": r.get("createdAt"), "url": r.get("url"),
                    }
                    case = record_occurrence(store, sig, r["name"], occurrence)
                    entry["signatures"].append({"signature": sig, "case_status": case["status"]})
                    touched_sigs.add(sig)
                    if case["status"] == "unjudged":
                        unjudged_signals[sig] = signal
            row["runs"].append(entry)
        commit_rows.append(row)

    save_store(store_path, store)

    unjudged = [
        {"case": c, "signal": unjudged_signals[c["id"]]}
        for c in store["cases"] if c["id"] in unjudged_signals
    ]
    already_known = [
        c for c in store["cases"]
        if c["id"] in touched_sigs and c["id"] not in unjudged_signals
    ]
    return {"commits": commit_rows, "unjudged": unjudged, "already_known": already_known}


def render_report(store: dict) -> str:
    lines = ["## CI failure triage — case store", ""]
    # Sort: open > unjudged > processed (closed work is reference, not action),
    # ties broken by occurrence count desc.
    cases = sorted(
        store["cases"],
        key=lambda c: (
            {"open": 0, "unjudged": 1, "processed": 2}.get(c["status"], 9),
            -len(c["occurrences"]),
        ),
    )
    if not cases:
        return "\n".join(lines + ["No failure cases recorded yet."])
    for c in cases:
        n = len(c["occurrences"])
        lines.append(f"### [{c['status']}] {c['workflow']} — {c['id']} ({n} occurrence{'s' if n != 1 else ''})")
        if c["status"] == "unjudged":
            lines.append("_not yet judged — run scan output through the model, then `record`_")
        elif c["status"] == "open":
            lines.append(f"- **cause**: {c['primary_cause']} / {c['secondary_cause']}")
            lines.append(f"- **evidence**: {c['evidence']}")
            lines.append(f"- **repro**: {c['repro']}")
            lines.append(f"- **regression_test**: {c['regression_test']}")
            lines.append(f"- **proposal**: {c['proposal']}")
            if c.get("hook_proposal"):
                lines.append(f"- **hook proposal**: {c['hook_proposal']}")
            if c.get("last_process_attempt"):
                # Multi-line shape so the dict's audit fields are
                # readable, matching the processed branch's layout
                # for the resolution block. A raw `repr()` would
                # bury them inside Python syntax.
                lpa = c["last_process_attempt"]
                lines.append("- **last_process_attempt**:")
                for k, v in lpa.items():
                    lines.append(f"  - {k}: {v}")
        elif c["status"] == "processed":
            lines.append(f"- **cause**: {c['primary_cause']} / {c['secondary_cause']}")
            res = c.get("resolution") or {}
            lines.append(f"- **processed_at**: {c.get('processed_at')}")
            lines.append(f"- **resolution.method**: {res.get('method')}")
            if res.get("commands_run"):
                lines.append("- **resolution.commands_run**:")
                for cmd in res["commands_run"]:
                    lines.append(f"  - `{cmd}`")
            if res.get("verify_pre") or res.get("verify_post"):
                lines.append(
                    f"- **resolution.verify**: "
                    f"pre={res.get('verify_pre')} → post={res.get('verify_post')}"
                )
            commit = res.get("commit") or {}
            if commit.get("sha"):
                lines.append(f"- **resolution.commit**: {commit.get('sha')} {commit.get('subject')}")
                if commit.get("pr_number"):
                    lines.append(f"- **resolution.pr**: #{commit.get('pr_number')} {commit.get('pr_url')}")
            if res.get("notes"):
                lines.append(f"- **resolution.notes**: {res['notes']}")
            pfs = c.get("post_fix_scan") or {}
            if pfs:
                lines.append(
                    f"- **post_fix_scan**: result={pfs.get('result')} "
                    f"fresh_failures={pfs.get('fresh_failures')} "
                    f"last_run={pfs.get('last_run_url')}"
                )
        lines.append("")
    return "\n".join(lines)


def _cli() -> int:
    p = argparse.ArgumentParser(prog="ci_triage")
    p.add_argument("--store", default=STORE_PATH_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan")
    g = ps.add_mutually_exclusive_group()
    g.add_argument("--commits", nargs="+")
    g.add_argument("--count", type=int)

    pr = sub.add_parser("record")
    pr.add_argument("--from-json", required=True, help="path to a JSON file: "
                     "{id, primary_cause, secondary_cause, evidence, repro, "
                     "regression_test, proposal, hook_proposal?}")

    sub.add_parser("report")

    # `summary` = scan + render_report in one shot. Use this when you
    # want the current store state after a fresh scan without
    # re-judging anything (already-known signatures get bumped only).
    # `--no-scan` prints the report from the existing store as-is
    # (offline mode for CI logs that are slow to fetch).
    psum = sub.add_parser(
        "summary",
        help="scan (--count/--commits) then render the full report in "
             "one call; no judging step. Use this for the 'just tell me "
             "what's in the store now' flow. Pass --no-scan to skip "
             "the scan and just render the existing store.",
    )
    # --commits / --count drive the scan; --no-scan disables the scan
    # entirely. They're not mutually exclusive on purpose: --no-scan
    # overrides whatever scan scope was given. Validation is enforced
    # at runtime below (one of {--commits, --count} must be set unless
    # --no-scan is passed).
    psum.add_argument("--commits", nargs="+", default=None,
                      help="scan these SHAs then report")
    psum.add_argument("--count", type=int, default=None,
                      help="scan the N most recent commits on HEAD then report")
    psum.add_argument("--no-scan", action="store_true",
                      help="skip the scan; render the report from the "
                           "existing store as-is (offline mode)")

    pp = sub.add_parser("process", help="auto-resolve open cases: "
                        "apply known-pattern fixes, verify via a fresh scan, "
                        "transition open -> processed with a full resolution "
                        "record (commands_run, verify_pre/post, commit + PR info).")
    pp.add_argument("--auto-fix", action="store_true",
                    help="apply the proposal when it matches a known auto-fixable pattern")

    def _clamped_verify_window(raw: str) -> int:
        """argparse `type=` for --verify-window. Clamps to [1, 1000]
        so `--verify-window 0` can't silently produce an empty
        `gh run list` (which would make every case look clean), and
        `--verify-window -1` can't produce a negative --limit that
        makes `gh run list` error."""
        n = int(raw)
        if n < 1 or n > 1000:
            raise argparse.ArgumentTypeError(
                f"--verify-window must be in [1, 1000], got {n}"
            )
        return n

    pp.add_argument("--verify-window", type=_clamped_verify_window, default=10,
                    help="number of recent workflow runs to scan for post-fix verification (default: 10, range: 1-1000)")

    args = p.parse_args()
    store_path = Path(args.store)

    if args.cmd == "scan":
        result = scan(commits=args.commits, count=args.count, store_path=store_path)
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "record":
        payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        store = load_store(store_path)
        record_judgment(
            store, payload["id"],
            primary_cause=payload["primary_cause"], secondary_cause=payload["secondary_cause"],
            evidence=payload["evidence"], repro=payload["repro"],
            regression_test=payload["regression_test"], proposal=payload["proposal"],
            hook_proposal=payload.get("hook_proposal"),
        )
        save_store(store_path, store)
        print(f"recorded judgment for case {payload['id']}")
        return 0

    if args.cmd == "report":
        store = load_store(store_path)
        print(render_report(store))
        return 0

    if args.cmd == "summary":
        # --no-scan renders the store as-is. Otherwise we need exactly
        # one of {--commits, --count} — same as the `scan` subcommand.
        if not args.no_scan:
            if (args.commits is None) == (args.count is None):
                p.error("summary: pass either --commits SHA... or --count N (or use --no-scan)")
            scan(commits=args.commits, count=args.count, store_path=store_path)
        store = load_store(store_path)
        print(render_report(store))
        return 0

    if args.cmd == "process":
        summary = process(
            auto_fix=args.auto_fix, verify_window=args.verify_window,
            store_path=store_path,
        )
        print(json.dumps(summary, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli())
