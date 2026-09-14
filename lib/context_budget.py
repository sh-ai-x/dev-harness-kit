"""Bounded handoff and context-diet primitives.

This module is intentionally policy-light. Static hooks and the existing
Iron-Law documents remain authoritative for permission and verification;
these helpers only prevent oversized or transcript-shaped payloads from
crossing a skill boundary.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

CONTEXT_SCHEMA_VERSION = 1
MAX_EXCERPT_CHARS = 4 * 1024
MAX_SUMMARY_CHARS = 2 * 1024
MAX_EVENT_BYTES = 16 * 1024
MAX_ARTIFACT_REFS = 32
MAX_ARTIFACT_REF_CHARS = 512
MAX_PROGRESS_BYTES = 64 * 1024
CONTEXT_MODES = frozenset({"artifact_ref", "summary_ref", "minimal_inline"})
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:sk|gh[pousr]|xox[baprs])-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
)
_FORBIDDEN_KEYS = frozenset({"prompt", "prompts", "transcript", "transcripts", "messages"})


def redact_excerpt(value: Any, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Return a deterministic, secret-redacted bounded text excerpt."""
    text = "" if value is None else str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) <= limit:
        return text
    suffix = "...[truncated]"
    return text[: max(0, limit - len(suffix))] + suffix


