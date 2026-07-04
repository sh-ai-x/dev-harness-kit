---
name: shortcut-quick-fix
category: shortcuts
description: verify+debug만 즉시 호출. 코드 작성 없음. 빌드 결과 검증 / 디버그 단계 빠르게.
when_to_use: |
  - User types /dev-kit:quick-fix
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch
model: sonnet
---

# shortcut-quick-fix — Verify + Debug Fast-Path

## Iron Law
**build/fix ✕ / verify + debug ◯.** code 변경 ❌.

## 동작

```
1. build-verify SKILL 호출 (verification-before-completion)
2. 실패 → build-debug SKILL 자동 호출 (4-phase)
3. STOP — 사용자 interrupt 대기
4. /dev-kit:build 호출 시 정상 flow
```

## 규칙

- read-only + run-tests 만
- Edit/Write 도구 ❌
- 빠른 검증 + 디버그 루프 → 사용자 보고

## Hook 정렬

Build stage와 동일 (tdd-guard OFF, verify ON).
