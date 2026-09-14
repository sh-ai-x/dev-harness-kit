---
type: llm
weight: 1
---

PASS if the final message contains a `## status` block with at least 3 of the rows: stage, cycles, drift, hand-off, eval-score. Also PASS if it contains a `no state found` line.
FAIL if the response is unrelated to status, refuses the request, or runs a different skill without producing any status info.
