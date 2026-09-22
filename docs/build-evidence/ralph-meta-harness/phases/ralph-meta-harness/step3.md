# Step 3 — Offline replay contract

Added explicit candidate replay/holdout evaluation and pure meta-harness
metrics. Evaluation is offline and opt-in; it cannot block a live RALPH
worker or Stop hook.

Verification command:

`PYTHONPATH=. pytest -q tests/test_meta_eval.py tests/test_meta_harness_loop.py tests/test_meta_harness_metrics.py`
