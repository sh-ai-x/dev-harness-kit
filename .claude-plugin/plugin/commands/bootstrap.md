---
name: bootstrap
description: 0-arg orchestrator. Runs sanity + codebase-map + active-hooks initialization. Writes CLAUDE.md SSOT.
when_to_use: |
  - User types `/dev-kit:bootstrap` 1st time on a new project
  - User wants to refresh CLAUDE.md / active-hooks.json
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: opus
---

# /dev-kit:bootstrap — First-Run Orchestrator

## Iron Law (예외 없음)
**인자 0개로 디폴트 OK 동작.** Hidden flags (`--skip-sanity`, `--skip-map`, `--slim|--full`, `--team`, `--strict`)만 허용.

## 4-Step Orchestration (3 autonomous + 1 user confirm)

```
[1] sanity bootstrap-sanity           → .dev-kit/sanity-report.md
       ↓ (auto, deterministic regex + glob)
[2] codebase-map bootstrap-codebase-map → §3 (5-line STUB default)
       ↓ (auto, Read + Glob + Bash)
[3] active-hooks bootstrap-active-hooks → .dev-kit/.active-hooks.json (SSOT)
       ↓ (auto)
[4] write-claude-md lib/write_claude_md.py → CLAUDE.md (§1~§5 atomic)
       ↓ (auto)
[5] user review 1회 (HOTL, MUST-29)
       ↓
[6] exit / hand-off → /dev-kit:plan 호출 대기
```

## Hook 정렬 (stage=bootstrap)

| Hook | Mode |
|---|---|
| tdd-guard | OFF |
| bash-guard | OFF |
| secret-scan | read-only |
| slop-detector | OFF |
| stop-verify | OFF |

`active-hooks.json` SSOT 자동 초기화 (MUST-13). `--strict` 시 모든 hook `exit 2`.

## 규칙 (예외 없음)

- **0-arg UX (MUST-21)**: 인자 0개. 분기는 `when_to_use` 자동 매칭.
- **HOTL (MUST-29)**: 단계 1~4 자동 진행. §5 hand-off pointer 자동 갱신.
- **YAGNI**: 별도 옵션 prompt ❌ (MUST-NOT-13). `--slim|--full` 등 hidden flag만.
- **No-over-engineering (MUST-25)**: 디폴트 동작으로 80% 해결. 추가 기능 = ADR 필요.

## Hand-off 결과

성공 시 `.dev-kit/hand-off/bootstrap→plan.md` 자동 작성 (state_codec.py). 다음 `/dev-kit:plan` 호출 시 preamble 자동 주입.

## Hot Failure (FAIL 시)

- sanity FAIL → Plan 진입 차단. `/dev-kit:plan` 호출 시 stderr 경고.
- Hook override (`DEV_KIT_HOOK_OFF=*`) 자동 감지 → sanity report WARN.
- `eval/golden/*.json` 부재 → bootstrap은 영향 없음 (Phase 3 이후 도입).
