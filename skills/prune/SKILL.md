---
name: prune
category: build
description: 0-arg slop-removal chain. One slash wraps inspect → build-prune → review. 3 gated phases for deleting AI slop and dead features (not refactoring).
when_to_use: |
  - User types /dev-kit:prune
  - User types "remove AI slop" / "delete dead code" / "sweep the codebase for cruft"
  - Whole-pipeline *deletion* after a refactor PR — for *refactoring* use `/dev-kit:refactor`
  - For removing one named feature end-to-end, use `/dev-kit:feat-remove <feature>` instead
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: Edit WebFetch
model: opus
user-invocable: true
---

## What it does

Whole-pipeline *deletion* sweep: dispatches `/dev-kit:inspect` for a
read-only baseline, then `/dev-kit:build-prune` for the mutating 3-pass
removal (orphan-code → dead-feature → slop-pattern), then
`/dev-kit:review` for the per-diff verification. The three phases are
**separate calls, not one big cycle** (MUST-NO-LOOP) -- each phase
gates the next on a quoted exit code and test count. No deletion lands
without a passing test suite; no phase starts without the previous
phase's green evidence.

**`/dev-kit:prune` vs. `/dev-kit:refactor` vs. `/dev-kit:feat-remove`:**

| Skill | Arg | Discovers | Action |
|---|---|---|---|
| `/dev-kit:refactor` | none | n/a (called by user) | refactor (rewrite/extract/rename) |
| `/dev-kit:prune` (this) | none | automatic: scan for slop | delete (rm) |
| `/dev-kit:feat-remove` | `<feature>` required | manual: user names it | delete (rm) — single feature |

`prune` is the project-wide counterpart to `feat-remove` — no `<feature>` arg, automatic discovery of dead/orphan/slop candidates.

## Pre-flight

- 0-arg: whole project directory. Optional `<path>` narrows scope to a
  subtree (passed through to each phase).
- No version-gated preconditions. The dev-harness-kit repo itself is
  the provider of `/dev-kit:ci-setup`, so requiring a consumer-side
  `ci-config.json` here is self-referential. The phase-1 inspect
  baseline + phase-2 per-pass green tests (MUST-L1, MUST-L3) already
  enforce a runnable, fast test suite.
- The test suite must be discoverable and runnable in < 10 minutes.
  If the existing suite is heavier, run `/dev-kit:feat-revise` for
  the affected feature first to keep per-pass gates fast.
- Optional `--phase N` (1|2|3) re-runs only that phase. Default: all
  three in order.
- Optional `--dry-run` (default: ON for first pass). The skill never
  calls `rm` or `git rm` itself — it emits commands and waits for the
  user to run them, mirroring `feat-remove` discipline.

## 3 phases (separate calls)

```
[1/3] INSPECT   -> /dev-kit:inspect
       (read-only baseline; .dev-kit/inspect-report.md with 8-dim findings)
       ↓ quoted: report path + verdict + finding count
[2/3] PRUNE     -> /dev-kit:build-prune
       (3 passes internally: orphan-code -> dead-feature -> slop-pattern;
        each pass ends with quoted regression-test green;
        skill emits rm/git-rm commands for the user to run)
       ↓ quoted: 3 × (pass name + test count + exit 0)
[3/3] REVIEW    -> /dev-kit:review
       (3-dim per-diff: correctness + security + architecture)
       ↓ quoted: per-dim finding count + overall verdict
```

## Rules (no exceptions)

- MUST-L1: no phase 2 (prune) without a phase-1 (inspect) report.
- MUST-L2: every deletion must have a reproducible signal (orphan grep,
  dead-feature dependency check, or slop-pattern match). No "I think
  this is unused."
- MUST-L3: each phase ends with a quoted exit code + test count
  (or per-dim finding count for inspect/review) before the next phase
  starts. No "trust me" hand-offs.
- MUST-L4: no commented-out code, no `pass`-as-stub, no "kept for
  reference" leftovers. Every deletion lands clean — actually removed
  from disk and git, not commented out.
- MUST-NO-LOOP: phases are sequential gates, not a single retried
  cycle. Phase 2's 3 passes are themselves separate calls inside
  `build-prune`; do not collapse them here.
- If any phase is RED, stop. Surface the failing phase's quoted output
  to the user. Do not run subsequent phases on a red baseline.
- The skill never deletes files itself. Phase 2 emits the commands;
  the user runs them. This mirrors `feat-remove` discipline.
- Dependents block by default. If a deletion candidate has any
  importer/caller/test/doc reference, the skill refuses to proceed
  until the user acks the cascade (same as `feat-remove` MUST).

## Hook integration

| Hook | Mode |
|---|---|
| tdd-guard | ON (phase 2 mutates; tdd-guard passes if test deletions accompany) |
| bash-guard | ON (blocks destructive `rm -rf` etc.; the skill still surfaces commands for the user) |
| secret-scan | ON (PostToolUse) |
| slop-detector | ON (the deletion itself reduces slop; the report prose is checked) |
| stop-verify | ON -- quoted full-suite green required before declaring done |

## Output

- `.dev-kit/inspect-report.md` (phase 1 artifact)
- `.dev-kit/hand-off/prune-report.md` (this skill's own log:
  per-phase start/end timestamps, quoted exit codes, per-phase finding
  counts, the hand-off chain to `/dev-kit:ship`)
- For each deletion candidate: path + reason + dependents list +
  exact `rm` / `git rm` command for the user to run. No silent cascade.
- Per-phase emitted hand-off JSON via `state_codec.append_hand_off`
  (`build -> review -> ship` once all three phases are green)
- After all 3 phases green: a single quoted full-suite run
  (test count + exit code + duration) in the final report.

## Red flags

| Thought | Reality |
|---|---|
| "Run all 3 phases in one big cycle" | MUST-NO-LOOP violation. Each phase must gate the next on quoted output. |
| "Skip phase 1, I already know the slop" | MUST-L1 violation. The baseline is what makes phase 2's deletions verifiable. |
| "Phase 2 is enough, review is overhead" | L3 violation. The review catches what the prune pass missed (e.g., accidental prod-path delete). |
| "I'll just comment out the dead code instead of deleting" | L4 violation. Commented-out code is stub. Delete or keep, no third state. |
| "I'll silently cascade to dependents" | Block. Surface the list. The user must ack. |
| "The skill should `rm` for me" | No. The skill emits commands; the user runs them. Mirrors `feat-remove` discipline. |
| "Suite still passes after phase 2" | L3 violation. Quote the count. |
| "Phase 3 found a HIGH, but it's small, let's ship" | Stop. Re-run phase 2 or hand off to `/dev-kit:plan` for a structured fix. |

## Next step

- All 3 phases green + user has run the deletion commands ->
  `/dev-kit:ship` (release tag emit) or `/dev-kit:status` (HOTL
  visualization of the whole sweep).
- Any phase RED -> the failing phase is the deliverable. Fix the
  blocker (usually: re-run `build-prune` after the regression is fixed,
  or `/dev-kit:plan` to scope a structured fix for HIGH findings).
- For one named feature end-to-end, use `/dev-kit:feat-remove <feature>`.
- For pure refactoring (no deletion), use `/dev-kit:refactor`.
