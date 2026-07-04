---
name: shortcut-tdd-fast
description: 우회 Build 직행. Bootstrap+Plan 우회.
when_to_use: |
  - User types /dev-kit:tdd-fast (긴급 핫픽스)
allowed-tools: Read Write Bash
model: sonnet
---

# /dev-kit:tdd-fast — 우회 Build 직행

## 동작

1. Plan/Design hand-off stub 마킹 (`.dev-kit/hand-off/plan→build.md` 비어있음).
2. `.dev-kit/state.json` `shortcut_used: "tdd-fast"` 기록.
3. 즉시 Build 호출 (harness-runner).

## 규칙

- 사용자 명시 우회 의도만. TDD 사이클은 유지.
- 후속 `/dev-kit:plan` 호출 시 정상 흐름 복귀 (Phase 자동 skip).
