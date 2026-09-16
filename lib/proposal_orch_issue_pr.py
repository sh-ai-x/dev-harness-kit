"""proposal_orch_issue_pr.py — orchestrator-first GitHub backlog triage engine.

The /dev-kit:proposal-orch-issue-pr skill uses this lib to snapshot the
repository's open PRs + issues, classify them to an orchestrator
boundary, score them on the three triage axes (Bottleneck / Risk /
Change containment), recommend a disposition (keep / replace / defer /
reject), order them into the orchestrator-critical-path buckets, and
compose a YAML proposal that is rendered by the existing
`lib.render_proposal_html` engine.

The module is a deterministic pipeline:

    snapshot_open_backlog(repo)
        -> open_prs, open_issues
    classify(item)        -> boundary
    score(item)           -> (bottleneck, risk, containment)
    recommend_disposition -> "keep"|"replace"|"defer"|"reject"
    bucket_for(item)      -> "hard-stop" | ... | "learning-documentation"
    compose_yaml(...)     -> str (renderable by parse_proposal_yaml)
    render_html(yaml_text, root) -> Path

The gh CLI is the only network-touching boundary; everything else is a
pure function of the snapshot dict. Tests pass a fake `gh_runner` that
returns canned JSON, exercising the pipeline without a real GitHub
account.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from lib import render_proposal_html
from lib.atomic import atomic_write_text
from lib.gh_cli import gh_available

# ----- Constants -------------------------------------------------------------

KST = timezone(timedelta(hours=9))

# Orchestrator boundaries (issue #843 step 3).
BOUNDARIES: Tuple[str, ...] = (
    "state",
    "edit-admission",
    "artifact",
    "side-effect",
    "verification",
    "recovery",
    "throughput",
    "measurement",
    "documentation",
)

# Dispositions (issue #843 acceptance criteria).
DISPOSITIONS: Tuple[str, ...] = ("keep", "replace", "defer", "reject")

# Orchestrator-critical-path ordering buckets.
ORDER_BUCKETS: Tuple[str, ...] = (
    "hard-stop",
    "resume-audit",
    "side-effect-integrity",
    "safe-cleanup",
    "measured-optimization",
    "learning-documentation",
)

# Default umbrella directory the proposal lives under.
UMBRELLA = "long-running-priorities"
DEFAULT_SUB = "open-work-priority"

# New-work window: items created or commented in this many days are
# considered "newly created work" per issue #843 step 1.
NEW_WORK_WINDOW_DAYS = 14

# Wide-PR threshold: >20 files triggers `replace` even with green checks.
WIDE_PR_FILE_THRESHOLD = 20


# ----- Data shapes -----------------------------------------------------------


@dataclass(frozen=True)
class OpenItem:
    """One open PR or issue, normalized for triage.

    `kind` is "pr" or "issue". `number`, `title`, `body` are
    passthrough from gh. `state` is "OPEN" (open issues/PRs only —
    closed items are filtered at snapshot time). `labels` is a tuple
    of label names. `created_at` is ISO 8601. `updated_at` is ISO
    8601. `author` is the login. `url` is the HTML URL. `is_draft` is
    set for PRs. `base_ref_name`, `head_ref_name` are PR-only. For
    issues those fields are empty. `checks_state` is one of
    "success" / "failure" / "pending" / "none" / "" (PR-only;
    `""` for issues). `files_count` is the PR's changed-files count
    or 0 for issues.
    """

    kind: str
    number: int
    title: str
    body: str
    state: str
    labels: Tuple[str, ...]
    created_at: str
    updated_at: str
    author: str
    url: str
    is_draft: bool = False
    base_ref_name: str = ""
    head_ref_name: str = ""
    checks_state: str = ""
    files_count: int = 0

    def containment_score(self, boundary: str) -> int:
        """Return the change-containment score for `boundary` (1-5).

        Mirrors `DEFAULT_SCORES[b][2]`. Pass the boundary explicitly so
        callers don't re-run `classify()` (the score rule already
        classifies once and threads the result through).
        """
        return DEFAULT_SCORES.get(boundary, (3, 3, 3))[2]

    @property
    def is_pr(self) -> bool:
        return self.kind == "pr"


@dataclass(frozen=True)
class ScoredItem:
    """OpenItem + the triage outputs (boundary, scores, disposition, bucket)."""

    item: OpenItem
    boundary: str
    bottleneck: int
    risk: int
    containment: int
    disposition: str
    bucket: str

    @property
    def number(self) -> int:
        return self.item.number

    @property
    def kind(self) -> str:
        return self.item.kind

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def url(self) -> str:
        return self.item.url

    @property
    def labels(self) -> Tuple[str, ...]:
        return self.item.labels

    @property
    def checks_state(self) -> str:
        return self.item.checks_state

    @property
    def files_count(self) -> int:
        return self.item.files_count

    @property
    def is_draft(self) -> bool:
        return self.item.is_draft


@dataclass(frozen=True)
class BacklogSnapshot:
    """The triage-ready view of the open backlog."""

    snapshot_date: str  # YYYY-MM-DD (Asia/Seoul)
    main_head_sha: str
    items: Tuple[OpenItem, ...]

    def prs(self) -> Tuple[OpenItem, ...]:
        return tuple(i for i in self.items if i.is_pr)

    def issues(self) -> Tuple[OpenItem, ...]:
        return tuple(i for i in self.items if not i.is_pr)

    def new_work(self) -> Tuple[OpenItem, ...]:
        cutoff = _parse_iso(self.snapshot_date + "T00:00:00+09:00")
        threshold = cutoff - timedelta(days=NEW_WORK_WINDOW_DAYS)
        out: List[OpenItem] = []
        for i in self.items:
            try:
                created = _parse_iso(i.created_at)
                updated = _parse_iso(i.updated_at)
                if created >= threshold or updated >= threshold:
                    out.append(i)
            except Exception:
                continue
        return tuple(out)


# ----- Errors ----------------------------------------------------------------


class GhUnavailable(RuntimeError):
    """Raised when `gh` is missing or not authenticated."""


class SnapshotError(RuntimeError):
    """Raised when the gh-CLI snapshot fails or returns malformed JSON."""


# ----- gh CLI integration ----------------------------------------------------


# `_run_gh` is the only function that talks to the network. Tests pass
# a fake `gh_runner` instead, exercising the pipeline without a real
# GitHub account. The default calls `gh` via subprocess, raising
# GhUnavailable on missing CLI / unauthenticated / non-zero exit, and
# SnapshotError on malformed JSON.
def _run_gh(args: List[str]) -> str:
    gh_path, degraded = gh_available()
    if not gh_path:
        raise GhUnavailable(degraded or "gh unavailable")
    cp = subprocess.run(
        [gh_path, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if cp.returncode != 0:
        raise SnapshotError(
            f"gh {' '.join(args)} failed: {cp.stderr.strip() or cp.stdout.strip()}"
        )
    return cp.stdout


def _snapshot_date_today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp (Z or +HH:MM). Naive fallback to KST."""
    if not s:
        return datetime.fromtimestamp(0, tz=KST)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Treat malformed timestamps as the epoch so they never look new.
        return datetime.fromtimestamp(0, tz=KST)


