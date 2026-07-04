# INTEGRATION — 5 repo → 1 plugin 매핑

> Dev-harness-kit 흡수 매핑. 옛 repo는 DEPRECATED.md 1줄로 표기.

| 옛 repo | 흡수 위치 | 비고 |
|---|---|---|
| `pm-prd-fast` | `skills/plan-ralph/SKILL.md` | Plan+Ralph loop, 6 gates + Seed convergence 통합 (MUST-50) |
| `interview-harness-skills` | (`plan-ralph`에 흡수) | Seed convergence → plan-ralph 안의 phase 2 |
| `dev-harness` | `lib/execute.py` + `skills/build-*` | harness-runner engine + 5 disciplines |
| `claude-review-plugins` | `skills/review/` (3-dim) + `skills/security/` (10-dim OWASP) | review-code/security-scan 기능 통합 → review/security 로 단일화 |
| `slop-shield` | `hooks/slop-detector.sh` + `hooks/secret-scan.sh` (slop-shield). Iron Laws → CLAUDE.md §1 | stop-verify 흡수, slop-detector SSOT |

## Hook 통합

| 옛 hook | 흡수 위치 | 모드 |
|---|---|---|
| dev-harness tdd-guard | `hooks/tdd-guard.sh` (dev-harness-kit) | advisory `--strict`만 차단 |
| dev-harness bash-guard | `hooks/bash-guard.sh` | 동상 |
| dev-harness secret-scan | `hooks/secret-scan.sh` | PostToolUse, advisory |
| slop-shield slop-detector | `hooks/slop-detector.sh` (SLOP= SSOT) | PostToolUse, advisory |
| slop-shield verify-gate | `hooks/stop-verify.sh` (통합) | Stop event, fail-open |
| dev-harness stop-verify | (위와 통합) | Stop event |
| claude-review-plugins pre-commit | `.githooks/pre-commit` (CI용) | review shell call |
| claude-review-plugins pre-push | `.githooks/pre-push` (main-block) | main push 차단 |

## 매핑 도식

```
[신규 dev-harness-kit plugin]
  ↓ 흡수
[옛 5 plugin 들은 DEPRECATED.md]
```

옛 repo 코드 자체는 보존. dev-harness-kit의 코드는 이 5개의 카피.
