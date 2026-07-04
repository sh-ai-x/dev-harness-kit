---
name: build-harness-engine
category: design
description: phase step files 생성. plan-ralph의 6단계 출력으로 phases/<name>/{index.json, step<N>.md} 자동 합성. plan-ralph가 dispatch.
when_to_use: |
  - Auto-invoked by plan-ralph after gate-pass
  - User triggers manual regeneration via `@dev-kit:plan`
allowed-tools: Read Write
disallowed-tools: Bash WebFetch Agent
model: sonnet
---

# build-harness-engine — Phase Decomposition (Plan+Design subgraph)

## Core Goal
PRD.md + non-goals를 받아 `phases/<name>/{index.json, step<N>.md}` 자동 생성.

## 출력

```
phases/<phase-alias>/
├── index.json          # step state machine
├── step0.md            # Phase 1 step (e.g., setup)
├── step1.md
└── stepN-output.json   # after execution
```

각 step file 형식:

```markdown
# Step N: <title>

## Must-read
- docs/ARCHITECTURE.md §<N>
- ../../CLAUDE.md §<N>

## Instruction (signature-level)
function createX(input: Type) -> Result
function validateX(input: Type) -> Result

## Acceptance Criteria (runnable)
\`\`\`bash
npm test -- --testNamePattern="createX"
\`\`\`
expected exit code 0, count 5+

## Don't do X because Y
- ❌ Don't use mock — production behavior required
- ❌ Don't skip tests — Iron Law L1
```

## 규칙

- 한 step = 한 layer / 한 module (harness-runner의 step 별개 사이클)
- must-read / AC / Don't do X because Y 필수 3섹션
- function signature-level instruction (body ❌)

## Hook 정렬

`stop-verify=ON` 만. 그 외 OFF (Plan 단계와 동일).
