---
name: status
category: status
description: HOTL 시각화. 현재 loop 진행 + 누적 사이클 + hand-off chain + eval 점수 한 화면.
when_to_use: |
  - User types @dev-kit status
allowed-tools: Read Grep
disallowed-tools: Bash Edit Write
model: haiku
disable-model-invocation: false
---

# @dev-kit:status — HOTL 시각화

읽기 전용. 현재 stage + 누적 cycles + drift score + hand-off pointer.

push 알림 ❌. 사용자 호출 시에만.