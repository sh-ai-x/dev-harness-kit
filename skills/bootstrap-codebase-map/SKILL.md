---
name: bootstrap-codebase-map
category: bootstrap
description: deterministic synthesis of codebase tree + manifest + dependencies + conventions → CLAUDE.md §3. Read-only, no LLM call.
when_to_use: |
  - When `/dev-kit:bootstrap` after sanity PASS
  - When user runs `/dev-kit:map` or `--refresh-codebase-map`
allowed-tools: Read Glob Bash
disallowed-tools: Write Edit WebFetch Agent
model: haiku
disable-model-invocation: false
---

# bootstrap-codebase-map — Auto Codebase Context

## Iron Law (예외 없음)
**추측 / 보강 ❌.** 사전 검증된 도구 (glob/cat/jq) 출력만 사용. 추정 발생 시 `STALE: 추정` 마커 + 다음 사용자 입력 대기.

## 4-Section 합성

`lib/write_claude_md.py`가 CLAUDE.md §3에 삽입:

| §3 Section | 원천 | 도구 |
|---|---|---|
| **Tree** | 재귀 glob (depth 4, `node_modules` `.git` `dist` `__pycache__` 제외) | `Glob` + 경로 정렬 |
| **Manifest** | `package.json` / `pyproject.toml` / `go.mod` 자동 검출 | `Bash: jq` / `Read` |
| **Deps** | lockfile (`pnpm-lock.yaml` / `package-lock.json` / `requirements.txt`) top 10 | `Bash: head -10` |
| **Conventions** | `.editorconfig` / `.eslintrc` / `pyproject.toml [tool.*]` / commit trailer 규칙 | `Read` |

## Lazy 모드 (MUST-11)

| Mode | 출력 | 토큰 |
|---|---|---|
| `--slim-claude-md` (default) | §3 = 5줄 STUB + `+codebase-map:full` 마커 | ~200 tokens |
| `--full-claude-md` (opt-in) | §3 = 전체 4 섹션 inline | 500~5000 tokens |

## 규칙 (예외 없음)

- **Determinism**: 동일 입력 → 동일 출력. `jq --sort-keys` + path stable sort.
- **Lockfile 수정 ❌**: `pnpm-lock.yaml`, `package-lock.json` 변경 ❌.
- **Secret mask**: deps / config 출력 시 `password|token|key` = `***` 마스킹.
- **STALE marker**: 추정 발생 시 `<!-- STALE: <reason> -->` 자동 부착 + 빌드/plan interrupt.

## Hook 정렬

Bootstrap stage에서:
- `slop-detector=OFF`
- `bash-guard=OFF` (안전 명령만)
- `secret-scan=read-only` (출력 secret 자동 마스킹)
