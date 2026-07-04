---
name: review
description: 3-dim parallel fan-out (correctness + security + architecture) + verifier pass. PR-style verdict + inline findings.
when_to_use: |
  - User types /dev-kit:review
  - Before merge / PR
allowed-tools: Read Grep Glob Bash Agent
model: opus
---

# /dev-kit:review — Multi-Dimension Code Review

## 동작

1. 3 sub-agent **단일 메시지 동시 fan-out** (MUST-10): correctness + security + architecture
2. Verifier pass 별개: 1 sub-agent refutes each candidate (CONFIRMED | PLAUSIBLE | REJECTED)
3. PR-style summary + inline comments

## Verdict

| Severity | Verdict |
|---|---|
| 🔴 critical ≥ 1 | Blocked |
| 🟠 major ≥ 1 (no critical) | Changes Requested |
| 그 외 | Approve |

## Hook 정렬 (Stage C)

`slop-detector, secret-scan, stop-verify` ON. tdd-guard OFF (review 단계).

## 다음 단계

`/dev-kit:security` (10-dim 별도) 또는 `/dev-kit:ship`.
