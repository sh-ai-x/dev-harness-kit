---
name: audit
category: audit
description: 0-arg cross-cutting. slop + secret 일괄 감사. READ-ONLY.
when_to_use: |
  - User types /dev-kit:audit
  - Bulk audit before release
allowed-tools: Read Grep Glob Bash
disallowed-tools: Write Edit
model: haiku
disable-model-invocation: false
---

# /dev-kit:audit — Cross-cutting audit

read-only. HIGH/MEDIUM/LOW buckets 출력. 절대 write ❌.

## 규칙

- `/dev-kit:audit --secrets-only` → secret 만
- `/dev-kit:audit --slop-only` → slop 만
- combined mode (default) → 둘 다