# JSON fields we request from `gh pr list` / `gh issue list`. Kept narrow
# so the snapshot is fast and so tests can construct canned fixtures.
PR_LIST_FIELDS = ",".join((
    "number", "title", "body", "state", "labels", "createdAt", "updatedAt",
    "author", "url", "isDraft", "headRefName", "baseRefName",
))
ISSUE_LIST_FIELDS = ",".join((
    "number", "title", "body", "state", "labels", "createdAt", "updatedAt",
    "author", "url",
))


def _common_normalize(raw: Dict[str, Any], *, kind: str) -> OpenItem:
    """Build an OpenItem from a `gh {kind} list` JSON record.

    `kind` is `"pr"` or `"issue"`. PR-specific fields default to empty
    strings / zero counts; issue-specific fields default to PR defaults
    via the dataclass. Both record shapes share the eight fields below
    (number / title / body / state / labels / createdAt / updatedAt /
    author / url).
    """
    author = raw.get("author") or {}
    if isinstance(author, dict):
        author_login = str(author.get("login", ""))
    else:
        author_login = str(author)
    labels = tuple(
        str(label.get("name", "")) for label in (raw.get("labels") or [])
    )
    return OpenItem(
        kind=kind,
        number=int(raw["number"]),
        title=str(raw.get("title", "")),
        body=str(raw.get("body", "") or ""),
        state=str(raw.get("state", "OPEN")),
        labels=labels,
        created_at=str(raw.get("createdAt", "")),
        updated_at=str(raw.get("updatedAt", "")),
        author=author_login,
        url=str(raw.get("url", "")),
        is_draft=bool(raw.get("isDraft", False)) if kind == "pr" else False,
        base_ref_name=str(raw.get("baseRefName", "")) if kind == "pr" else "",
        head_ref_name=str(raw.get("headRefName", "")) if kind == "pr" else "",
    )


def _normalize_pr_safe(raw: Dict[str, Any]) -> OpenItem:
    """Build an OpenItem from a PR-list JSON record."""
    return _common_normalize(raw, kind="pr")


def _normalize_issue(raw: Dict[str, Any]) -> OpenItem:
    """Build an OpenItem from an issue-list JSON record."""
    return _common_normalize(raw, kind="issue")


