---
name: shortcut-tdd-fast
category: shortcuts
description: Bootstrap+Plan 우회 → Build 직행. hand-off stub 마킹. 긴급 핫픽스용.
when_to_use: |
  - User types /dev-kit:tdd-fast
  - 긴급 핫픽스 (hotfix)
allowed-tools: Read Write Bash
disallowed-tools: WebFetch Agent
model: sonnet
---

# shortcut-tdd-fast — 우회 Build 직행

## Iron Law
**사용자가 명시 우회 의도를 표현했을 때만.** urgent hotfix / prototype 일 때.

## 동작

1. Plan/Design hand-off stub 자동 마킹 (`.dev-kit/hand-off/plan→build.md` 빈 파일)
2. `.dev-kit/state.json` `shortcut_used: "tdd-fast"` 기록
3. 즉시 Build 호출 (harness-runner engine)
4. Review/Security stage는 후속 호출에서

## 규칙

- Plan 단계의 6 gates 자동 skip (사용자 명시 OK만)
- `/dev-kit:plan` 후속 호출 시 정상 흐름 복귀
- 사용자 코드 자동 변경 ❌ (TDD 사이클은 유지)

## 훅 정렬

Build stage와 동일.

## 후속 hand-off

`build→review.md` (full chain 정상) + 별도 `plan→build.md` stub 으로 audit 가능.
