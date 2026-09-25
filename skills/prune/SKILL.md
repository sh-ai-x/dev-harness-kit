---
name: prune
category: build
description: 0-arg slop-removal chain. One slash wraps inspect → 3-pass delete sweep → review. Gated phases for deleting AI slop and dead features (not refactoring).
alpha: analysis
when_to_use:
  - User types /dev-kit:prune
  - User types "remove AI slop" / "delete dead code" / "sweep the codebase for cruft"
  - Whole-pipeline deletion after a refactor PR — for refactoring use `/dev-kit:refactor`
  - For deleting one named feature end-to-end, use `/dev-kit:prune --target <feature>`
allowed-tools: Read Write Bash Glob Grep
disallowed-tools: Edit WebFetch
model: opus
user-invocable: true
---
> [← Skills index](../../README.md)

Whole-pipeline **deletion** sweep. `/dev-kit:inspect` baseline → 3-pass delete
via `lib.analysis_core.run_analysis(..., mode="delete", ...)` → `/dev-kit:review`.
Phases are **separate calls**, each gating the next on a quoted exit code +
test count. `prune` is project-wide; `prune --target <feature>` deletes one
named feature end-to-end. `prune` deletes; `/dev-kit:refactor` rewrites.

## 4 phases (separate calls)

0-arg: whole project. `<path>` narrows. `--target <feat>` switches to single-
feature deletion. No version-
gated preconditions (self-referential). Suite must run < 10 min. `--phase N`
re-runs one phase. `--dry-run` defaults ON for first pass.

```
[1/4] SWEEP       --target <feat>    (already exists; add --target flag)
       ↓ quoted: 3 × (pass name + test count + exit 0)
[2/4] DEPENDENTS   → block until user acks (NEW)
       ↓ quoted: dependents report path + user ack
[3/4] REPORT       → write .dev-kit/hand-off/prune-target-report.md (NEW)
       ↓ quoted: report path + finding count + verdict
[4/4] VERIFY       → run full suite, fail → mattpocock-skills:diagnosing-bugs (NEW)
       ↓ quoted: full suite exit code + test count
```

## Phase 1 — 3-pass deletion sweep

Sibling of the refactor cleanup pass (rewrites). `prune` *deletes*. **Iron Law.** No
deletion without reproducible signal + regression test.

```
[1/3] ORPHAN-CODE  -> exports with no callers, files with no importers, unreachable branches
[2/3] DEAD-FEATURE -> entire capabilities with no live users (unused env vars, deprecated paths)
[3/3] SLOP-PATTERN -> AI-tell patterns: defensive over-engineering, comment-as-narration, try/except pass blocks
```

One pass = one kind. Confirm regression test pass after each. The skill
**emits** `rm` / `git rm` commands to a report file. It never deletes files itself.

## Phase 2 — DEPENDENTS sweep

After Phase 1 finds deletion candidates, the skill invokes
`python3 -m lib.analysis_core --delete --target <feat>` (the
single-source CLI entry into the engine at `lib/analysis_core/__main__.py`)
to walk the call graph of every candidate and surface live importers /
callers / runtime references. Output is a DEPENDENTS block inside the
report that names each call site with file + line. **Phase blocks** until
the user explicitly acks each dependent line. Default behavior:
dependents block. Pass `--no-block` only when the user has signed off in
advance (e.g. via `--force`).

```
[2/4] DEPENDENTS   → python3 -m lib.analysis_core --delete --target <feat>
       → quoted: dependents report path + user ack per row
```

## Phase 3 — REPORT

Renders the merged finding set (Phase 1 candidates + Phase 2 dependent
annotations) into `.dev-kit/hand-off/prune-target-report.md`. Report shape
is fixed: per-finding block carries file, line, severity, confidence,
title, tldr, scenario, and a Fix line for the deletion command. Verdict
follows the engine's Healthy / Critical / Major drift / Minor drift scale.
For `--target <feat>` mode the report file is suffixed
`prune-target-<feat>-report.md` so multiple target sweeps don't clobber
each other.

```
[3/4] REPORT       → render_markdown + emit_suggested_diffs
       → quoted: report path + finding count + verdict
```

## Phase 4 — VERIFY

Run the full test suite (the project's standard runner — `pytest`,
`npm test`, `go test ./...`, etc.). On green, hand off to `/dev-kit:ship`
or `/dev-kit:status`. On red, the skill refuses to declare success and
routes to `mattpocock-skills:diagnosing-bugs` for systematic reproduction. No
deletion is final until the suite is green post-deletion.

```
[4/4] VERIFY       → <project test runner>
       → quoted: full suite exit code + test count
       → on red: mattpocock-skills:diagnosing-bugs
```

## `--target <feat>` flag

Narrows the sweep to a single named feature. Replaces the old
single-feature deletion flow. Resolution rules:

- `<feat>` MUST resolve to a phase name, a directory under `skills/`, or a
  Python module under `lib/`. Unresolvable names fail with exit 2.
- The sweep restricts scope (`paths=[<feat>-root]`) so off-feature findings
  are dropped at parse time (see `_is_in_scope` in `runner.py`).
- DEPENDENTS phase becomes mandatory — single-target deletions are more
  likely to have undeclared callers than project-wide sweeps.
- REPORT filename becomes `prune-target-<feat>-report.md`.
- The full suite (Phase 4) is required even when no candidates are
  deleted — `--target` mode runs Phase 4 unconditionally.

## Phase rules

MUST-L1: no phase 2 without a phase-1 report. MUST-L2: every deletion needs a reproducible signal. MUST-L3: each phase ends with quoted exit code + test count. MUST-L4: no commented-out code, no `pass`-as-stub. MUST-NO-LOOP: phases are sequential gates, not a retried cycle.

## Next step

All 4 phases green + user has run deletion commands -> `/dev-kit:ship` or `/dev-kit:status`. Any phase RED -> fix the blocker. For pure refactoring, `/dev-kit:refactor`.
