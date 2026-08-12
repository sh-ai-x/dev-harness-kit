---
name: build-verify
category: build
description: verification-before-completion. No "done" without quoted exit code + test count + build log (MUST-L3, hook stop-verify).
alpha: enforcement
when_to_use: |
  - User types "done" / "finished" / "passing" (declaration phrases)
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch
model: haiku
user-invocable: false
---
> [← Skills index](../../README.md)

# build-verify — Evidence-Before-Done

## Iron Law (no exceptions)
**No "done" without running the actual command and quoting evidence.**

## Verification priority (strong → weak)

```
1. exit code (test runner / linter / build)
2. test count (passed/failed/skipped)
3. lint count
4. runtime log (last 30 lines)
5. file path + line number cited
```

## Rules

- "should work" / "probably fine" / "done" ❌
- On "passing" claim, stop-verify hook auto-warns on stderr:
  > [verify-gate] You said it works but cited no output/exit/test evidence.
  > [verify-gate] Run the verify command and quote the output.
- 2-layer (MUST-9): (a) hook auto-check + (b) skill advisory
- Regression fixtures (`fixtures/real-bugs/`) recommended

## Hook integration

`stop-verify.sh` is auto-active in Build / Review / Security / Ship stages. When user says "done" / "finished", hook receives stop event and runs verify command + outputs result to stderr.

## Usage example

```
User: "TDD done"
Hook: [stop-verify] running pytest...
       passed 12, failed 0, error 0
       OK — quoted 12/12.
       Iron Law L3 verified.
```

OR (failure):

```
User: "done"
Hook: [verify-gate] You said it works but cited no output/exit/test evidence.
       Run the verify command and quote the output.
```

## Hand-off

On verify pass → `state_codec.transition_stage(root, "review")` auto. On fail → loop back to the per-step harness runner (lib/execute.py).
