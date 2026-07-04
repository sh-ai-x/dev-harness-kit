---
name: shortcut-quick-fix
description: verify + debug만 빠르게.
when_to_use: |
  - User types /dev-kit:quick-fix
allowed-tools: Read Bash
disallowed-tools: Write Edit
model: sonnet
---

# /dev-kit:quick-fix — Verify + Debug Fast-Path

build-verify SKILL → 실패 시 build-debug SKILL → STOP.

read-only + run-tests만. Edit/Write ❌.