def fingerprint(value: str) -> str:
    """Return opaque metadata suitable for joining context records."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _refs(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(redact_excerpt(value, limit=MAX_ARTIFACT_REF_CHARS))
        if len(result) >= MAX_ARTIFACT_REFS:
            break
    return result


def build_handoff(
    *,
    intent_ref: str,
    checkpoint: str = "",
    failure_signature: str = "",
    artifact_refs: Iterable[Any] = (),
    summary: str = "",
    context_mode: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    replay_tokens: int | None = None,
    compaction_observed: bool = False,
) -> dict[str, Any]:
    """Build the only payload shape allowed across skill handoffs.

    Full prompts/transcripts are rejected instead of silently truncating them;
    callers must pass an artifact reference or a compact summary.
    """
    if not isinstance(intent_ref, str) or not intent_ref.strip():
        raise ValueError("intent_ref is required")
    refs = _refs(artifact_refs)
    bounded_summary = redact_excerpt(summary, limit=MAX_SUMMARY_CHARS)
    if context_mode is None:
        context_mode = "artifact_ref" if refs else (
            "summary_ref" if bounded_summary else "minimal_inline"
        )
    if context_mode not in CONTEXT_MODES:
        raise ValueError(f"unsupported context_mode: {context_mode!r}")
    bounded_checkpoint = redact_excerpt(checkpoint, limit=MAX_SUMMARY_CHARS)
    bounded_failure = redact_excerpt(failure_signature, limit=MAX_SUMMARY_CHARS)
    payload: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "intent_ref": intent_ref,
        "checkpoint": bounded_checkpoint,
        "failure_signature": bounded_failure,
        "artifact_refs": refs,
        "summary": bounded_summary,
        "context_mode": context_mode,
        "context_fingerprint": fingerprint(
            "|".join([intent_ref, bounded_checkpoint, bounded_failure, *refs, bounded_summary])
        ),
        "context_ref_count": len(refs),
        "replay_tokens": _nonnegative_int(replay_tokens),
        "compaction_observed": bool(compaction_observed),
    }
    if input_tokens is not None:
        payload["input_tokens"] = _nonnegative_int(input_tokens)
    if output_tokens is not None:
        payload["output_tokens"] = _nonnegative_int(output_tokens)
    _validate_no_transcript(payload)
    _validate_size(payload)
    return payload


def validate_handoff(payload: dict[str, Any]) -> None:
    """Validate a handoff created by :func:`build_handoff`."""
    if not isinstance(payload, dict):
        raise ValueError("handoff must be an object")
    if payload.get("schema_version") != CONTEXT_SCHEMA_VERSION:
        raise ValueError("unsupported handoff schema_version")
    _validate_no_transcript(payload)
    required = {
        "intent_ref", "checkpoint", "failure_signature", "artifact_refs",
        "summary", "context_mode", "context_fingerprint", "context_ref_count",
        "replay_tokens", "compaction_observed",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError("handoff missing required fields: " + ", ".join(missing))
    if not isinstance(payload["intent_ref"], str) or not payload["intent_ref"].strip():
        raise ValueError("handoff intent_ref is required")
    for name, limit in (
        ("checkpoint", MAX_SUMMARY_CHARS),
        ("failure_signature", MAX_SUMMARY_CHARS),
        ("summary", MAX_SUMMARY_CHARS),
    ):
        if not isinstance(payload[name], str) or len(payload[name]) > limit:
            raise ValueError(f"handoff {name} exceeds the context cap")
    mode = payload.get("context_mode")
    if mode not in CONTEXT_MODES:
        raise ValueError("handoff context_mode is invalid")
    refs = payload.get("artifact_refs")
    if not isinstance(refs, list) or len(refs) > MAX_ARTIFACT_REFS:
        raise ValueError("handoff artifact_refs exceed the context cap")
    if any(not isinstance(ref, str) or not ref or len(ref) > MAX_ARTIFACT_REF_CHARS for ref in refs):
        raise ValueError("handoff artifact reference exceeds the context cap")
    if payload.get("context_ref_count") != len(refs):
        raise ValueError("handoff context_ref_count does not match artifact_refs")
    _nonnegative_int(payload.get("replay_tokens"))
    for name in ("input_tokens", "output_tokens"):
        if name in payload:
            _nonnegative_int(payload[name])
    if not isinstance(payload["compaction_observed"], bool):
        raise ValueError("handoff compaction_observed must be boolean")
    _validate_size(payload)


def context_metrics(records: Iterable[dict[str, Any]], *, expected: int | None = None) -> dict[str, dict[str, Any]]:
    """Project bounded context telemetry into honest ratio metrics.

    Each result exposes numerator/denominator/coverage and status. A metric
    with no eligible records is explicitly insufficient rather than zero.
    """
    rows = list(records)
    expected_count = len(rows) if expected is None else max(0, expected)

    def ratio(
        name: str,
        numerator: int,
        denominator: int,
        *,
        observed: int | None = None,
    ) -> dict[str, Any]:
        coverage = _coverage(
            len(rows) if observed is None else observed,
            expected_count,
        )
        if denominator == 0:
            return {
                "name": name,
                "value": None,
                "numerator": numerator,
                "denominator": denominator,
                "coverage": coverage,
                "status": INSUFFICIENT_EVIDENCE,
            }
        return {
            "name": name,
            "value": numerator / denominator,
            "numerator": numerator,
            "denominator": denominator,
            "coverage": coverage,
            "status": "OK" if coverage >= 1.0 else INSUFFICIENT_EVIDENCE,
        }

    usage = [
        row for row in rows
        if isinstance(row.get("input_tokens"), int)
        and isinstance(row.get("output_tokens"), int)
        and row.get("input_tokens", -1) >= 0
        and row.get("output_tokens", -1) >= 0
    ]
    usage_input = sum(row["input_tokens"] for row in usage)
    cache_read = sum(_safe_int(row.get("cache_read_tokens")) for row in usage)
    total_context = usage_input + cache_read
    handoffs = [row for row in rows if row.get("context_mode") in CONTEXT_MODES]
    artifact_handoffs = [
        row for row in handoffs if row.get("context_mode") in {"artifact_ref", "summary_ref"}
    ]
    full_reinjections = [
        row for row in rows
        if row.get("context_mode") in {"full", "transcript", "prompt"}
        or any(key in row for key in _FORBIDDEN_KEYS)
    ]
    replay = sum(_safe_int(row.get("replay_tokens")) for row in usage)
    payload_violations = [row for row in rows if _safe_int(row.get("payload_bytes")) > MAX_EVENT_BYTES]
    return {
        "usage_coverage": ratio(
            "usage_coverage", len(usage), expected_count, observed=len(usage)
        ),
        "cache_hit_ratio": ratio(
            "cache_hit_ratio", cache_read, total_context, observed=len(usage)
        ),
        "artifact_handoff_rate": ratio(
            "artifact_handoff_rate", len(artifact_handoffs), len(handoffs), observed=len(handoffs)
        ),
        "full_context_reinjection_rate": ratio(
            "full_context_reinjection_rate", len(full_reinjections), len(rows)
        ),
        "resume_replay_ratio": ratio("resume_replay_ratio", replay, sum(
            _safe_int(row.get("input_tokens")) for row in usage
        ), observed=len(usage)),
        "context_payload_cap_violation_rate": ratio(
            "context_payload_cap_violation_rate", len(payload_violations), len(rows)
        ),
    }


def _validate_no_transcript(payload: dict[str, Any]) -> None:
    forbidden = sorted(_forbidden_paths(payload))
    if forbidden:
        raise ValueError(
            "full prompts/transcripts are not valid handoff fields: "
            + ", ".join(forbidden)
        )


def _forbidden_paths(value: Any, prefix: str = "") -> list[str]:
    """Return every forbidden prompt/transcript key at any JSON depth."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            if name.casefold() in _FORBIDDEN_KEYS:
                found.append(path)
            found.extend(_forbidden_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{prefix}[{index}]"))
    return found


def _nonnegative_int(value: int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("token counts must be non-negative integers")
    return value


def _validate_size(payload: dict[str, Any]) -> None:
    encoded = str(payload).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("handoff exceeds MAX_EVENT_BYTES")


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _coverage(observed: int, expected: int) -> float | None:
    if expected <= 0:
        return None
    return observed / expected
