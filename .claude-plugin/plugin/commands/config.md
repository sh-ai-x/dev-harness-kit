---
name: config
description: skill + MCP + hook + methodology picker (multiSelect).
when_to_use: |
  - User types /dev-kit:config
allowed-tools: Read Write Edit
model: haiku
---

# /dev-kit:config — Inter-Skill Selector

multiSelect 4 질문:
1. Skills — 어떤 스킬 활성/비활성 (default 전부 ON)
2. MCP — 어떤 MCP enable (default 전부 OFF)
3. Hook matrix — stage별 hook 활성 (default 매트릭스)
4. Methodology — TDD/SDD/DDD/BDD/FDD (default TDD)

결과 → `.dev-kit/.enabled.json` + `.dev-kit/methodology.json` 갱신.
