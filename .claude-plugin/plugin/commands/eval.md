---
name: eval
description: 자산 신선도 (CLAUDE.md / skill / hook / Iron Law) LLM-as-judge 평가. /dev-kit:eval dry-run + golden set 13개 cross-check.
when_to_use: |
  - User types /dev-kit:eval
  - nightly cron 자동 호출
allowed-tools: Read Grep Bash Agent
disallowed-tools: Write Edit
model: opus
---

# /dev-kit:eval — Asset Freshness Eval (4 axes)

4축 점수 (semantic_drift / completeness / correctness / consistency). 0-10 척도. ≥ 8 OK, 5~7 drift warning, < 5 ROT.

## 규칙

- DRIFT WARNING → 비동기 알림 (Slack/Email/PR 봇).
- ROT → CI fail. hard-block 없음 — 사용자 interrupt 가능.
- 2-judge cross-check (MUST-NOT-23). 부적합 시 `.pending.json` 격리.

## 다음 단계

DRIFT WARNING 시 `/dev-kit:repair` 자동 호출 (8단계 loop).
