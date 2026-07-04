---
name: security
description: 10-dim OWASP Top 10 2025 fan-out (A01-A10). Per-category breakdown + verdict. /dev-kit:review 와 독립 커맨드.
when_to_use: |
  - User types /dev-kit:security
  - Pre-release / quarterly / before major refactor
allowed-tools: Read Grep Glob Bash Agent
model: opus
---

# /dev-kit:security — OWASP Top 10 Audit

## 동작

1. 10 sub-agent **단일 메시지 동시 fan-out** (A01-A10). 별도 fan-out (review와 다른 차원).
2. Per-category breakdown table.
3. Verdict: ≥ 5 finding = Approve (severity별). 0~2 = Blocked.

## Hook

Review와 동일 (slop-detector, secret-scan, stop-verify ON).

## 다음 단계

`/dev-kit:review` 또는 `/dev-kit:ship`.
