# Step 3 — Offline replay contract

Add the smallest reusable candidate manifest and replay/holdout result
contract to the existing meta-evaluation path. It must run only when invoked
explicitly by evaluation/proposal tooling, never from a hot-path hook, and must
reject candidates that violate hard gates.

Verification:

- `pytest -q tests/test_meta_eval.py tests/test_meta_harness_loop.py tests/test_meta_harness_metrics.py`
- active RALPH execution is unaffected by an offline candidate failure.
