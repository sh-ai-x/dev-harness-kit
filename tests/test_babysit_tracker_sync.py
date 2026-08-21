from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from babysit_tracker_sync import format_transition, transition_marker  # noqa: E402


def test_transition_body_is_stable_and_restart_deduplicable() -> None:
    body = format_transition(
        key="695:abc:0:wait_for_approval",
        pr_number=695,
        phase="wait_for_approval",
        strategy="continue",
        head_sha="abc",
        context_epoch=0,
        review_verdict="REVIEW_REQUIRED",
        checks_summary="12/12 green",
    )
    assert body.startswith(transition_marker("695:abc:0:wait_for_approval"))
    assert "WAIT_FOR_APPROVAL" not in body
    assert "Resume: re-run" in body
