"""request_intent.py — client-neutral request classifier (issue #845).

Pure classifier that turns a raw prompt into a structured
``Classification`` record with a stable ``request_id``, the calling
``client`` (``claude-code`` / ``codex``), a ``mode`` of
``read_only`` | ``proposal`` | ``implementation`` | ``uncertain``, a
``worktree_required`` flag, a ``confidence`` score in [0.0, 1.0], a
list of ``reason_codes`` explaining the decision, a ``prompt_hash``
for audit, and a ``next_action`` describing what the caller should
do next.

Hard rules (per the issue brief):

  * No I/O. The classifier must not read files, call out to git, or
    touch the network. Importable from both the Claude
    ``UserPromptSubmit`` shell adapter and the Codex orchestrator
    path; call sites pass the raw prompt and ``client`` string in.
  * Determinism. Two calls with the same ``prompt`` and ``client``
    produce the same ``Classification`` modulo ``request_id`` and
    ``prompt_hash`` (which both round-trip through stable hashing).
  * No silent implementation. An uncertain prompt yields
    ``mode=uncertain`` — never ``implementation``.

The companion ``Intent`` record (below) is what gets written to
``.dev-kit/round-0/intent.md`` once the originator accepts the
classification. The cut step is gated on
``intent.decision_mode == "accepted"`` (see
``hooks/worktree-auto-cut.sh``).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import FrozenSet, List, Literal, Optional

# ---- public schema ----

Client = Literal["claude-code", "codex"]
Mode = Literal["read_only", "proposal", "implementation", "uncertain"]
DecisionMode = Literal["accepted", "rejected", "pending"]
NextAction = Literal[
    "stay_in_current_context",
    "capture_intent",
    "clarify_with_user",
    "request_handoff",
]

# English task verbs. Conservative — adding to this set is a coordinate
# change between classifier behaviour and the existing
# ``hooks/worktree-auto-cut.sh`` English regex. Keep in sync with the
# Korean verb set below so neither language silently downgrades.
_TASK_VERBS_EN: FrozenSet[str] = frozenset(
    {
        "implement", "add", "build", "create", "fix", "refactor",
        "develop", "introduce", "write", "design",
        # "rename" / "delete" / "remove" / "update" / "change" are in
        # the verb+noun regex of the shell hook but not in the leading-
        # verb list. Adding them here so "rename function foo to bar"
        # is recognised as a task verb by the first pass too.
        "rename", "delete", "remove", "update", "change",
    }
)

# Korean task verbs. Matches the project's bilingual shell hook.
_TASK_VERBS_KO: FrozenSet[str] = frozenset(
    {"수정", "해결", "구현", "추가", "변경", "만들", "작업"}
)

# Code/repository nouns that, when paired with a task verb, signal a
# real implementation request. Mirrors the second pass in the shell
# hook so parity holds across clients.
_CODE_NOUNS_EN: FrozenSet[str] = frozenset(
    {
        "file", "function", "method", "class", "module", "hook", "skill",
        "test", "feature", "column", "field", "variable", "api",
        "endpoint", "route", "handler", "component", "import", "export",
        "line", "lines",
    }
)

# Repository/code domain nouns in Korean (a small set used by the shell
# hook regex). Mirrors the Korean noun pass so parity is preserved.
_CODE_NOUNS_KO: FrozenSet[str] = frozenset(
    {"hook", "브랜치", "worktree", "레포", "repo", "코드", "파일",
     "에러", "오류", "기능"}
)

# Phrases that, when the prompt starts with them, signal "I want to
# learn / look at / understand" — these classify as ``read_only`` even
# if they mention a code noun ("read me foo.py").
_READ_ONLY_LEAD_RE = re.compile(
    r"^\s*(?:what|why|how|where|when|who|which|explain|describe|tell me|show me|"
    r"read|review|investigate|examine|inspect|trace|debug|find out|figure out|"
    r"clarify|interpret|analyze|analyse|compare|walkthrough|walk through)\b",
    re.IGNORECASE,
)

# Phrases that, when the prompt starts with them, signal "draft a
# proposal / plan / design" without committing to build it.
_PROPOSAL_LEAD_RE = re.compile(
    r"^\s*(?:propose|draft|outline|plan|design|sketch|brainstorm|"
    r"explore|investigate options|think about|consider|recommend|"
    r"suggest|spec(?:ify)?|scope)\b",
    re.IGNORECASE,
)

# Boundary threshold for promotion from ``uncertain`` to
# ``implementation``. Below this, the classifier stays in
# ``uncertain`` even if the regex matched — the originator's
# acceptance gate is then the source of truth.
_IMPLEMENTATION_CONFIDENCE_THRESHOLD = 0.75


# ---- dataclasses ----


@dataclass(frozen=True)
class Classification:
    """The structured result of classifying one raw prompt.

    Fields are deliberately stable: the contract this module exposes
    to both Claude and Codex adapters. Schema additions are a
    coordinate change here + ``tests/test_request_intent.py`` (the
    golden corpus) + ``lib/intent_metrics.py`` (which reads
    ``mode`` / ``client`` / ``confidence``).
    """

    request_id: str
    client: Client
    mode: Mode
    worktree_required: bool
    confidence: float
    reason_codes: List[str]
    prompt_hash: str
    next_action: NextAction
    # Optional field: when the classifier detects an implementation
    # intent, ``slug_hint`` carries a best-effort ``<type>/<slug>``
    # suggestion (kebab-case, <=30 chars) so callers don't have to
    # re-derive. None when the mode is read_only / proposal /
    # uncertain without enough signal.
    slug_hint: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Intent:
    """The captured proto-spec written to ``.dev-kit/round-0/intent.md``.

    ``decision_mode`` is the only field a worker is allowed to
    flip after capture. ``goal``, ``acceptance_criteria`` and
    ``constraints`` are owned by the originator and must round-trip
    through any revision. ``decided_at`` / ``decided_by`` are
    populated when the originator accepts or rejects.
    """

    request_id: str
    client: Client
    decision_mode: DecisionMode
    goal: str
    acceptance_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    decided_at: Optional[str] = None  # ISO-8601 UTC, set on accept/reject
    decided_by: Optional[str] = None  # who flipped the decision

    def is_cut_eligible(self) -> bool:
        """A rejected or pending intent must never trigger a worktree cut.

        The hook gates ``cut_worktree`` on this method so a rejected
        intent cannot silently become an implementation worktree.
        """
        return self.decision_mode == "accepted"

    def to_dict(self) -> dict:
        return asdict(self)


# ---- classifier internals ----


def _hash_prompt(prompt: str) -> str:
    """Stable SHA-256 of ``prompt``, truncated to 16 hex chars.

    Used both for the ``prompt_hash`` field on ``Classification``
    (audit trail) and for stable ``request_id`` generation when the
    caller does not supply its own. A truncated SHA-256 is plenty
    for collision resistance within a single session.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _generate_request_id(prompt: str, client: Client) -> str:
    """Build a ``request_id`` of the form ``req-<16hex>``.

    Deterministic per ``(prompt, client)`` — same input always yields
    the same id. Callers wanting a fresh id per call can pass one in
    via ``classify_request(..., request_id=...)``.
    """
    seed = f"{client}:{prompt}"
    return f"req-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _has_task_verb_en(prompt_lower: str) -> bool:
    """True if a leading English task verb is present.

    Matches ``hooks/worktree-auto-cut.sh``'s first regex pass. A
    leading verb alone is not enough — the second pass (with a code
    noun) is what makes the request actionable.
    """
    m = re.match(r"^\s*([a-z][a-z'\-]+)", prompt_lower)
    if not m:
        return False
    return m.group(1) in _TASK_VERBS_EN


