---
name: inspect
category: audit
description: 0-arg read-only code health audit. 8-dim fan-out (dead, dup, smell, overeng, overarch, cleancode, tokenbudget, slop) + --secrets/--slop aliases to the audit family (lib/analysis_core/dimensions.py).
alpha: analysis
when_to_use:
  - User types /dev-kit:inspect or /dev-kit:inspect --html
  - Pre-release hygiene sweep
  - Pre-step for /dev-kit:refactor or /dev-kit:prune (baseline report)
allowed-tools: Read Grep Glob Bash Agent
disallowed-tools: Write Edit
model: opus
user-invocable: true
---
> [← Skills index](../../README.md)
Read-only whole-codebase health sweep. Delegates to `lib.analysis_core.run_analysis(dimensions=group("inspect"), mode="read-only", paths=...)`. With `--secrets`, the skill passes `dimensions=["secret"]` (from the audit family) to the same engine; with `--slop`, `dimensions=["slop"]` (already in inspect family). Both bypass the inspect group default and run a single-dimension sweep. Engine owns registry, evidence schema, FP filter, verifier, renderer; this skill owns the parallel Agent fan-out and the markdown wrapper. **Iron Law.** Read-only. `disallowed-tools: Write Edit`.
## Scope

1. No positional arg -> whole project; `<path>` -> that subtree.
2. `--html` -> after the markdown artifact, run
   `python3 bin/dev-kit-report.py --project-root .` to render `.dev-kit/report.html`.
3. `--dim <name>` -> `dead | dup | smell | overeng | overarch | cleancode | tokenbudget | slop`.
4. `--secrets` / `--slop` -> aliases for `--dim secret` / `--dim slop` (lib/analysis_core/dimensions.py). Replaces the removed `/dev-kit:audit` slash.
5. Empty source set -> stop. >~40 files -> narrow with positional arg. Skip `.git/`, `node_modules/`, `dist/`, and generated min/pb files.
6. With `--secrets` (or `--dim secret`): do NOT apply the lockfile/generated-artifact exclusions — credential patterns can appear in lockfiles (private-registry URLs with embedded tokens) and generated artifacts. The secret scan must inspect every file.
## Fan-out + verify

Issue all Agent calls inside ONE assistant message so they run concurrently. Each: `subagent_type: "general-purpose"`, `model: "sonnet"`. Pass each expert its charter from `lib.analysis_core.dimensions` + the shared contract (`file, line, severity, confidence, failure_scenario, title, tldr, fix_hint`). Return a fenced `json` array. One verifier Agent returns `[{id, verdict: CONFIRMED|PLAUSIBLE|REJECTED, reason}]`; REJECTED are dropped.

The skill body owns the dedupe (on `file,line,theme`) + verifier + synthesize pipeline inline — the body collapses duplicates, applies the verifier verdict, and synthesizes the markdown report.

## Dimensions (charters live in `lib/analysis_core/dimensions.py`)

- **dead** — unused exports, unreachable branches, commented-out blocks (>3 lines), orphan config keys.
- **dup** — copy-paste of >= 5 near-identical lines across 2+ files, parallel class hierarchies.
- **smell** — long methods (>50 lines), long param lists (>4), deep nesting (>4), data clumps.
- **overeng** — interface with one implementer, speculative params, premature generalization, hot-path N+1.
- **overarch** — module-boundary leaks, premature layering, parallel hierarchies, circular imports.
- **cleancode** — SRP/DRY/KISS/YAGNI with evidence; vague names; bare except: pass; magic numbers.
- **tokenbudget** — file > 800 lines with low signal, dead-comment blocks, export/consumer skew.
- **slop** — dead else branches, hallucinated API calls, over-defensive try/except, AI-tell phrasing.
## Render

Append engine's markdown to `.dev-kit/inspect-report.md`. With `--html`, then
run `python3 bin/dev-kit-report.py --project-root .` and quote its exit code
and output path. No PR comments, no source edits. Verdict: `Critical` (>=1 HIGH) | `Major drift` (>=3 MED) | `Minor drift` | `Healthy`.

## Hand-off
| Dim | Target | Pass |
|---|---|---|
| dead | refactor cleanup pass | [1/4] |
| dup | refactor cleanup pass | [2/4] |
| smell | refactor cleanup pass | [3/4] |
| overeng | refactor cleanup pass | [3/4] |
| overarch | /dev-kit:plan -> build | full cycle |
| cleancode | refactor cleanup pass | [3/4] |
| tokenbudget | refactor cleanup pass | [1/4]+[3/4] |
| slop | /dev-kit:prune | deletion sweep |

Next: `/dev-kit:refactor` (rewrite), `/dev-kit:prune` (delete), `/dev-kit:plan` (HIGH > 0), `/dev-kit:review` (per-PR).