def _fetch_pr_checks_for(
    pr_number: int,
    runner: Callable[[List[str]], str],
) -> Tuple[str, int]:
    """Fetch checks + files for `pr_number` using `runner`.

    Failures degrade to ("none", 0) so the snapshot does not blow up on
    a transient gh CLI failure.
    """
    try:
        raw = runner(["pr", "view", str(pr_number),
                       "--json", "statusCheckRollup,changedFiles"])
    except (GhUnavailable, SnapshotError):
        return ("none", 0)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ("none", 0)
    rollup = parsed.get("statusCheckRollup") or []
    if not rollup:
        state = "none"
    else:
        states = {str(c.get("conclusion") or c.get("status") or "") for c in rollup}
        if "FAILURE" in states or "failure" in states:
            state = "failure"
        elif ("PENDING" in states
              or "pending" in states
              or "IN_PROGRESS" in states
              or "queued" in states
              or "in_progress" in states
              or "waiting" in states):
            state = "pending"
        else:
            state = "success"
    files = int(parsed.get("changedFiles") or 0)
    return (state, files)


# ----- Pure functions: classify, score, recommend, bucket --------------------


# Boundary classification rule table. Order matters: first match wins.
# Each entry: (regex, boundary). A regex is matched against the
# lowercase concatenation of title + body + labels.
_LABEL_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\barea[:\-]hook\b"), "edit-admission"),
    (re.compile(r"\barea[:\-](state|ralph|orchestrator)\b"), "state"),
    (re.compile(r"\btype[:\-](verification|regression-test)\b"), "verification"),
    (re.compile(r"\btype[:\-](cleanup|janitor|retention)\b"), "recovery"),
    (re.compile(r"\btype[:\-](docs|documentation)\b"), "documentation"),
    (re.compile(r"\barea[:\-](perf|latency|throughput)\b"), "throughput"),
    (re.compile(r"\barea[:\-](measure|metric|dashboard|token)\b"), "measurement"),
    (re.compile(r"\barea[:\-](artifact|build-evidence|phases)\b"), "artifact"),
    (re.compile(r"\barea[:\-](ci|workflow|action)\b"), "verification"),
]

_TITLE_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(worktree[- ]guard|tdd[- ]guard|edit[ -]den|pre[ -]?tool)\b"), "edit-admission"),
    (re.compile(r"\b(journal|evidence|artifact|promote[- ]phases|build[- ]evidence)\b"), "artifact"),
    (re.compile(r"\b(push[ -]?confirm|force[ -]?push|commit[ -]?gate|pre[ -]commit)\b"), "side-effect"),
    (re.compile(r"\b(docs?|documentation|readme)\b"), "documentation"),
    (re.compile(r"\b(janitor|retention|retry|resume|cooldown|cleanup)\b"), "recovery"),
    (re.compile(r"\b(latency|timeout|hang|stall)\b"), "throughput"),
    (re.compile(r"\b(measure|metric|dashboard|scorecard|effectiveness)\b"), "measurement"),
    (re.compile(r"\b(verify|verification|regression|test)\b"), "verification"),
    (re.compile(r"\b(ralph|state[ -]machine|orchest)\b"), "state"),
]


def classify(item: OpenItem) -> str:
    """Map an open item to its orchestrator boundary.

    Label rules are tried first; on no match, title/body pattern rules
    fire; on no match, `state` is the conservative default (anything
    unspecified is assumed to touch state until proven otherwise).
    """
    haystack = " ".join((
        item.title.lower(),
        (item.body or "").lower(),
        " ".join(item.labels).lower(),
    ))
    for rx, boundary in _LABEL_RULES:
        if rx.search(haystack):
            return boundary
    for rx, boundary in _TITLE_RULES:
        if rx.search(haystack):
            return boundary
    return "state"


# Bottleneck / Risk / Containment default table, indexed by boundary.
# Each value is the (B, R, C) heuristic. Heuristic because the repo has
# no stable corpus for stop rate / recovery time / artifact-loss rate
# yet; reviewers can edit the YAML's per-item scores after generation.
DEFAULT_SCORES: Dict[str, Tuple[int, int, int]] = {
    "state":               (4, 4, 3),
    "edit-admission":      (5, 4, 4),
    "artifact":            (5, 3, 4),
    "side-effect":         (4, 4, 4),
    "verification":        (4, 3, 3),
    "recovery":            (3, 3, 3),
    "throughput":          (2, 2, 3),
    "measurement":         (2, 2, 4),
    "documentation":       (1, 1, 5),
}


def score(item: OpenItem, boundary: Optional[str] = None) -> Tuple[int, int, int]:
    """Return (bottleneck, risk, containment) for `item`.

    A wide PR on the critical path adds +1 to risk (more files = more
    blast radius). A red-checked PR adds +1 to bottleneck (a stopped
    run is the canonical bottleneck).
    """
    b = boundary or classify(item)
    base = DEFAULT_SCORES.get(b, (3, 3, 3))
    bottleneck, risk, containment = base
    if item.is_pr and item.files_count > WIDE_PR_FILE_THRESHOLD:
        risk = min(5, risk + 1)
    if item.checks_state == "failure":
        bottleneck = min(5, bottleneck + 1)
    return (bottleneck, risk, containment)


