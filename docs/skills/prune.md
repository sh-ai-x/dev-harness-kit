> [← Skills index](README.md) · [Project README](../../README.md)

# `prune`

**Category:** `build` · **Alpha:** `analysis` · **Invocation:** `/dev-kit:prune` (human-invoked)

`prune` is the whole-pipeline **deletion** chain: one slash command wraps an `/dev-kit:inspect` baseline, a 3-pass delete sweep run via `lib.analysis_core.run_analysis(..., mode="delete", ...)`, and a final `/dev-kit:review`. It is project-wide by default, and `prune --target <feature>` narrows it to a single named feature deleted end to end. It exists as a distinct skill from `/dev-kit:refactor` because deletion and rewriting need different gates: `prune` deletes, `refactor` rewrites, and each phase here is a separate call gated on a quoted exit code plus test count before the next phase runs.

## When to use it

- The user types `/dev-kit:prune`.
- The user's language is "remove AI slop" / "delete dead code" / "sweep the codebase for cruft".
- The user wants a whole-pipeline deletion sweep after a refactor PR — for refactoring itself, use `/dev-kit:refactor` instead.
- The user wants to delete one named feature end to end, via `/dev-kit:prune --target <feature>`.

## How it works

`prune` runs four ordered phases, each a separate call:

1. **SWEEP (Phase 1 — 3-pass deletion sweep).** Sibling of the refactor cleanup pass (which rewrites) — `prune` deletes. Iron Law: no deletion without a reproducible signal plus a regression test. Three passes, one kind per pass, each confirmed by a green regression test:
   - `[1/3] ORPHAN-CODE` — exports with no callers, files with no importers, unreachable branches.
   - `[2/3] DEAD-FEATURE` — entire capabilities with no live users (unused env vars, deprecated paths).
   - `[3/3] SLOP-PATTERN` — AI-tell patterns: defensive over-engineering, comment-as-narration, `try/except pass` blocks.
   The skill only **emits** `rm` / `git rm` commands to a report file; it never deletes files itself.

2. **DEPENDENTS (Phase 2).** After Phase 1 finds deletion candidates, the skill invokes `python3 -m lib.analysis_core --delete --target <feat>` — the single-source CLI entry into the engine at `lib/analysis_core/__main__.py` — to walk the call graph of every candidate and surface live importers, callers, and runtime references. The findings render as a DEPENDENTS block in the report, naming each call site by file and line. This phase blocks until the user explicitly acknowledges each dependent line; the default is to block, and `--no-block` is only appropriate when the user has already signed off in advance (e.g. via `--force`).

3. **REPORT (Phase 3).** Renders the merged finding set (Phase 1 candidates plus Phase 2 dependent annotations) into `.dev-kit/hand-off/prune-target-report.md`. Each finding block carries file, line, severity, confidence, title, tl;dr, scenario, and a Fix line with the deletion command. The verdict follows the engine's Healthy / Critical / Major drift / Minor drift scale. In `--target <feat>` mode the report file is suffixed `prune-target-<feat>-report.md` so multiple target sweeps don't clobber each other.

4. **VERIFY (Phase 4).** Runs the full test suite (the project's standard runner — `pytest`, `npm test`, `go test ./...`, etc.). On green, hand off to `/dev-kit:ship` or `/dev-kit:status`. On red, the skill refuses to declare success and routes to `mattpocock-skills:diagnosing-bugs` for systematic reproduction — no deletion is final until the suite is green post-deletion. In `--target` mode, Phase 4 runs unconditionally even when no candidates were deleted.

`--target <feat>` resolution rules: `<feat>` must resolve to a phase name, a directory under `skills/`, or a Python module under `lib/` — unresolvable names fail with exit 2. The sweep restricts scope (`paths=[<feat>-root]`) so off-feature findings are dropped at parse time (see `_is_in_scope` in `runner.py`). The DEPENDENTS phase becomes mandatory in this mode, since single-target deletions are more likely to have undeclared callers than project-wide sweeps.

Phase rules: MUST-L1 forbids Phase 2 without a Phase 1 report; MUST-L2 requires every deletion to have a reproducible signal; MUST-L3 requires each phase to end with a quoted exit code and test count; MUST-L4 forbids commented-out code or `pass`-as-stub; MUST-NO-LOOP treats phases as sequential gates, not a retried cycle.

## Usage

```bash
/dev-kit:prune [<path>] [--target <feature>] [--phase N] [--dry-run] [--no-block]
```

| Flag | Effect |
|---|---|
| (0-arg) | Sweeps the whole project. |
| `<path>` | Narrows the sweep to a subpath. |
| `--target <feat>` | Switches to single-feature deletion. Must resolve to a phase name, a `skills/` directory, or a `lib/` module, or it fails with exit 2. |
| `--phase N` | Re-runs one phase only. |
| `--dry-run` | Defaults ON for the first pass. |
| `--no-block` | Skips the DEPENDENTS acknowledgment gate; use only when the user has already signed off (e.g. via `--force`). |

The full suite must run in under 10 minutes. There are no version-gated preconditions — the skill is self-referential.

## Output

- `.dev-kit/hand-off/prune-target-report.md` (or `prune-target-<feat>-report.md` in `--target` mode): per-finding blocks (file, line, severity, confidence, title, tl;dr, scenario, Fix line) plus a Healthy / Critical / Major drift / Minor drift verdict.
- Quoted evidence at each phase boundary: 3× (pass name + test count + exit 0) for Phase 1, dependents report path + per-row user ack for Phase 2, report path + finding count + verdict for Phase 3, and full suite exit code + test count for Phase 4.

## Related

- [refactor](refactor.md) — the rewrite counterpart; `prune` deletes, `refactor` rewrites.
- [refactor](refactor.md) — rewrite pipeline; explicitly contrasted as "prune deletes."
- `mattpocock-skills:diagnosing-bugs` — where a red Phase 4 verify routes for systematic reproduction (replaces the deleted `/dev-kit:build-debug`).
- `lib/analysis_core.runner.run_analysis` (`mode="delete"`) — the shared engine backing both Phase 1 and Phase 2.
- `python3 -m lib.analysis_core --delete --target <feat>` — the Phase 2 dependents walker (CLI entry into `lib/analysis_core/__main__.py`).
- `/dev-kit:ship`, `/dev-kit:status` — the next steps once all 4 phases are green.

---
*Source: [`skills/prune/SKILL.md`](../../skills/prune/SKILL.md)*
