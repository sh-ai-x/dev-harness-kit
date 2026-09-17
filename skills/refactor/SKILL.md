---
name: refactor
category: build
description: 0-arg cleanup chain. One slash wraps inspect -> cleanup -> review. 3 gated phases with quoted exit codes between each.
alpha: analysis
when_to_use:
  - User types /dev-kit:refactor
  - User types "clean up the codebase" / "refactor everything" / "simplify the whole project"
  - Whole-pipeline cleanup after a refactor PR
  - For actually deleting slop/dead features use `/dev-kit:prune`
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: Edit WebFetch
model: sonnet
user-invocable: true
---
> [← Skills index](../../README.md)

Whole-pipeline refactor. `/dev-kit:inspect` baseline → a four-pass cleanup
(dead → dup → naming → coverage) → `/dev-kit:review`. Phases are **separate
calls**, each gating the next on a quoted exit code + test count.
This skill does **not delete** features; for deletion use `/dev-kit:prune`.
For one named feature end-to-end, use `prune --target <feature>`.

## Optional Linear preflight

At the start of a new refactor task, invoke `/dev-kit:linear` once when Linear
is enabled or available. Continue normally on `LINEAR_SKIP` or an implicit
`LINEAR_ERROR`; do not invoke it per phase. See `skills/linear/SKILL.md` for
the reconciliation contract.

## 3 phases (separate calls)

0-arg: whole project. `<path>` narrows. No version-gated preconditions
(self-referential). Suite must run < 10 min. `--phase N` re-runs one phase.

```
[1/3] INSPECT   -> /dev-kit:inspect  (.dev-kit/inspect-report.md)
       ↓ quoted: report path + verdict + finding count
[2/3] REFACTOR  -> cleanup pass           (dead → dup → naming → coverage)
       ↓ quoted: 4 × (pass name + test count + exit 0)
[3/3] REVIEW    -> /dev-kit:review  (correctness + security + architecture)
       ↓ quoted: per-dim finding count + verdict
```

## Phase rules

MUST-L1: no phase 2 without a phase-1 report. MUST-L3: each phase ends with quoted exit code + test count (or per-dim finding count). MUST-L4: no commented-out code, no `pass`-as-stub, no "we'll fix this later" leftovers. MUST-NO-LOOP: phases are sequential gates, not a retried cycle. If any phase is RED, stop. The skill itself never edits source files; phase 2 owns the mutations; phases 1 and 3 are read-only.

## Hooks

`tdd-guard` ON (phase 2 mutates), `bash-guard` ON, `secret-scan` ON,
`slop-detector` ON, `stop-verify` ON — quoted full-suite green required before declaring done.

## Next step

All 3 phases green -> `/dev-kit:ship` or `/dev-kit:status`. Any phase RED -> the failing phase is the deliverable. Fix the blocker (re-run the cleanup pass after regression fix, or `/dev-kit:plan` to scope a structured fix for HIGH findings). For project-wide deletion of slop/dead features, use `/dev-kit:prune`.
