# Step 2 — Completion proof

Added a deterministic completion receipt to Ralph promotion. The receipt
contains the candidate id, artifact hash, evidence references, verifier
provenance, acceptance checks, and unresolved-risk status.

Verification command:

`PYTHONPATH=. pytest -q tests/test_ralph_promote.py`