def recommend_disposition(
    item: OpenItem,
    boundary: Optional[str] = None,
) -> str:
    """Pick one disposition based on the item's signature.

    Rule (in order):
    - title contains "duplicate of", "superseded by", "wontfix" → reject
    - body opens with a duplicate-of cross-link → reject
    - critical-path boundary + non-green checks → replace
    - critical-path boundary + wide PR → replace
    - critical-path boundary + green narrow PR + high containment → keep
    - critical-path boundary otherwise → replace (conservative)
    - throughput / measurement / documentation → defer
    """
    b = boundary or classify(item)

    title_lower = item.title.lower()
    body_lower = (item.body or "").lower()
    if any(k in title_lower for k in ("duplicate of", "superseded by", "wontfix", "won't fix")):
        return "reject"
    if "duplicate" in body_lower[:200] and "/issues/" in body_lower[:200]:
        return "reject"

    is_critical_path = b not in ("throughput", "measurement", "documentation")

    if is_critical_path and item.is_pr:
        if item.checks_state == "failure" or item.checks_state == "pending":
            return "replace"
        if item.files_count > WIDE_PR_FILE_THRESHOLD:
            return "replace"
        if item.containment_score(b) >= 4:
            return "keep"
        return "replace"

    if is_critical_path:
        return "replace"

    return "defer"


# Bucket → list of boundaries that bucket holds. Empty bucket means
# the item is dropped from the proposal (e.g. items already in
# `reject` disposition don't need an ordering bucket).
_BUCKET_BOUNDARIES: Dict[str, Tuple[str, ...]] = {
    "hard-stop":              ("edit-admission", "state"),
    "resume-audit":           ("artifact", "verification"),
    "side-effect-integrity":  ("side-effect",),
    "safe-cleanup":           ("recovery",),
    "measured-optimization":  ("throughput", "measurement"),
    "learning-documentation": ("documentation",),
}


def bucket_for(boundary: str) -> str:
    """Map a boundary to its ordering bucket.

    Boundaries not present in `_BUCKET_BOUNDARIES` fall into
    `resume-audit` (the safest mid-critical-path bucket). `reject`
    dispositions are filtered upstream so they never reach this
    function in practice.
    """
    for bucket, bounds in _BUCKET_BOUNDARIES.items():
        if boundary in bounds:
            return bucket
    return "resume-audit"


# ----- Snapshot --------------------------------------------------------------


def snapshot_open_backlog(
    *,
    repo_root: Path,
    gh_runner: Optional[Callable[[List[str]], str]] = None,
) -> BacklogSnapshot:
    """Fetch the open PR + issue snapshot for `repo_root`.

    `gh_runner` defaults to `_run_gh`; tests pass a fake. The function
    fails with SnapshotError if the JSON shape is wrong, GhUnavailable
    if `gh` is missing or not authenticated.
    """
    runner = gh_runner or _run_gh
    main_sha = _read_main_head(repo_root)
    snapshot_date = _snapshot_date_today()

    try:
        prs_raw = json.loads(runner([
            "pr", "list", "--state", "open", "--limit", "200",
            "--json", PR_LIST_FIELDS,
        ]))
        issues_raw = json.loads(runner([
            "issue", "list", "--state", "open", "--limit", "200",
            "--json", ISSUE_LIST_FIELDS,
        ]))
    except json.JSONDecodeError as e:
        raise SnapshotError(f"gh list returned malformed JSON: {e}") from e

    if not isinstance(prs_raw, list) or not isinstance(issues_raw, list):
        raise SnapshotError("gh list returned non-list JSON")

    prs: List[OpenItem] = []
    for raw in prs_raw:
        if not isinstance(raw, dict):
            continue
        base_item = _normalize_pr_safe(raw)
        checks, files = _fetch_pr_checks_for(base_item.number, runner)
        prs.append(OpenItem(
            kind=base_item.kind,
            number=base_item.number,
            title=base_item.title,
            body=base_item.body,
            state=base_item.state,
            labels=base_item.labels,
            created_at=base_item.created_at,
            updated_at=base_item.updated_at,
            author=base_item.author,
            url=base_item.url,
            is_draft=base_item.is_draft,
            base_ref_name=base_item.base_ref_name,
            head_ref_name=base_item.head_ref_name,
            checks_state=checks,
            files_count=files,
        ))

    issues: List[OpenItem] = []
    for raw in issues_raw:
        if not isinstance(raw, dict):
            continue
        issues.append(_normalize_issue(raw))

    return BacklogSnapshot(
        snapshot_date=snapshot_date,
        main_head_sha=main_sha,
        items=tuple(prs + issues),
    )


