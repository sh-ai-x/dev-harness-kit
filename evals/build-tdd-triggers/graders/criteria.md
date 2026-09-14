---
type: llm
weight: 1
---

PASS if the final message invokes the build-tdd chain (Red-Green-Refactor) or states that production code will not be written until a failing test exists.
FAIL if the response writes production code first, refuses, or runs a different skill.
