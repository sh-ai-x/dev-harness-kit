#!/usr/bin/env python3
"""interview_engine.py — Phase 6 5-field safety contract core.

Drives the user-facing interview skill. Two responsibilities:

1. Conversational state machine:
   - ``next_question(answers, asked)``: pick the next question to ask.
   - ``apply_answer(answers, qid, value)``: merge an answer + status
     transition (ok | held | best-effort | user-acknowledged | rejected).
   - ``should_terminate(status, cycle)``: gate the loop on the 5-field
     contract + safety_valve cap.

2. Five-field safety contract (one of each):
   - ``goal``              — one sentence describing the outcome.
   - ``constraints``       — explicit guardrails / non-negotiables.
   - ``success_criteria``  — measurable pass conditions.
   - ``anti_goals``        — what we will NOT do (negative spec).
   - ``acceptance_rubric`` — how a reviewer scores "done".

Each field maps to one axis in the ``interview_ambiguity`` judge
rubric (see ``lib/llm_judge.py:DIM_AXES``).

Loop semantics
--------------
- ``safety_valve=8`` cycle cap.
- ``narrowed_delta``: score MUST decrease each iteration; equality
  fires ``dedup_metric: identical-ambiguity-cycle=2``.
- ``dedup_metric``: two cycles with the same ambiguity_score breaks
  the loop and surfaces "best-effort" to the user.
- ``user_interrupt``: any user interrupt (empty answer / "stop") moves
  the status to ``user-acknowledged`` and freezes the contract.

JSON shape
----------
``score_interview_ambiguity(answers)`` returns
``{value_score, ambiguity_score, evidence_count, status}`` — matches
the ``.dev-kit/hand-off/<step>.md`` 5-field frontmatter contract that
``/dev-kit:plan`` reads before emitting a PRD.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# The 5 mandated fields. Order is the order the skill prompts them in.
FIVE_FIELDS: Tuple[str, ...] = (
    "goal",
    "constraints",
    "success_criteria",
    "anti_goals",
    "acceptance_rubric",
)

# Cycle cap (issue #381 — safety_valve=8).
DEFAULT_SAFETY_VALVE: int = 8

# Per-field ambiguity score when the answer is missing (worst case).
MISSING_FIELD_SCORE: int = 10

# Per-field ambiguity score when the answer is "ok" (clarity floor).
CLEAR_FIELD_SCORE: int = 2

# Threshold below which a single field counts as "clear enough".
FIELD_CLEAR_THRESHOLD: int = 4

# Allowed status values. ``user-acknowledged`` is a user-driven halt;
# ``best-effort`` is a dedup_metric break; ``held`` is the safety_valve
# cap; ``rejected`` is an explicit "don't proceed".
STATUSES: Tuple[str, ...] = (
    "ok",
    "held",
    "best-effort",
    "user-acknowledged",
    "rejected",
)

# Question → 5-field mapping. The skill iterates through
# QUESTION_PLAN, applies each question's answer to the field it maps
# to, and re-scores ambiguity per field. The plan order matches
# FIVE_FIELDS so the interviewer does not skip around.
QUESTION_PLAN: Tuple[Tuple[str, str], ...] = (
    ("q_goal",              "goal"),
    ("q_constraints",       "constraints"),
    ("q_success_criteria",  "success_criteria"),
    ("q_anti_goals",        "anti_goals"),
    ("q_acceptance_rubric", "acceptance_rubric"),
)

# Heuristic ambiguity scoring per field. Deterministic — no LLM call.
# A real eval run re-scores via the ``interview_ambiguity`` LLM judge
# (issue #383). These scores are the local fallback the skill uses
# between turns.
_AMBIGUITY_PHRASES = (
    "maybe", "kind of", "tbd", "todo", "later", "not sure",
    "i think", "probably", "somehow", "etc", "stuff", "things",
)
_AMBIGUITY_TOO_SHORT = 12  # chars; under this = trivial answer


def _is_ambiguous_text(text: str) -> bool:
    """Heuristic: is the user's answer too thin to be unambiguous?

    Returns True if the answer is empty, too short, or matches one of
    the soft-language phrases above. Deterministic — no LLM call.
    """
    s = (text or "").strip().lower()
    if not s:
        return True
    if len(s) < _AMBIGUITY_TOO_SHORT:
        return True
    return any(p in s for p in _AMBIGUITY_PHRASES)


def validate_5_field(answers: Dict[str, str]) -> Dict:
    """Check that all 5 fields are present and unambiguous.

    Returns:
        {
            "valid": bool,                # all 5 fields present + clear
            "missing": list[str],         # field names absent / empty
            "ambiguous": list[str],       # field names with weak answers
        }

    A field is *missing* when the key is absent OR the value is empty
    after stripping. A field is *ambiguous* when present but fails the
    heuristic (`_is_ambiguous_text`).
    """
    missing: List[str] = []
    ambiguous: List[str] = []
    for field in FIVE_FIELDS:
        value = answers.get(field, "")
        if not (value or "").strip():
            missing.append(field)
            continue
        if _is_ambiguous_text(value):
            ambiguous.append(field)
    return {
        "valid": not missing and not ambiguous,
        "missing": missing,
        "ambiguous": ambiguous,
    }


def extract_5_field(conversation: List[Dict]) -> Dict:
    """Extract the 5 fields from a conversation transcript.

    Each conversation entry is ``{"role": "assistant"|"user",
    "content": str}``. The latest user answer for each field (matched
    by the question id in the preceding assistant turn) wins.

    Returns a dict with the 5 field keys; absent fields are empty
    strings. Callers should pair this with ``validate_5_field`` to
    surface missing / ambiguous fields.

    Question-id detection accepts both underscore (``q_anti_goals``)
    and hyphen (``q_anti-goals``) forms: hyphens and underscores are
    normalized to underscores before matching, so the rubric label
    ``anti-goals`` and the programmatic id ``q_anti_goals`` are
    interchangeable. The token-prefix check still bounds matches so
    ``"goal"`` does not falsely match inside ``"anti-goals"``.
    """
    import re
    out: Dict[str, str] = {field: "" for field in FIVE_FIELDS}
    pending_field: Optional[str] = None
    for entry in conversation:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = (entry.get("content") or "").strip()
        if role == "assistant" and content:
            # Normalize hyphens to underscores so the rubric's
            # human-facing "anti-goals" form matches the
            # programmatic q_anti_goals id.
            normalized = re.sub(r"[-_]", "_", content)
            for qid, field in QUESTION_PLAN:
                if re.search(r"(?:^|\s|\W)" + re.escape(qid) + r"\b", normalized):
                    pending_field = field
                    break
                if re.search(r"\b" + re.escape(field) + r"\b", normalized):
                    pending_field = field
                    break
        elif role == "user" and pending_field and content:
            out[pending_field] = content
            pending_field = None
    return out


def _score_field(value: str) -> int:
    """Per-field ambiguity score, 0..10. Lower = clearer."""
    if not (value or "").strip():
        return MISSING_FIELD_SCORE
    if _is_ambiguous_text(value):
        return 7  # present but weak
    return CLEAR_FIELD_SCORE


def _aggregate(per_field: Dict[str, int]) -> int:
    """Mean of per-field ambiguity scores, rounded to int."""
    if not per_field:
        return MISSING_FIELD_SCORE
    return round(sum(per_field.values()) / len(per_field))


def score_interview_ambiguity(answers: Dict[str, str]) -> Dict:
    """Score a completed set of interview answers.

    Returns the canonical 4-key hand-off shape written to
    ``.dev-kit/hand-off/<step>.md`` and read by ``/dev-kit:plan``:
        {
            "value_score":     float,  # fraction of fields clear
            "ambiguity_score": int,    # mean of per-field ambiguity
            "evidence_count":  int,    # fields that pass the heuristic
            "status":          str,    # ok | held | best-effort | user-acknowledged | rejected
        }

    Status decision matrix (MUST-15 plan pattern):
      - ``ok``             iff validate_5_field(valid=True)
      - ``held``           iff any field is missing (zero-evidence fields fail-closed)
      - ``best-effort``    iff >= 3 fields present + clear, but at least one is ambiguous
      - ``user-acknowledged`` / ``rejected`` are reserved for the conversational
        state machine (apply_answer / user_interrupt) — never set here.
    """
    per_field = {field: _score_field(answers.get(field, "")) for field in FIVE_FIELDS}
    ambiguity = _aggregate(per_field)
    clear = sum(1 for v in per_field.values() if v < FIELD_CLEAR_THRESHOLD)
    validate = validate_5_field(answers)
    value = round(clear / len(FIVE_FIELDS), 2) if FIVE_FIELDS else 0.0
    if validate["valid"]:
        status = "ok"
    elif validate["missing"]:
        status = "held"
    elif clear >= 3:
        status = "best-effort"
    else:
        status = "held"
    return {
        "value_score": value,
        "ambiguity_score": ambiguity,
        "evidence_count": clear,
        "status": status,
    }


# ----- conversational state machine -----


def next_question(answers: Dict[str, str], asked: List[str]) -> Optional[str]:
    """Pick the next unanswered question id, or None when all 5 are done.

    Iterates ``QUESTION_PLAN`` in order; skips any question whose id is
    already in ``asked``. Returns the question id of the next field
    that is missing OR ambiguous, so the skill can re-ask weak answers.
    """
    validate = validate_5_field(answers)
    weak = set(validate["missing"]) | set(validate["ambiguous"])
    for qid, field in QUESTION_PLAN:
        if qid in asked and field not in weak:
            continue
        return qid
    return None


def apply_answer(
    answers: Dict[str, str],
    qid: str,
    value: str,
) -> Dict[str, str]:
    """Merge a user answer into the answers dict.

    Maps the question id to its 5-field, strips the value, and stores
    it. Empty / whitespace-only answers are skipped so the caller can
    detect "user declined to answer" by absence rather than by an
    empty string. Returns the updated dict (mutated in place for
    convenience; same object identity).
    """
    field = dict(QUESTION_PLAN).get(qid)
    if field is None:
        return answers
    stripped = (value or "").strip()
    if not stripped:
        return answers
    answers[field] = stripped
    return answers


def should_terminate(
    status: str,
    cycle: int,
    *,
    safety_valve: int = DEFAULT_SAFETY_VALVE,
) -> bool:
    """Decide whether the conversational loop should exit.

    Returns True iff:
      - status is one of ``ok | held | best-effort | user-acknowledged
        | rejected`` (a terminal state), OR
      - cycle has reached ``safety_valve`` (fail-closed cap).

    The ``safety_valve`` default is 8 (Phase 6 contract).
    """
    if status in STATUSES and status != "ok" and cycle > 0:
        return True
    if cycle >= safety_valve:
        return True
    return False


def is_narrowing(prev: float, cur: float) -> bool:
    """Return True iff ``cur < prev`` (strict narrowing).

    Used to enforce the ``narrowed_delta`` contract: each loop
    iteration's ambiguity_score must strictly decrease. Equality does
    NOT narrow; the dedup_metric breaker fires on the second equal
    cycle.

    The function is named ``is_narrowing`` (a boolean predicate). The
    legacy name ``narrowed_delta`` is kept as an alias for callers
    that imported the older, misleadingly-quantitative name.
    """
    return cur < prev


# Backwards-compatible alias. Prefer the boolean-predicate name
# ``is_narrowing`` for new code; ``narrowed_delta`` remains for any
# external callers (and the skill frontmatter contract keyword).
narrowed_delta = is_narrowing


def dedup_metric(history: List[float]) -> bool:
    """Return True iff the last two history entries are equal.

    The ``dedup_metric: identical-ambiguity-cycle=2`` breaker fires
    when two cycles in a row produce the same ambiguity score,
    regardless of absolute value. The skill then surfaces
    ``status="best-effort"`` to the user.
    """
    if len(history) < 2:
        return False
    return history[-1] == history[-2]


def user_interrupt(answers: Dict[str, str], qid: str, value: str) -> bool:
    """Return True iff the user signaled an interrupt on this turn.

    Recognized interrupt tokens (case-insensitive, exact match after
    strip): ``"stop"``, ``"cancel"``, ``"skip"``, ``"abort"``,
    ``"later"``.

    ``answers`` and ``qid`` are accepted for forward-compat with a
    planned "empty-answer-after-re-ask counts as interrupt" branch.
    Today the predicate is token-only; the re-ask heuristic is left
    to the conversational state machine (``apply_answer`` already drops
    whitespace-only values) so a single source of truth governs
    "did the user decline to answer".
    """
    INTERRUPT_TOKENS = {"stop", "cancel", "skip", "abort", "later"}
    s = (value or "").strip().lower()
    return s in INTERRUPT_TOKENS


__all__ = [
    "FIVE_FIELDS",
    "QUESTION_PLAN",
    "STATUSES",
    "DEFAULT_SAFETY_VALVE",
    "validate_5_field",
    "extract_5_field",
    "score_interview_ambiguity",
    "next_question",
    "apply_answer",
    "should_terminate",
    "narrowed_delta",
    "dedup_metric",
    "user_interrupt",
]
