---
type: llm
weight: 1
---

PASS if the final message invokes the refactor chain (inspect -> build-refactor -> review) or shows a phased refactor plan with quoted exit codes between phases.
FAIL if the response refuses, runs a different skill, or produces a single-pass "fix everything" outline with no phasing.
