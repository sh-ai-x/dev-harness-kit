---
name: audit-secret
category: audit
description: secret scan read-only audit. credential pattern detection. NEVER print secret text.
when_to_use: |
  - User types /dev-kit:audit (cross-cutting, or --secrets-only flag)
allowed-tools: Read Grep Bash
disallowed-tools: Write Edit
model: haiku
---

# audit-secret — Read-Only Credential Audit

## Iron Law
**secret text 절대 print ❌.** Match만 보고 (path + masked value `***`).

## SSOT

`hooks/secret-scan.sh`의 patterns:
- `AKIA...` (AWS)
- `sk-...` / `sk-ant-...` (Anthropic)
- `ghp_...` / `gho_...` (GitHub)
- `xox[bpoa]-...` (Slack)
- `-----BEGIN ... PRIVATE KEY-----`
- `postgres://user:pass@`
- `mongodb+srv://user:pass@`

## 출력

```markdown
## /dev-kit:audit secret — {path} — {N} files / {M} matches

### CRITICAL
- src/auth.ts:42 `AKIA***` (AWS key — REMOVE)

### WARN
- scripts/setup.sh:8 — env file 참조 (확인 필요)
```

## 규칙

- 발견 시 즉시 CRITICAL. fail-open 모드도 warn.
- Line number 명시. masked value 1개만 (예시).
- Read-only — 절대 write ❌.

## Hook

`secret-scan.sh` PostToolUse 자동 활성 (Build/Review/Security).

## 회귀

- 빈 fixture → 0 finding
- 기제 fixture (rotate) → masked-only report