def _read_main_head(repo_root: Path) -> str:
    """Read the local `origin/main` (or `main`) HEAD SHA. Empty on error."""
    for ref in ("origin/main", "main"):
        try:
            cp = subprocess.run(
                ["git", "rev-parse", ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    return ""


# ----- Compose YAML ----------------------------------------------------------


def _score_one(item: OpenItem) -> ScoredItem:
    """Compute one item's triage outputs in a single pass."""
    boundary = classify(item)
    s = score(item, boundary)
    disposition = recommend_disposition(item, boundary)
    bucket = bucket_for(boundary) if disposition != "reject" else ""
    return ScoredItem(
        item=item,
        boundary=boundary,
        bottleneck=s[0],
        risk=s[1],
        containment=s[2],
        disposition=disposition,
        bucket=bucket,
    )


def compose_yaml(snapshot: BacklogSnapshot) -> Tuple[str, Dict[str, int]]:
    """Build the proposal YAML text + per-disposition counts.

    The output text is byte-identical for a given snapshot (modulo the
    date field, which is `snapshot.snapshot_date`). The grammar matches
    what `lib/render_proposal_html.parse_proposal_yaml` accepts. The
    returned counts dict lets `main()` report the disposition
    distribution without re-running `_score_one` over the snapshot.
    """
    scored: List[ScoredItem] = [_score_one(i) for i in snapshot.items]

    by_bucket: Dict[str, List[ScoredItem]] = {b: [] for b in ORDER_BUCKETS}
    for sc in scored:
        if sc.bucket in by_bucket:
            by_bucket[sc.bucket].append(sc)

    disposition_counts: Dict[str, int] = {d: 0 for d in DISPOSITIONS}
    for sc in scored:
        disposition_counts[sc.disposition] = (
            disposition_counts.get(sc.disposition, 0) + 1
        )

    # The proposal payload is a dict; yaml.safe_dump round-trips cleanly.
    payload: Dict[str, Any] = {
        "title": "Open work priority — orchestrator-first triage",
        "status": "ready-for-review",
        "issue": 843,
        "date": snapshot.snapshot_date,
        "tags": ["long-running", "ralph", "orchestrator", "triage",
                 "github-backlog"],
        "before": {
            "summary": _compose_before_summary(snapshot, scored),
            "evidence": _compose_before_evidence(snapshot, scored, disposition_counts),
        },
        "after": {
            "summary": _compose_after_summary(scored),
            "files": [
                {"path": "docs/proposals/pending/long-running-priorities/open-work-priority.yaml",
                 "change": "Snapshot date + open-PR/issue table + per-bucket sections regenerated by /dev-kit:proposal-orch-issue-pr."},
                {"path": "docs/proposals/pending/long-running-priorities/open-work-priority.html",
                 "change": "Rendered HTML deliverable (via lib/render_proposal_html)."},
            ],
        },
        "pros": _compose_pros(),
        "cons": _compose_cons(),
        "limitations": _compose_limitations(),
        "sections": _compose_sections(snapshot, scored, by_bucket, disposition_counts),
    }
    # `default_flow_style=False` keeps block style for readability.
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=200)
    # YAML header comment: the snapshot provenance. The renderer does not
    # parse comments, but a reviewer reading the source sees when the
    # snapshot was taken and from what HEAD.
    header = (
        f"# Snapshot date: {snapshot.snapshot_date} (Asia/Seoul)\n"
        f"# origin/main HEAD: {snapshot.main_head_sha or 'unknown'}\n"
        f"# Generated by: /dev-kit:proposal-orch-issue-pr\n"
        f"# Open PRs: {len(snapshot.prs())}, Open issues: {len(snapshot.issues())}\n"
    )
    return (header + text, disposition_counts)


def _compose_before_summary(
    snapshot: BacklogSnapshot,
    scored: List[ScoredItem],
) -> str:
    n_prs = len(snapshot.prs())
    n_issues = len(snapshot.issues())
    new = snapshot.new_work()
    return (
        f"The repository has **{n_prs} open PRs** and **{n_issues} open issues** "
        f"as of {snapshot.snapshot_date}. Newly-created work in the last "
        f"{NEW_WORK_WINDOW_DAYS} days: {len(new)} item(s). The previous triage "
        f"treated the open PR list as a merge queue — that abstraction is "
        f"wrong for an orchestrator because an orchestrator is blocked by "
        f"state-boundary failures, not by PR age. A PR may be valuable "
        f"without being mergeable; an issue can be accepted without "
        f"accepting its current PR.\n\n"
        f"This proposal accepts issues independently of their current PRs "
        f"and recommends small replacement PRs from `origin/main` when a "
        f"current PR is wide, off-base, or has red checks."
    )


def _compose_before_evidence(
    snapshot: BacklogSnapshot,
    scored: List[ScoredItem],
    counts: Dict[str, int],
) -> List[str]:
    out: List[str] = [
        f"Snapshot date: {snapshot.snapshot_date} (Asia/Seoul)",
        f"origin/main HEAD: `{snapshot.main_head_sha or 'unknown'}`",
        f"Open PRs: {len(snapshot.prs())}",
        f"Open issues: {len(snapshot.issues())}",
        "Accept-issue-not-PR rule: `/dev-kit:proposal-orch-issue-pr` issue body, step 6",
    ]
    out.extend(f"Disposition `{d}`: {n}" for d, n in counts.items())
    return out


def _compose_after_summary(scored: List[ScoredItem]) -> str:
    return (
        "Backlog is reordered around the orchestrator's critical path. The "
        "first wave hard-stops edit-admission failures and ensures durable "
        "evidence; the second wave makes unattended side effects safe; only "
        "then does the proposal split/replace the large wide PRs.\n\n"
        "Default disposition: **accept the issue, not necessarily the PR**.\n\n"
        "Layers:\n\n"
        "- **State layer:** `RalphState` transitions, attended locking.\n"
        "- **Execution layer:** `lib/execute.py` owns step lifecycle + subprocess evidence.\n"
        "- **Boundary adapters:** promotion, path-aware guards, side-effect gates.\n"
        "- **Protection layer:** worktree/TDD/pre-commit/destructive-confirm hooks stay fail-closed.\n"
        "- **Measurement layer:** effectiveness reports consume provenance, not invented scores."
    )


def _compose_pros() -> List[str]:
    return [
        "Accepts the problem independently of the current PR.",
        "Orchestrator-first ordering: hard stop -> resume/audit -> side-effect -> cleanup -> optimization -> docs.",
        "Deterministic disposition rule (keep / replace / defer / reject) is reviewable from the YAML.",
        "Reuses the existing `lib/render_proposal_html` renderer — no duplicate HTML logic.",
        "Structured before/after + pros/cons/limitations + acceptance gates, same shape as `/dev-kit:proposal`.",
    ]


def _compose_cons() -> List[str]:
    return [
        "Boundary classification is heuristic — label/title patterns can mis-route new areas.",
        "Bottleneck/Risk/Containment scores are operator defaults until the project has a stable corpus.",
        "Single umbrella (`long-running-priorities`) per invocation; other umbrellas are a separate render.",
        "Requires an authenticated `gh` CLI.",
    ]


def _compose_limitations() -> List[str]:
    return [
        "Cannot detect semantic duplicates across open items; reviewers dedupe.",
        "Cannot detect staleness from lack of activity.",
        "`gh pr view --json statusCheckRollup` reads the last CI snapshot, not a fresh trigger.",
        "YAML body is a programmatic scaffold; reviewers add prose justifications before promotion.",
    ]


def _compose_sections(
    snapshot: BacklogSnapshot,
    scored: List[ScoredItem],
    by_bucket: Dict[str, List[ScoredItem]],
    disposition_counts: Dict[str, int],
) -> List[Dict[str, str]]:
    sections: List[Dict[str, str]] = []

    sections.append({
        "title": "Snapshot summary",
        "body": (
            f"- Snapshot date: {snapshot.snapshot_date} (Asia/Seoul)\n"
            f"- origin/main HEAD: `{snapshot.main_head_sha or 'unknown'}`\n"
            f"- Open PRs: **{len(snapshot.prs())}**\n"
            f"- Open issues: **{len(snapshot.issues())}**\n"
            f"- Newly-created work (last {NEW_WORK_WINDOW_DAYS} days): "
            f"**{len(snapshot.new_work())}**\n"
            f"- Dispositions: keep={disposition_counts['keep']}, "
            f"replace={disposition_counts['replace']}, "
            f"defer={disposition_counts['defer']}, "
            f"reject={disposition_counts['reject']}.\n"
        ),
    })

    sections.append({
        "title": "Decision rule: accept issues, not PRs",
        "body": (
            "For each open item, apply this sequence:\n\n"
            "1. Accept or reject the observed problem independently of its proposed implementation.\n"
            "2. Identify the orchestrator boundary: state, edit-admission, artifact, side-effect, verification, recovery, throughput, measurement, documentation.\n"
            "3. Keep the current PR only if it changes one boundary, has a narrow file set, is rebased on `origin/main`, and has green required checks.\n"
            "4. Otherwise mark the PR superseded/replace with a link to the accepted issue and recommend a replacement PR from `origin/main`.\n"
            "5. Do not close the issue until the replacement is merged and its boundary-level acceptance test passes.\n"
        ),
    })

    sections.append({
        "title": "Bottleneck map — orchestrator critical path",
        "body": _render_bottleneck_table(scored),
    })

    sections.append({
        "title": "Open work by orchestrator bucket",
        "body": _render_bucket_sections(by_bucket),
    })

    sections.append({
        "title": "Risk-based disposition table",
        "body": _render_disposition_table(disposition_counts),
    })

    sections.append({
        "title": "Cons, limitations, accepted trade-offs",
        "body": _render_tradeoffs(),
    })

    sections.append({
        "title": "Acceptance gates and exit criteria",
        "body": (
            "A replacement PR is ready for review only if it satisfies all applicable gates:\n\n"
            "- One issue, one boundary, one rollback path; no unrelated cleanup or documentation bundle.\n"
            "- Based on current `origin/main`; current PRs may be closed or marked superseded rather than repaired indefinitely.\n"
            "- Reproducer test exists for the incident or bottleneck being addressed.\n"
            "- Hook changes preserve fail-closed behavior and generated hook-matrix parity.\n"
            "- External side effects have dry-run/default-safe behavior and explicit provenance.\n"
            "- A completion claim includes command, exit code, check identity, and artifact path; an agent exit code is not independent verification.\n"
        ),
    })

    sections.append({
        "title": "Current backlog snapshot",
        "body": _render_snapshot_list(scored),
    })

    return sections


def _render_bottleneck_table(scored: List[ScoredItem]) -> str:
    """Render the per-item B/R/C + disposition table."""
    lines = [
        "| Item | Boundary | B | R | C | Recommendation |",
        "|---|---|---:|---:|---:|---|",
    ]
    bucket_index = {b: i for i, b in enumerate(ORDER_BUCKETS)}
    ordered = sorted(
        scored,
        key=lambda sc: (
            bucket_index.get(sc.bucket, 99),
            -sc.bottleneck,
            -sc.risk,
            sc.item.number,
        ),
    )
    for sc in ordered:
        kind = "PR" if sc.item.is_pr else "Issue"
        lines.append(
            f"| [{kind} #{sc.item.number}]({sc.item.url}) | "
            f"{sc.boundary} | {sc.bottleneck} | {sc.risk} | {sc.containment} | "
            f"{sc.disposition} |"
        )
    return "\n".join(lines) + "\n"


def _render_bucket_sections(by_bucket: Dict[str, List[ScoredItem]]) -> str:
    """Render the per-bucket sections in fixed critical-path order."""
    out: List[str] = []
    for bucket in ORDER_BUCKETS:
        items = sorted(by_bucket.get(bucket, []), key=lambda s: (-s.bottleneck, s.item.number))
        out.append(f"### {bucket}\n")
        if not items:
            out.append("(no items)\n")
            continue
        for sc in items:
            kind = "PR" if sc.item.is_pr else "Issue"
            summary = (sc.item.body or "").splitlines()[0] if sc.item.body else ""
            out.append(
                f"- [{kind} #{sc.item.number}]({sc.item.url}) — "
                f"boundary `{sc.boundary}`, "
                f"B/R/C = {sc.bottleneck}/{sc.risk}/{sc.containment}, "
                f"disposition **{sc.disposition}**, "
                f"checks_state `{sc.item.checks_state or 'n/a'}`, "
                f"files={sc.item.files_count}.\n"
                f"  - {sc.item.title}\n"
                f"  - {summary[:160]}"
            )
        out.append("")
    return "\n".join(out) + "\n"


def _render_disposition_table(counts: Dict[str, int]) -> str:
    """Render the disposition summary table."""
    lines = [
        "| Disposition | Count |",
        "|---|---:|",
    ]
    for d in DISPOSITIONS:
        lines.append(f"| `{d}` | {counts.get(d, 0)} |")
    return "\n".join(lines) + "\n"


def _render_tradeoffs() -> str:
    return (
        "**Trade-off A — minimal fix vs integrated refactor.**\n\n"
        "A path-aware guard fix and a promotion command are less elegant "
        "than a unified orchestration abstraction, but they are observable "
        "and reversible. The proposal accepts temporary adapters and "
        "duplicate boundary code until traces show which abstraction is "
        "actually shared.\n\n"
        "**Trade-off B — explicit promotion vs automatic DONE promotion.**\n\n"
        "Manual promotion leaves a short-lived operator step and is not the "
        "final unattended experience. Automatic promotion is accepted only "
        "after the command is idempotent and its failure behavior is "
        "visible in `next_action`/`blockers`.\n\n"
        "**Trade-off C — split PRs vs delivery speed.**\n\n"
        "Closing/superseding wide PRs can feel slower than merging a "
        "large bundle, but it reduces review ambiguity and makes rollback "
        "local. The proposal prefers two independently mergeable small PRs "
        "over one fast PR that couples cleanup, latency, and protection "
        "behavior.\n\n"
        "**Trade-off D — fail-closed vs unattended continuity.**\n\n"
        "Ambiguous retention, missing evidence, or unverified staged "
        "content may stop a run. That is an intentional cost: a visible "
        "RECOVERY_REQUIRED state is preferable to deleting retained work, "
        "publishing conflict markers, or claiming independent "
        "verification that did not happen.\n\n"
        "**Trade-off E — measured optimization vs perceived speed.**\n\n"
        "Latency-only changes that bypass hook stages or increase false "
        "resumes can lower total harness effectiveness even if "
        "wall-clock time improves. Measurement is required first."
    )


def _render_snapshot_list(scored: List[ScoredItem]) -> str:
    """Render the raw open-PR + open-issue list with disposition + checks."""
    prs = sorted([sc for sc in scored if sc.item.is_pr], key=lambda s: s.item.number)
    issues = sorted([sc for sc in scored if not sc.item.is_pr], key=lambda s: s.item.number)
    lines: List[str] = []
    if prs:
        lines.append("**Open PRs:**")
        for sc in prs:
            lines.append(
                f"- [#{sc.item.number}]({sc.item.url}) — `{sc.disposition}`, "
                f"checks_state=`{sc.item.checks_state or 'n/a'}`, "
                f"files={sc.item.files_count}: {sc.item.title}"
            )
        lines.append("")
    if issues:
        lines.append("**Open issues:**")
        for sc in issues:
            lines.append(
                f"- [#{sc.item.number}]({sc.item.url}) — `{sc.disposition}`: "
                f"{sc.item.title}"
            )
        lines.append("")
    if not prs and not issues:
        lines.append("(no open PRs or issues in this snapshot)")
    return "\n".join(lines) + "\n"


# ----- Render hand-off -------------------------------------------------------


def render_html(
    yaml_text: str,
    *,
    repo_root: Path,
    bucket: str,
    main: str,
    sub: str,
) -> Path:
    """Write `yaml_text` and render the matching HTML via the existing proposal renderer.

    The renderer is invoked via `lib.render_proposal_html.render_from_yaml`
    so the rendering pipeline is exercised end-to-end. The HTML file is
    written atomically via `lib.atomic.atomic_write_text` and lives at
    `docs/proposals/<bucket>/<main>/<sub>.html`.
    """
    bucket = _safe_bucket(bucket)
    main = _safe_slug(main, label="main")
    sub = _safe_slug(sub, label="sub")

    yaml_path = repo_root / "docs" / "proposals" / bucket / main / f"{sub}.yaml"
    html_path = repo_root / "docs" / "proposals" / bucket / main / f"{sub}.html"

    atomic_write_text(yaml_path, yaml_text)

    html_text = render_proposal_html.render_from_yaml(yaml_text)
    atomic_write_text(html_path, html_text)

    return html_path


def _safe_bucket(bucket: str) -> str:
    """Validate the bucket against the BUCKETS whitelist."""
    if bucket not in render_proposal_html.BUCKETS:
        raise ValueError(
            f"bucket must be one of {render_proposal_html.BUCKETS}, got {bucket!r}"
        )
    return bucket


_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _safe_slug(value: str, *, label: str) -> str:
    """Validate a topic slug against the renderer's slug regex."""
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"{label} must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$, got {value!r}"
        )
    return value