def _has_code_noun_en(prompt_lower: str) -> bool:
    """True if an English task verb + a code noun appear anywhere in
    the prompt (the safer-trigger pass from the shell hook)."""
    return bool(
        re.search(
            r"\b(" + "|".join(sorted(_TASK_VERBS_EN)) + r")\b"
            r"\s+(file|function|method|class|module|hook|skill|test|"
            r"feature|column|field|variable|api|endpoint|route|handler|"
            r"component|import|export|line|lines)\b",
            prompt_lower,
        )
    )


def _has_task_verb_and_code_noun_ko(prompt_lower: str) -> bool:
    """Korean equivalent of the verb+noun pass."""
    has_verb = any(verb in prompt_lower for verb in _TASK_VERBS_KO)
    has_noun = any(noun in prompt_lower for noun in _CODE_NOUNS_KO)
    if not (has_verb and has_noun):
        return False
    # Require the verb and noun to actually overlap (not just be
    # present in unrelated positions). The shell hook regex is
    # bidirectional; mirror that here.
    for verb in _TASK_VERBS_KO:
        for noun in _CODE_NOUNS_KO:
            if verb in prompt_lower and noun in prompt_lower:
                # Bounded by 200 chars — short-circuit on the first
                # match. The "bidirectional" check the shell hook does
                # would be expensive in pure Python without a regex
                # engine that supports variable-width lookbehind.
                return True
    return False


