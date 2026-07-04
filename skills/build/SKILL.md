---
name: build
category: build
description: 0-arg. Per-step sub-agent 위임 + 자가 수정 루프 (MUST-36~38). harness-runner engine 사용. TDD + verify + debug 통합.
when_to_use: |
  - User types /dev-kit:build
  - After plan+design (PRD.md + phases/<name>/ 존재)
allowed-tools: Read Write Bash Glob Grep Agent
disallowed-tools: WebFetch
model: opus
disable-model-invocation: false
---

# /dev-kit:build — Step-by-Step Implementation

## Iron Law
**sub-agent 자가 수정, 사용자 인터럽트 1회, AC 충족 시에만 다음 step.**

## 동작

1. harness-runner engine (`lib/execute.py`) 자동 호출.
2. `phases/<name>/index.json` 순차 (또는 `--parallel N` 동시).
3. 각 step = sub-agent 1명 (MUST-36):
   - Worktree 격리 (MUST-38)
   - AC 위임 + 5필드 loop 의미 (MUST-15)
   - 자가 수정 루프 (MUST-37): lint / test / browser access
   - 3 cycles max (MUST-NOT-9, 10)
4. PASS 시 다음 step 자동. 3회 FAIL 시 build↔debug hand-off 자동.

## Hook 정렬 (Stage B)

| Hook | Mode |
|---|---|
| tdd-guard | ON (methodology=tdd 일 때) |
| bash-guard | ON |
| secret-scan | ON (PostToolUse) |
| slop-detector | ON |
| stop-verify | ON |

## 출력

- `phases/<name>/step<N>-output.json` per step (exit code + stdout + stderr + duration)
- `.dev-kit/hand-off/build→review.md` 자동
- 2-commit protocol: `feat(scope): step N — <name>` + `chore(scope): step N output`

## 다음 단계

`/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) 후 `/dev-kit:ship`.