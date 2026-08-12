---
name: build-debug
category: build
description: 4-phase systematic debugging. No fix proposal before Phase 1 (reproduce) completes (MUST-L2). Root-cause-first Iron Law. Standalone invocation hands the root cause to /dev-kit:plan instead of fixing inline.
alpha: enforcement
when_to_use: |
  - User types "bug" / "doesn't work" / "why failing" / "error"
  - User types /dev-kit:build-debug <bug description> for a standalone root-cause investigation, outside any active build step
allowed-tools: Read Bash Skill
disallowed-tools: Edit Write WebFetch
model: opus
user-invocable: true
---
> [← Skills index](../../README.md)

# build-debug — Systematic Debugging (4 Phase)

## Iron Law
**No fix proposal before Phase 1 (reproduce) completes.**

## Two invocation contexts

Phases 1-3 (reproduce → isolate → root cause) are identical either
way. Phase 4 branches on context:

| | In-build self-fix loop | Standalone (top-level) |
|---|---|---|
| Trigger | model auto-invokes mid-step, inside an already-reviewed build step (`lib/execute.py`'s 3-cycle self-fix guard) | user types `/dev-kit:build-debug <bug description>`, or the model reasons a bug report needs root-cause-first investigation with no build step running |
| Phase 4 | fixes inline with a regression test (Iron Law L1: no test = no fix) | hands the quoted root cause to `/dev-kit:plan` via `Skill("plan", ...)`; does NOT patch code |
| Reviewed before code changes? | no — it's already inside a reviewed step | yes — `/dev-kit:plan`'s Gate 5/5 stops at the auto-rendered `proposal.html` and waits for the user to explicitly type `/dev-kit:build` |

The standalone path exists because there was no top-level command for
"here's a bug report, root-cause it and plan a proper fix with a
reviewable proposal" — before this, a user who found a bug with no
build step running had to invent the reproduce/root-cause discipline
by hand, or jump straight from "found a bug" to editing code with no
PRD, no proposal, no review.

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
[4/4] FIX or PLAN HAND-OFF
       in-build:    fix with a regression test (Iron Law L1: no test = no fix)
       standalone:  Skill("plan", <bug summary + root cause>) — no code written here
```

## Rules (no exceptions)

- Do not bundle 4 phases into one cycle (MUST-NO-LOOP).
- User confirmation after each phase, or 4 phases in separate calls.
- Asserting "probably X" without root cause quoted ❌.
- One change at a time. Multiple changes at once ❌.
- Standalone Phase 4 refuses to write a regression test, patch source,
  or invoke `/dev-kit:build`/`/dev-kit:build-tdd` directly — that work
  is scoped and reviewed by `/dev-kit:plan` first.

## Hook integration

`tdd-guard` is ON during build stage. Writing a fix during the in-build
Phase 4 forces a regression test to accompany it. The standalone Phase
4 never reaches `tdd-guard` — it writes no code.

## Hand-off

**In-build self-fix loop**, after 4 phases:
- Quote root cause in 1 line + regression test GREEN
- `state_codec.append_hand_off(root, "build", "build", "..")`
- Loop back to the per-step harness runner (lib/execute.py)

**Standalone**, after Phase 4's plan hand-off:
- `Skill("plan", <bug summary + root cause>)` — the quoted root cause
  (file:line + call stack) becomes Gate 1's `situation` field and
  Gate 2's evidence signal.
- `/dev-kit:plan` runs its full 5-gate loop and, at Gate 5/5, emits
  `PRD.md` + `phases/<name>/` and auto-renders
  `docs/proposals/<main>/<sub>.html`. This skill's job ends there.

## Red Flags

| Thought | Reality |
|---|---|
| "Probably X" | Unknown if not reproduced |
| "Just patch it" | Ignoring root cause → same bug repeats |
| "Multiple changes at once" | Can't tell which change was the fix |
| "Skip reproduce" | L2 violation |