def _derive_slug_hint(prompt: str) -> Optional[str]:
    """Best-effort ``<type>/<slug>`` derivation.

    Mirrors the deterministic-stride of the shell hook's
    ``derive_slug``: take the first task verb, the first non-stop
    word after it, lowercase, kebab-case, cap at 30 chars. Returns
    ``None`` when no verb is found (read_only / proposal /
    uncertain-without-implementation-signal).

    Note: the shell hook has full type detection ("fix" / "feat" /
    "refactor"); here we always prefix ``fix/`` because the brief
    is explicit that ``cut_worktree``'s public signature is
    unchanged and the type-prefix mapping lives downstream. Callers
    that want the historical ``feat/`` / ``refactor/`` mapping can
    call ``lib/derive_branch.py`` once intent is accepted.
    """
    lower = prompt.lower()
    verb = None
    for v in _TASK_VERBS_EN:
        if re.search(rf"\b{v}\b", lower):
            verb = v
            break
    if verb is None:
        return None
    # Take everything after the first verb, strip punctuation, drop
    # stopwords, take first two tokens.
    after = re.sub(rf"^\s*{verb}\b", "", lower)
    after = re.sub(r"[^a-z0-9\s-]", " ", after)
    tokens = [t for t in after.split() if t and t not in _STOPWORDS_EN]
    noun = "-".join(tokens[:2])
    slug = f"fix/{verb}" if not noun else f"fix/{verb}-{noun}"
    slug = slug[:30].rstrip("-")
    if not re.fullmatch(r"fix/[a-z0-9-]{2,30}", slug):
        return None
    return slug


_STOPWORDS_EN: FrozenSet[str] = frozenset(
    {
        "a", "an", "the", "to", "for", "of", "in", "on", "at", "by",
        "with", "that", "this", "it", "its", "be", "is", "are", "was",
        "were", "i", "me", "my", "we", "our", "you", "your", "and",
        "or", "so", "but", "as", "do", "does", "did", "please",
        "should", "would", "could", "can", "will", "shall", "let's",
        "want", "need", "make", "made", "from", "into", "about",
        "over", "than", "then", "there", "here", "what", "when",
        "where", "why", "how", "who",
    }
)


