from __future__ import annotations

import pytest

from lib.context_budget import (
    MAX_ARTIFACT_REF_CHARS,
    MAX_ARTIFACT_REFS,
    MAX_EXCERPT_CHARS,
    MAX_SUMMARY_CHARS,
    build_handoff,
    context_metrics,
    fingerprint,
    redact_excerpt,
    validate_handoff,
)


def test_handoff_prefers_artifact_refs_and_caps_payloads() -> None:
    payload = build_handoff(
        intent_ref="request-1",
        artifact_refs=[
            "artifact-long-" + "x" * MAX_ARTIFACT_REF_CHARS,
            *[f"artifact-{i}" for i in range(MAX_ARTIFACT_REFS + 3)],
        ],
        summary="x" * (MAX_SUMMARY_CHARS + 100),
    )
    assert payload["context_mode"] == "artifact_ref"
    assert len(payload["artifact_refs"]) == MAX_ARTIFACT_REFS
    assert len(payload["artifact_refs"][0]) == MAX_ARTIFACT_REF_CHARS
    assert all(len(ref) <= MAX_ARTIFACT_REF_CHARS for ref in payload["artifact_refs"])
    assert len(payload["summary"]) == MAX_SUMMARY_CHARS
    validate_handoff(payload)


def test_handoff_uses_summary_or_minimal_inline() -> None:
    assert build_handoff(intent_ref="a", summary="short")["context_mode"] == "summary_ref"
    assert build_handoff(intent_ref="a")["context_mode"] == "minimal_inline"


def test_validate_handoff_rejects_unbounded_or_inconsistent_refs() -> None:
    payload = build_handoff(intent_ref="a", artifact_refs=("artifact",))
    payload["artifact_refs"] = ["x" * (MAX_ARTIFACT_REF_CHARS + 1)]
    with pytest.raises(ValueError, match="artifact reference"):
        validate_handoff(payload)

    payload = build_handoff(intent_ref="a", artifact_refs=("artifact",))
    payload["context_ref_count"] = 0
    with pytest.raises(ValueError, match="context_ref_count"):
        validate_handoff(payload)


def test_handoff_rejects_transcript_fields() -> None:
    with pytest.raises(ValueError, match="transcripts"):
        validate_handoff(
            {
                "schema_version": 1,
                "context_mode": "minimal_inline",
                "artifact_refs": [],
                "summary": "",
                "transcripts": "do not persist",
            }
        )


def test_redaction_and_excerpt_are_bounded() -> None:
    value = "Bearer secret-token api_key=hidden sk-1234567890 " + "x" * MAX_EXCERPT_CHARS
    result = redact_excerpt(value)
    assert len(result) == MAX_EXCERPT_CHARS
    assert "secret-token" not in result
    assert "hidden" not in result
    assert "sk-1234567890" not in result
    assert result.endswith("...[truncated]")


def test_fingerprint_is_opaque_and_stable() -> None:
    assert fingerprint("same") == fingerprint("same")
    assert len(fingerprint("same")) == 16
    assert fingerprint("same") != fingerprint("other")


def test_token_counts_are_validated() -> None:
    with pytest.raises(ValueError):
        build_handoff(intent_ref="a", input_tokens=-1)
    with pytest.raises(ValueError):
        build_handoff(intent_ref="a", output_tokens=True)


def test_context_metrics_expose_coverage_and_do_not_score_missing_usage() -> None:
    metrics = context_metrics(
        [
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 900,
                "context_mode": "artifact_ref",
                "replay_tokens": 5,
            },
            {"context_mode": "minimal_inline"},
        ],
        expected=3,
    )
    assert metrics["usage_coverage"]["numerator"] == 1
    assert metrics["usage_coverage"]["denominator"] == 3
    assert metrics["usage_coverage"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert metrics["cache_hit_ratio"]["value"] == 0.9
    assert metrics["artifact_handoff_rate"]["value"] == 0.5
    assert metrics["full_context_reinjection_rate"]["value"] == 0.0


def test_context_metrics_empty_input_is_insufficient() -> None:
    metrics = context_metrics([])
    assert metrics["usage_coverage"]["value"] is None
    assert metrics["full_context_reinjection_rate"]["status"] == "INSUFFICIENT_EVIDENCE"
