> [← Skills index](README.md) · [Project README](../../README.md)

# `build-debug`

**Category:** `build` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:build-debug <bug description>` (human-invoked) — also auto-invoked by the model mid-build-step for the in-build self-fix loop

`build-debug` exists to stop the model from jumping straight to a fix the moment something looks broken. Its Iron Law — no fix proposal before Phase 1 (reproduce) completes — forces a strict reproduce → isolate → root-cause sequence so debugging sessions produce an actual root cause instead of a guess that happens to make the symptom go away. What happens after the root cause is found depends on how the skill was entered.

## Two invocation contexts

Phases 1-3 (reproduce → isolate → root cause) are identical either way. Phase 4 branches on context:

| | In-build self-fix loop | Standalone (top-level) |
|---|---|---|
| Trigger | model auto-invokes mid-step, inside an already-reviewed build step (`lib/execute.py`'s 3-cycle self-fix guard) | user types `/dev-kit:build-debug <bug description>`, or the model reasons a bug report needs root-cause-first investigation with no build step running |
| Phase 4 | fixes inline with a regression test (Iron Law L1: no test = no fix) | hands the quoted root cause to `/dev-kit:plan` via `Skill("plan", ...)`; does NOT patch code |
| Reviewed before code changes? | no — it's already inside a reviewed step | yes — `/dev-kit:plan`'s Gate 5/5 stops at the auto-rendered `proposal.html` and waits for the user to explicitly type `/dev-kit:build` |

The standalone path exists because there was previously no top-level command for "here's a bug report, root-cause it and plan a proper fix with a reviewable proposal" — a user who found a bug with no build step running had to invent the reproduce/root-cause discipline by hand, or jump straight from "found a bug" to editing code with no PRD, no proposal, no review.

## When to use it

- The model or user surfaces language like "bug" / "doesn't work" / "why failing" / "error" during a build step (in-build context).
- The user types `/dev-kit:build-debug <bug description>` for a standalone root-cause investigation outside any active build step.

## How it works

The skill runs four phases as separate cycles, never bundled into one:

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

Rules enforced throughout: the 4 phases must not be bundled into one cycle (MUST-NO-LOOP); the user confirms after each phase, or the 4 phases run as separate calls; asserting "probably X" without a quoted root cause is disallowed; only one change is made at a time. The standalone Phase 4 additionally refuses to write a regression test, patch source, or invoke `/dev-kit:build`/`/dev-kit:build-tdd` directly — that work is scoped and reviewed by `/dev-kit:plan` first.

The `tdd-guard` hook is ON during the build stage, so writing a fix during the in-build Phase 4 forces a regression test to accompany it — the skill cannot silently skip that requirement even if it wanted to. The standalone Phase 4 never reaches `tdd-guard` — it writes no code.

**In-build**, after all 4 phases complete: the skill quotes the root cause in one line, confirms the regression test is GREEN, calls `state_codec.append_hand_off(root, "build", "build", "..")`, and loops back to the per-step harness runner (`lib/execute.py`).

**Standalone**, after Phase 4's plan hand-off: `Skill("plan", <bug summary + root cause>)` — the quoted root cause (file:line + call stack) becomes Gate 1's `situation` field and Gate 2's evidence signal. `/dev-kit:plan` runs its full 5-gate loop and, at Gate 5/5, emits `PRD.md` + `phases/<name>/` and auto-renders `docs/proposals/<main>/<sub>.html`. This skill's job ends there.

## Output

- **In-build**: a quoted one-line root cause plus a GREEN regression test run, then a hand-off record via `state_codec.append_hand_off(root, "build", "build", "..")` looping control back to `lib/execute.py`.
- **Standalone**: quoted root cause (file:line + call stack) in the chat transcript, then `PRD.md` + `phases/<name>/` + `docs/proposals/<main>/<sub>.html` (owned by `/dev-kit:plan`, produced after the Phase 4 hand-off).

## Red flags

| Thought | Reality |
|---|---|
| "Probably X" | Unknown if not reproduced |
| "Just patch it" | Ignoring root cause → same bug repeats |
| "Multiple changes at once" | Can't tell which change was the fix |
| "Skip reproduce" | L2 violation |

## Related

- [build](build.md) — the parent skill whose per-step harness runner (`lib/execute.py`) the in-build self-fix loop loops back into; also the separate, explicit command the user invokes after `/dev-kit:plan`'s proposal is reviewed on the standalone path.
- [build-tdd](build-tdd.md) — supplies the regression-test discipline that the in-build Phase 4 fix relies on.
- [build-verify](build-verify.md) — the evidence-before-done gate that governs completion claims elsewhere in the build stage.
- [plan](plan.md) — emits `PRD.md` + `phases/<name>/index.json` + `step<N>.md`, auto-renders the proposal, and owns everything past the standalone Phase 4 hand-off.

---
*Source: [`skills/build-debug/SKILL.md`](../../skills/build-debug/SKILL.md)*
