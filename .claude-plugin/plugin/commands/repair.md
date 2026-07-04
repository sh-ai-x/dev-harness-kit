---
name: repair
description: 8단계 Eval-Repair loop (golden → judge → root cause → fix → judge → A/B → diff → Human Review). 마지막 단계 = 사용자 1회 approve.
when_to_use: |
  - User types /dev-kit:repair approve|reject|defer <asset>
allowed-tools: Read Grep Glob Bash Agent
disallowed-tools: Edit Write
model: opus
---

# /dev-kit:repair — Eval-Repair Loop (Human Review terminal)

8단계 자동 + 마지막 단계 = 사용자 1회 approve. auto-commit 절대 ❌ (MUST-NOT-31).

## 8 단계

1. golden_set read
2. LLM as Judge (4축 점수)
3. 실패 점수화 + root cause
4. Specialized Fixer 호출 (9개 category)
5. Fix candidate → 재평가 (loop max 3)
6. A/B Validation Regression (golden 불변)
7. Diff 초안 자동 작성 (`.dev-kit/repair/<asset>.diff`)
8. **Human Review** (사용자 `approve|reject|defer <asset>`)

## 명령

- `/dev-kit:repair list` — pending diff 목록
- `/dev-kit:repair approve <asset>` — git apply
- `/dev-kit:repair reject <asset>` — diff 폐기 + golden 회귀 패턴 추가
- `/dev-kit:repair defer <asset>` — diff 보존
