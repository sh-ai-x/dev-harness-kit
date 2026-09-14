---
type: llm
weight: 1
---

PASS if the final message contains a clear PASS or FAIL verdict line for a CI readiness audit (e.g., starts with "PASS" or "FAIL", contains a "Verdict:" line, or has explicit per-row PASS/FAIL markers).
FAIL if the response has no such verdict, refuses to run anything, or runs a different skill.