def _score_implementation(prompt_lower: str) -> tuple:
    """Return ``(confidence, reason_codes)`` for implementation.

    Confidence ranges from 0.0 (no signal) to 1.0 (every gate
    matches). Each reason code contributes a bounded weight.
    Returns an empty list of reason codes when nothing fires, in
    which case the caller should not promote to that bucket.
    """
    reasons: List[str] = []
    score = 0.0

    if _has_task_verb_en(prompt_lower):
        score += 0.30
        reasons.append("task_verb_english")
    if _has_code_noun_en(prompt_lower):
        score += 0.50
        reasons.append("code_noun_english")
    if _has_task_verb_and_code_noun_ko(prompt_lower):
        score += 0.75
        reasons.append("task_verb_korean_with_code_noun")

    # Slash command prefix (Claude's ``/foo`` invocation) is a
    # strong signal — usually a skill invocation, which is
    # implementation-flavored.
    if prompt_lower.startswith("/"):
        score += 0.3
        reasons.append("slash_command")

    # A ``branch`` / ``worktree`` / ``commit`` / ``PR`` noun bumps
    # the score because it's literally what the hook guards.
    if re.search(
        r"\b(branch|worktree|commit|pr|pull request|merge|rebase)\b",
        prompt_lower,
    ):
        score += 0.1
        reasons.append("git_vocabulary")

    # Cap at 1.0 so confidence is a proper probability.
    return min(score, 1.0), reasons


# ---- public API ----


def classify_request(
    prompt: str,
    *,
    client: Client,
    request_id: Optional[str] = None,
    now: Optional[float] = None,
) -> Classification:
    """Classify a raw prompt into a structured ``Classification``.

    Parameters
    ----------
    prompt : str
        The raw prompt text the user submitted. May be empty (in
        which case the classifier returns ``mode=read_only`` with
        confidence 0 and reason ``empty_prompt``).
    client : Client
        ``"claude-code"`` or ``"codex"``. Recorded on the
        classification so callers can audit parity.
    request_id : str, optional
        Stable id for this classification. Default: deterministic
        hash of ``(client, prompt)`` so two adapters called with
        the same input produce the same id.
    now : float, optional
        Override for the wall-clock time. Tests pass this to keep
        the output deterministic; production calls let it default
        to ``time.time()``. (Currently unused but reserved so a
        future timestamp-based field does not break the contract.)

    Returns
    -------
    Classification
        Frozen record. ``mode`` is one of ``read_only`` /
        ``proposal`` / ``implementation`` / ``uncertain``. ``confidence``
        is in ``[0.0, 1.0]``. ``worktree_required`` mirrors the mode:
        True only for ``implementation`` above the threshold.
    """
    if client not in ("claude-code", "codex"):  # type: ignore[comparison-overlap]
        # Fail closed: an unknown client cannot be routed. The
        # orchestrator treats this as ``uncertain`` (do not cut).
        return Classification(
            request_id=request_id or _generate_request_id(prompt, "claude-code"),
            client="claude-code",  # type: ignore[arg-type]
            mode="uncertain",
            worktree_required=False,
            confidence=0.0,
            reason_codes=["unknown_client"],
            prompt_hash=_hash_prompt(prompt),
            next_action="clarify_with_user",
            slug_hint=None,
        )

    prompt_lower = (prompt or "").lower()
    if not prompt.strip():
        return Classification(
            request_id=request_id or _generate_request_id(prompt, client),
            client=client,
            mode="read_only",
            worktree_required=False,
            confidence=0.0,
            reason_codes=["empty_prompt"],
            prompt_hash=_hash_prompt(prompt),
            next_action="stay_in_current_context",
            slug_hint=None,
        )

    # Read-only leading phrases win early — even when the rest of
    # the prompt mentions code nouns ("explain how foo.py works").
    if _READ_ONLY_LEAD_RE.match(prompt):
        return Classification(
            request_id=request_id or _generate_request_id(prompt, client),
            client=client,
            mode="read_only",
            worktree_required=False,
            confidence=0.85,
            reason_codes=["read_only_lead"],
            prompt_hash=_hash_prompt(prompt),
            next_action="stay_in_current_context",
            slug_hint=None,
        )

    # Proposal / draft / design leads are non-committal by design —
    # the user is asking for a proposal, not an implementation.
    if _PROPOSAL_LEAD_RE.match(prompt):
        return Classification(
            request_id=request_id or _generate_request_id(prompt, client),
            client=client,
            mode="proposal",
            worktree_required=False,
            confidence=0.8,
            reason_codes=["proposal_lead"],
            prompt_hash=_hash_prompt(prompt),
            next_action="stay_in_current_context",
            slug_hint=None,
        )

    impl_score, impl_reasons = _score_implementation(prompt_lower)

    if impl_score >= _IMPLEMENTATION_CONFIDENCE_THRESHOLD:
        slug = _derive_slug_hint(prompt)
        return Classification(
            request_id=request_id or _generate_request_id(prompt, client),
            client=client,
            mode="implementation",
            worktree_required=True,
            confidence=impl_score,
            reason_codes=impl_reasons or ["implementation_score_below_threshold"],
            prompt_hash=_hash_prompt(prompt),
            next_action="capture_intent",
            slug_hint=slug,
        )

    # Below threshold. Could be: implementation-flavoured but too
    # vague, or genuinely a read-only question that did not match
    # the lead regex. Stay in ``uncertain`` — never silently bump
    # up to ``implementation``.
    if impl_score > 0.0:
        return Classification(
            request_id=request_id or _generate_request_id(prompt, client),
            client=client,
            mode="uncertain",
            worktree_required=False,
            confidence=impl_score,
            reason_codes=impl_reasons + ["below_implementation_threshold"],
            prompt_hash=_hash_prompt(prompt),
            next_action="clarify_with_user",
            slug_hint=None,
        )

    # No implementation signal at all — default to read_only.
    return Classification(
        request_id=request_id or _generate_request_id(prompt, client),
        client=client,
        mode="read_only",
        worktree_required=False,
        confidence=0.5,
        reason_codes=["no_implementation_signal"],
        prompt_hash=_hash_prompt(prompt),
        next_action="stay_in_current_context",
        slug_hint=None,
    )