# ----- CLI entry ------------------------------------------------------------


# ----- CLI entry ------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """Run the snapshot + compose + render pipeline end-to-end."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m lib.proposal_orch_issue_pr",
        description=(
            "Snapshot the open GitHub backlog, score each item by "
            "orchestrator boundary, compose a YAML proposal, render the "
            "HTML via lib.render_proposal_html."
        ),
    )
    parser.add_argument(
        "--project-root", default=".",
        help="repo root for proposals/ writes (default: cwd)",
    )
    parser.add_argument(
        "--bucket", default="review",
        choices=render_proposal_html.BUCKETS,
        help="proposal bucket (default: review)",
    )
    parser.add_argument(
        "--main", default=UMBRELLA,
        help="proposal umbrella directory (default: long-running-priorities)",
    )
    parser.add_argument(
        "--sub", default=DEFAULT_SUB,
        help="proposal leaf slug (default: open-work-priority)",
    )
    parser.add_argument(
        "--print-yaml", action="store_true",
        help="print the composed YAML to stdout and skip the HTML render",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.project_root).resolve()

    try:
        snapshot = snapshot_open_backlog(repo_root=repo_root)
    except GhUnavailable as e:
        print(f"error: gh CLI unavailable: {e}", file=sys.stderr)
        return 2
    except SnapshotError as e:
        print(f"error: snapshot failed: {e}", file=sys.stderr)
        return 3

    yaml_text, counts = compose_yaml(snapshot)

    if args.print_yaml:
        print(yaml_text)
        return 0

    try:
        html_path = render_html(
            yaml_text,
            repo_root=repo_root,
            bucket=args.bucket,
            main=args.main,
            sub=args.sub,
        )
    except (ValueError, OSError) as e:
        print(f"error: render failed: {e}", file=sys.stderr)
        return 4

    print("## /dev-kit:proposal-orch-issue-pr")
    print()
    print(f"**Snapshot date**: {snapshot.snapshot_date}")
    print(f"**Open PRs**: {len(snapshot.prs())}")
    print(f"**Open issues**: {len(snapshot.issues())}")
    print(f"**Bucket**: {args.bucket}")
    print(
        f"**Source**: docs/proposals/{args.bucket}/{args.main}/{args.sub}.yaml"
    )
    print(
        f"**Output**: docs/proposals/{args.bucket}/{args.main}/{args.sub}.html"
    )
    print()
    print("**Dispositions**:")
    for d in DISPOSITIONS:
        print(f"  - {d}: {counts.get(d, 0)}")
    print()
    print(
        f"**Open in browser**: `open docs/proposals/{args.bucket}/{args.main}/{args.sub}.html`"
    )
    print(f"(wrote {html_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
