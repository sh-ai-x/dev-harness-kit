---
name: build-simplify
category: build
description: 4-pass cleanup (dead → dup → naming → coverage). 회귀 테스트 없는 cleanup ❌ (MUST-L1 + L4).
when_to_use: |
  - User types "정리" / "리팩토링" / "슬롭 정리" / "단순화"
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: WebFetch Agent
model: sonnet
---

# build-simplify — 4-Pass Cleanup

## Iron Law
**회귀 테스트 없는 cleanup ❌.** 첫 pass = 데드 코드 제거 시 영향 받는 모든 테스트 통과 확인 후 다음 pass.

## 4 Passes (별개 호출)

```
[1/4] DEAD CODE   → unused exports / dead branches / commented-out blocks
       (Grep with permission to find all references first)
       ↓ regression test green
[2/4] DUPLICATION → 동일 로직 2+ 곳. extract helper / module
       ↓ regression test green
[3/4] NAMING      → 변수 / 함수 / 파일 / 모듈 이름 의미 명확하게
       ↓ regression test green
[4/4] COVERAGE    → 부족한 테스트 보강. hot path + edge cases
       ↓ full test suite green
```

## 규칙

- 4 pass를 한 cycle에 묶지 않는다 (MUST-NO-LOOP).
- 한 pass = 한 종류만. 완료 후 회귀 테스트 통과 확인.
- 추측 ❌. 측정 먼저 (e.g., `coverage report --include=src/lib`).

## Hook 정렬

Build stage 활성. cleanup 중 edit 시 `tdd-guard` hook은 test 변경 동반 시 통과 (rename 도움).

## Red Flags

| 생각 | 현실 |
|---|---|
| "4 pass 한 번에" | 회귀 실패 시 어느 pass 범인지 모름 |
| "테스트는 그대로 두고" | L1 위반 |
| "주석 처리로 disable" | L4 위반 |
| "나중에 검증" | L3 위반 |

## Hand-off

4 pass 완료 후 `state_codec.append_hand_off(root, "build", "review", "...")`. 다음 `/dev-kit:review` 호출.