# ---- intent.md schema ----


_INTENT_HEADER_RE = re.compile(r"^#\s+Intent:", re.MULTILINE)
_REQUEST_ID_RE = re.compile(r"^\s*request_id:\s*(\S+)\s*$", re.MULTILINE)
_CLIENT_RE = re.compile(r"^\s*client:\s*(\S+)\s*$", re.MULTILINE)
_DECISION_MODE_RE = re.compile(
    r"^\s*decision_mode:\s*(accepted|rejected|pending)\s*$", re.MULTILINE
)
_GOAL_RE = re.compile(r"^\s*goal:\s*(.+?)\s*$", re.MULTILINE)
_DECIDED_AT_RE = re.compile(r"^\s*decided_at:\s*(.+?)\s*$", re.MULTILINE)
_DECIDED_BY_RE = re.compile(r"^\s*decided_by:\s*(.+?)\s*$", re.MULTILINE)
_ACCEPTANCE_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)


def render_intent(intent: Intent) -> str:
    """Render an ``Intent`` to the canonical ``intent.md`` text shape.

    The text shape is what ``load_intent`` parses back. Sections
    are stable so a downstream worker can grep them. The shape is
    deliberately Markdown (not JSON) so a human can review the
    intent without a parser.
    """
    safe_goal = (intent.goal or "").replace("\n", " ").strip()
    lines = [
        f"# Intent: {safe_goal[:80] or 'untitled'}",
        "",
        f"request_id: {intent.request_id}",
        f"client: {intent.client}",
        f"decision_mode: {intent.decision_mode}",
        f"goal: {safe_goal}",
        "",
        "## Goal",
        intent.goal or "",
        "",
        "## Acceptance criteria",
    ]
    if intent.acceptance_criteria:
        for ac in intent.acceptance_criteria:
            lines.append(f"- {ac}")
    else:
        lines.append("- (none yet)")
    lines.extend([
        "",
        "## Constraints",
    ])
    if intent.constraints:
        for c in intent.constraints:
            lines.append(f"- {c}")
    else:
        lines.append("- (none)")
    lines.extend([
        "",
        "## Decision",
        f"decision_mode: {intent.decision_mode}",
    ])
    if intent.decided_at:
        lines.append(f"decided_at: {intent.decided_at}")
    if intent.decided_by:
        lines.append(f"decided_by: {intent.decided_by}")
    lines.append("")
    return "\n".join(lines)


