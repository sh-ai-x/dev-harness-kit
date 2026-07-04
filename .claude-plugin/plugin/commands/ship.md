---
name: ship
description: 0-arg. Release tag 발행. gate check only (hooks auto). Review verdict=Approve + main-block 통과 필수.
when_to_use: |
  - User types /dev-kit:ship
  - Release cutoff
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch
model: haiku
---

# /dev-kit:ship — Release Gate

## 동작

1. Verify pre-push main-block 통과 (gh-autoswitch).
2. Review verdict=Approve 확인 (security scan 별도 통과이면 OK).
3. CHANGELOG entry 자동.
4. git tag + push.

## Iron Law

- main 직접 push ❌. PR only.
- --no-verify 남용 ❌.
- auto-merge 미포함 (사용자 검토 후).

## Hook 정렬

`stop-verify=ON`. main-block hook 검증.
