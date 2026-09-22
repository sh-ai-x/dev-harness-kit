# Meta-harness fault triage

When a task fails, preserve the exact scenario and classify the failure before
changing product code.

```text
same scenario + same product snapshot
              ↓
replay current harness
              ↓
harness | adapter | policy | docs | product
              ↓
change the cheapest meta-layer first
              ↓
replay again → holdout → canary
```

## Classification rules

1. Missing checkpoint, incorrect Stop behavior, false completion, or missing
   evidence is a harness failure.
2. A provider-specific payload mismatch is an adapter failure; add a narrow
   adapter contract rather than a universal wrapper.
3. A forbidden action or threshold decision is a policy failure; safety and
   kernel rules cannot be self-promoted.
4. A stale or contradictory instruction is a documentation/fixture failure;
   update the source-of-truth document or golden case and replay.
5. Only after the same scenario passes the meta-layer replay and independent
   verification still identifies a product defect may product code change.

Telemetry is evidence, not semantic proof. Unknown provider/MCP coverage is
reported as unknown and never upgraded to verified by inference.