def load_intent(path: Path) -> Optional[Intent]:
    """Parse ``.dev-kit/round-0/intent.md`` back into an ``Intent``.

    Returns ``None`` when the file does not exist, has no header,
    or fails the minimal validation. Validation is deliberately
    permissive (the originator can save partial drafts); the
    hard validation lives in ``validate_intent`` and is what the
    hook uses to decide whether to cut.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not _INTENT_HEADER_RE.search(text):
        return None

    request_id_m = _REQUEST_ID_RE.search(text)
    client_m = _CLIENT_RE.search(text)
    decision_m = _DECISION_MODE_RE.search(text)
    goal_m = _GOAL_RE.search(text)
    decided_at_m = _DECIDED_AT_RE.search(text)
    decided_by_m = _DECIDED_BY_RE.search(text)

    if not request_id_m or not client_m or not decision_m:
        return None

    # Acceptance criteria: the ``## Acceptance criteria`` block,
    # then every ``- <text>`` under it until the next ``##``.
    acceptance: List[str] = []
    in_acceptance = False
    constraints: List[str] = []
    in_constraints = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_acceptance = line.startswith("## Acceptance")
            in_constraints = line.startswith("## Constraints")
            continue
        if in_acceptance or in_constraints:
            m = _ACCEPTANCE_RE.match(line)
            if m:
                if in_acceptance:
                    acceptance.append(m.group(1))
                else:
                    constraints.append(m.group(1))

    return Intent(
        request_id=request_id_m.group(1),
        client=client_m.group(1),  # type: ignore[arg-type]
        decision_mode=decision_m.group(1),  # type: ignore[arg-type]
        goal=goal_m.group(1) if goal_m else "",
        acceptance_criteria=acceptance,
        constraints=constraints,
        decided_at=decided_at_m.group(1) if decided_at_m else None,
        decided_by=decided_by_m.group(1) if decided_by_m else None,
    )


def validate_intent(intent: Optional[Intent]) -> tuple:
    """Validate an ``Intent`` for cut eligibility.

    Returns ``(ok, reason)``. ``ok=True`` does not by itself mean
    the intent will be cut — the caller must also check
    ``intent.is_cut_eligible()`` (which gates on
    ``decision_mode == "accepted"``).

    Validation rules:

      * ``request_id`` must be non-empty and start with ``req-``.
      * ``client`` must be ``claude-code`` or ``codex``.
      * ``decision_mode`` must be one of the three valid literals.
      * ``goal`` must be non-empty (the originator stated an
        outcome — even a working-draft string is allowed, an
        empty one is not).
      * At least one acceptance criterion. A rejected intent is
        allowed to have none (the decision itself records the
        rejection reason).

    Returns ``(False, "missing_intent")`` when ``intent`` is None —
    i.e. the file does not exist or failed to parse.
    """
    if intent is None:
        return False, "missing_intent"
    if not intent.request_id or not intent.request_id.startswith("req-"):
        return False, "bad_request_id"
    if intent.client not in ("claude-code", "codex"):
        return False, "bad_client"
    if intent.decision_mode not in ("accepted", "rejected", "pending"):
        return False, "bad_decision_mode"
    if not intent.goal.strip():
        return False, "empty_goal"
    if intent.decision_mode != "rejected" and not intent.acceptance_criteria:
        return False, "missing_acceptance_criteria"
    return True, "ok"


__all__ = [
    "Classification",
    "Client",
    "DecisionMode",
    "Intent",
    "Mode",
    "NextAction",
    "classify_request",
    "load_intent",
    "render_intent",
    "validate_intent",
]
