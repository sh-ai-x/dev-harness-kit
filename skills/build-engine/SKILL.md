---
name: build-engine
category: build
description: harness-runner engine per step. atomic write + 2-commit protocol + parallel worktree. 자체 사이클 없음 (각 step은 별개 사이클, MUST-NO-LOOP).
when_to_use: |
  - Auto-invoked by /dev-kit:build per step
allowed-tools: Read Write Bash
disallowed-tools: Edit WebFetch Agent
model: sonnet
---

# build-engine — Phase Step Executor

## Iron Law
**step 외 작업 추가 ❌.** step file에 명시되지 않은 파일·기능은 만들지 않는다. 추가 필요 시 새 step을 index.json에 등록.

## 2-Commit Protocol

```
[1] feat(scope): step<N> — <name>
    (코드 변경)
[2] chore(scope): step<N> output
    (step<N>-output.json 기록)
```

`git reset HEAD -- <path>` 두 커밋 사이 사용.

## Hook 정렬

Build stage에서 모두 ON:
- `tdd-guard` (active per methodology)
- `bash-guard` (destructive 명령 차단)
- `secret-scan` (PostToolUse: credential pattern)
- `slop-detector` (KO+EN banned phrases)
- `stop-verify` (Stop event: AC claim)

## 규칙

- **MAX_RETRIES=3**: step별 3회 재시도. 그 후 → `status=error` + 메인에 report.
- **`--parallel N`**: N개 독립 step을 worktree 격리 동시 실행. phase dependencies 자동 감지.
- **resume**: pending step 자동 이어서. `index.json` status 머신.
- **blocked**: 사용자 개입 필요 (API key / 수동 설정). `blocked_reason` 필수 (status state machine validate).
- **idempotent**: `step<N>-output.json` 재실행 시 atomic overwrite.

## 출력

- `step<N>-output.json`: `{step, phase, exit_code, stdout, stderr, duration_seconds, timestamp}`

## Sub-agent 위임

MUST-36: 메인 오케스트레이터 = AC 위임. sub-agent는 `lib/sub_agent_runner.py` 통해 spawn (Phase 3 흡수 예정). worktree 격리 + 권한 (Bash/Read/Edit/Write/Glob/Grep/WebFetch/lint/browser).
