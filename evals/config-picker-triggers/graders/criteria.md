---
type: llm
weight: 1
---

PASS if the final message asks a multiSelect question OR lists 3 categories (skills, hooks, methodology) for the user to pick from.
FAIL if the response refuses, runs a different skill, or produces an unrelated answer with no picker-shape.
