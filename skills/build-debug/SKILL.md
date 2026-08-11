---
name: build-debug
category: build
description: 4-phase systematic debugging. No fix proposal before Phase 1 (reproduce) completes (MUST-L2). Root-cause-first Iron Law.
alpha: enforcement
when_to_use: |
  - User types "bug" / "doesn't work" / "why failing" / "error"
allowed-tools: Read Write Bash
disallowed-tools: Edit Write WebFetch
model: opus
user-invocable: false
---
> [← Skills index](../../README.md)

# build-debug — Systematic Debugging (4 Phase)

## Iron Law
**No fix proposal before Phase 1 (reproduce) completes.**

## Optional Linear preflight

At the start of a new debugging task, invoke `/dev-kit:linear` once when
Linear is enabled or available. Continue normally on `LINEAR_SKIP` or an
implicit `LINEAR_ERROR`; do not invoke it between debugging phases. See
`skills/linear/SKILL.md` for the reconciliation contract.

## 4 Phases (separate cycles)

```
[1/4] REPRODUCE  → 1+ failing cases
       (don't proceed if reproduction fails)
       ↓
[2/4] ISOLATE    → minimal reproduction
       (reduce input / block external deps)
       ↓
[3/4] ROOT CAUSE → specific line + call stack quoted
       (tools: git blame, git bisect, log, debugger)
       ↓
[4/4] FIX        → with regression test
       (Iron Law L1: no test = no fix)
```

## Rules (no exceptions)

- Do not bundle 4 phases into one cycle (MUST-NO-LOOP).
- User confirmation after each phase, or 4 phases in separate calls.
- Asserting "probably X" without root cause quoted ❌.
- One change at a time. Multiple changes at once ❌.

## Hook integration

`tdd-guard` is ON during build stage. Writing a fix during debug forces a regression test to accompany it.

## Hand-off

After 4 phases:
- Quote root cause in 1 line + regression test GREEN
- `state_codec.append_hand_off(root, "build", "build", "..")`
- Loop back to the per-step harness runner (lib/execute.py)

## Red Flags

| Thought | Reality |
|---|---|
| "Probably X" | Unknown if not reproduced |
| "Just patch it" | Ignoring root cause → same bug repeats |
| "Multiple changes at once" | Can't tell which change was the fix |
| "Skip reproduce" | L2 violation |
