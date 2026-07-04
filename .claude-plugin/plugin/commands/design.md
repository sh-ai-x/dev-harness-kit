---
name: design
description: DEPRECATED ALIAS for /dev-kit:plan (MUST-50 Plan+Design 통합). 호출 시 자동 통합 + deprecation notice.
when_to_use: |
  - Legacy users typing /dev-kit:design (deprecated since MUST-50 merge)
allowed-tools: Read Glob
disallowed-tools: Bash Edit NotebookEdit WebFetch
model: opus
---

# /dev-kit:design — DEPRECATED ALIAS

**DEPRECATION NOTICE (MUST-50)**: 이 커맨드는 `/dev-kit:plan`의 별칭으로 통합됨. 호출 시:

1. ✅ 동일 결과 — `plan-ralph` SKILL dispatch.
2. ⚠️ Stdout에 deprecation message 출력.
3. 🔄 다음 사용자는 `/dev-kit:plan` 사용 권장.

```bash
/dev-kit:design "my idea"   # → 자동 /dev-kit:plan 호출 + 위 결과 동일
```

## 무엇이 통합됐나

옛 5-stage flow `Plan → Design → Build → Review → Ship` (8 stages with san/api) → 새 4-stage `Plan+Design → Build → Review → Ship`.

| 옛 | 새 |
|---|---|
| `/dev-kit:plan` (PM 6 gates) | `/dev-kit:plan` (PM 6 gates + Seed convergence + Phase 분해 모두 한 루프) |
| `/dev-kit:design` (Seed convergence) | (통합됨) |

MUST-50 + ADR-0020 참조.
