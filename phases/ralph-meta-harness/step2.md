# Step 2 — Completion proof

Extend Ralph promotion with a deterministic completion receipt. The receipt
must identify the product snapshot, harness candidate, checks, evidence paths,
hashes, verifier provenance, and unresolved-risk status. Existing promotion
must stay idempotent and reject incomplete source bundles.

Verification:

- `pytest -q tests/test_ralph_promote.py`
- Receipt is stable across a repeated promotion of the same bundle.
