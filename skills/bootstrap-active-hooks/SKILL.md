---
name: bootstrap-active-hooks
category: bootstrap
description: stage-aware hook matrix initialization. Reads `.claude-plugin/plugin/hooks/hooks.json` and writes `.dev-kit/.active-hooks.json` with stage-aware defaults.
when_to_use: |
  - When `/dev-kit:bootstrap` after codebase-map
  - When stage transition (Plan → Design → Build, etc.)
allowed-tools: Read Write Glob
disallowed-tools: Bash WebFetch Agent
model: haiku
---

# bootstrap-active-hooks — Stage-Aware Hook Matrix (SSOT)

## Iron Law
**모든 hook 활성 상태는 단 한 곳 `.dev-kit/.active-hooks.json`에서 결정.** `hooks/hooks.json`은 매트릭스 reader만 등록.

## 출력 형식

```json
{
  "schema_version": "1.0.0",
  "updated_at": "2026-07-04T15:30:00Z",
  "matrix": {
    "bootstrap": {
      "tdd-guard": false,
      "bash-guard": false,
      "secret-scan": "read-only",
      "slop-detector": false,
      "stop-verify": false
    },
    "plan":       { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true },
    "design":     { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true },
    "build":      { "tdd-guard": true,  "bash-guard": true,  "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "review":     { "tdd-guard": false, "bash-guard": false, "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "security":   { "tdd-guard": false, "bash-guard": false, "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "ship":       { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true }
  },
  "override": {
    "disabled_hooks": [],
    "strict_mode": false
  }
}
```

## Hook 셸 (참조)

| Hook | Stage ON | 비고 |
|---|---|---|
| `tdd-guard` | build | lib/methodology/tdd.py 활성 시에만 (MUST-48) |
| `bash-guard` | build | `rm -rf`, `git push --force main` 등 패턴 |
| `secret-scan` | build / review / security | PostToolUse: credential pattern grep |
| `slop-detector` | build / review / security | KO+EN banned phrases |
| `stop-verify` | plan / design / build / review / security / ship | Stop event: AC claim 검증 |

## 규칙

- **모든 hook default `exit 0`** (MUST-12). hard-block(`exit 2`)은 `--strict` 모드만.
- **`--strict` flag**: 모든 hook `exit 2` 활성화. 사용자 명시 opt-in.
- **`DEV_KIT_HOOK_OFF=<hook1>,<hook2>` env**: 일시 OFF (override).

## Stage transition 자동 갱신

`/dev-kit:<stage>` 호출 시 `lib/state_codec.py`가 `.active-hooks.json`의 `current_stage` field 자동 갱신 + hook 셸 `read` 호출 시 매트릭스 확인.
