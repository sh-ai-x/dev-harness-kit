# Step 1 — Thin continuity

Implement the file-backed RALPH checkpoint adapter and make Stop/SessionEnd
distinguish worker session closure from workflow completion. Preserve the
existing non-RALPH measurement contract and emergency safety paths.

Verification:

- `pytest -q tests/test_ralph_controller.py tests/test_trace_session_end.py tests/test_trace_session_end_hook.py`
- RALPH state remains resumable from `.dev-kit/ralph/<session>.json`.
