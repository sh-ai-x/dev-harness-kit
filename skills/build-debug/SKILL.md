---
name: build-debug
category: build
description: 4-phase systematic debugging. Phase 1 reproduce 완료 전 fix 제안 ❌ (MUST-L2). root-cause-first Iron Law.
when_to_use: |
  - User types "버그" / "안 돼" / "왜 실패" / "에러 발생"
allowed-tools: Read Bash
disallowed-tools: Edit Write WebFetch
model: opus
---

# build-debug — Systematic Debugging (4 Phase)

## Iron Law
**Phase 1 (재현) 완료 전 fix 제안 ❌.**

## 4 Phases (별개 사이클로 호출)

```
[1/4] REPRODUCE  → failing case 1개 이상 재현
       (재현 실패 시 단계 진행 ❌)
       ↓
[2/4] ISOLATE    → minimal reproduction
       (input 줄이기 / 외부 의존 차단)
       ↓
[3/4] ROOT CAUSE → 구체적 라인 + 호출 stack 인용
       (도구: git blame, git bisect, log, debugger)
       ↓
[4/4] FIX        → 회귀 테스트 동반
       (Iron Law L1: 테스트 없는 fix ❌)
```

## 규칙 (예외 없음)

- 4 phase를 한 cycle에 묶지 않는다 (MUST-NO-LOOP).
- phase마다 사용자 확인. 또는 4 phase 분리 호출.
- root cause 인용 없이 "Probably X" 단정 ❌.
- 변경은 한 번에 하나. 여러 변경 동시에 ❌.

## Hook 정렬

Build stage에서 `tdd-guard` ON. debug 중 fix 작성 시 회귀 테스트 동반 강제.

## Hand-off

4 phase 완료 후:
- root cause 1줄 + 회귀 테스트 GREEN 보고
- `state_codec.append_hand_off(root, "build", "build", "..")`
- 다음 step으로 회귀 (build-engine 재호출)

## Red Flags

| 생각 | 현실 |
|---|---|
| "Probably X" | reproduce 못 했으면 모름 |
| "Just patch it" | root cause 무시 → 같은 버그 반복 |
| "여러 변경 한꺼번에" | 어느 변경이 fix였는지 모름 |
| "Skip reproduce" | L2 위반 |
