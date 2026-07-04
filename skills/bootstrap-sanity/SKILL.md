---
name: bootstrap-sanity
category: bootstrap
description: read-only audit of project preconditions. Deterministic (regex + glob, no LLM call). Outputs PASS/WARN/FAIL to .dev-kit/sanity-report.md.
when_to_use: |
  - When `/dev-kit:bootstrap` first run
  - When user runs `/dev-kit:audit` with --sanity-only
allowed-tools: Read Glob Bash
disallowed-tools: Write Edit WebFetch Agent
model: haiku
disable-model-invocation: false
---

# bootstrap-sanity — Read-Only Precondition Audit

## Iron Law (예외 없음)
**파일을 절대 수정하지 않는다.** 입력 디렉토리만 읽고 결과를 `.dev-kit/sanity-report.md`로 emit.

## Gate 출력

| Result | Condition |
|---|---|
| **PASS** | 모든 필수 precondition 통과 |
| **WARN** | 1~3개 WARN (pass-through 허용) |
| **FAIL** | 4+ WARN 또는 critical 1+ — Plan 진입 ❌ |

## 7-Check Audit (결정론)

| # | Check | Tool | Severity |
|---|---|---|---|
| 1 | `package.json` 또는 `pyproject.toml` 존재 (manifest) | `Glob` | WARN |
| 2 | `.git/` 디렉토리 정상 (HEAD 존재) | `Bash: git rev-parse --git-dir` | WARN |
| 3 | `docs/` 디렉토리 4개 템플릿 placeholder (`ARCHITECTURE.md`, `PRD.md`, `ADR.md`, `DESIGN.md`) | `Glob` | WARN |
| 4 | banned-phrase scan (slop-detector SSOT regex) | `Bash: slop-detector.sh` (read-only) | WARN |
| 5 | secret-scan (credential pattern) | `Bash: secret-scan.sh` (read-only) | **CRITICAL FAIL** |
| 6 | hook bypass detection (`DEV_KIT_HOOK_OFF=*` 환경) | `Bash: env \| grep` | WARN |
| 7 | methodology lockfile (`lib/methodology.json` 일관성) | `Read` | WARN |

## 출력 형식

```markdown
# Sanity Report — dev-harness-kit
- scanned_at: ISO-8601 KST
- target: <absolute path>
- result: PASS / WARN / FAIL
- checks:
  - [PASS] check_1: package.json found
  - [PASS] check_2: .git/ OK
  - [WARN] check_3: docs/DESIGN.md template missing (Bootstrap will create)
  ...
- critical_issues: []
- recommendations:
  - "ok to proceed to /dev-kit:plan"
```

## 규칙 (예외 없음)

- **Read-only invariant**: 어떤 파일도 수정 ❌. 검증은 Read + Glob + Bash (stat/grep/cat)만.
- **LLM 호출 0회**: 결정론. 결과 재현 가능.
- **빠른 실패**: critical 1개라도 발견 시 즉시 FAIL + Plan 진입 차단.

## Hook 정렬

이 스킬은 active-hooks.json의 Bootstrap stage에서:
- `slop-detector=OFF` (sanity 자체가 slop 검증 = 자체)
- `secret-scan=read-only` (sanity 결과로 secret 발견 가능)
- `bash-guard=OFF` (sanity는 안전한 Bash만 호출)
