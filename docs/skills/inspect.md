> [← Skills index](README.md) · [Project README](../../README.md)

# `inspect`

**Category:** `audit` · **Alpha:** `analysis` · **Invocation:** `/dev-kit:inspect` (human-invoked)

`inspect` is a read-only, whole-codebase health sweep that fans out across 8 analysis dimensions in parallel and renders a single markdown report. It delegates to `lib.analysis_core.run_analysis(dimensions=group("inspect"), mode="read-only", paths=...)`, which owns the registry, evidence schema, false-positive filter, verifier, and renderer; the skill itself owns the parallel Agent fan-out and the markdown wrapper. Its Iron Law is strict read-only: `Write` and `Edit` are disallowed.

## When to use it

- The user types `/dev-kit:inspect`.
- The user wants a pre-release hygiene sweep.
- The user wants a baseline report as a pre-step for `/dev-kit:refactor` or `/dev-kit:prune`.

## How it works

**Scope.** With no positional argument the sweep covers the whole project; a `<path>` argument narrows it to that subtree. `--dim <name>` restricts to a single dimension (`dead | dup | smell | overeng | overarch | cleancode | tokenbudget | slop | secret`). If the resulting source set is empty, the skill tells the user and stops; if it is larger than roughly 40 files, it asks the user to narrow with a positional argument. `.git/`, `node_modules/`, `dist/`, and generated files (`.pb.go`, `.min.js`, `.min.css`) are always skipped. **With `--secrets` (or `--dim secret`): lockfiles are NOT excluded** — credential patterns can appear in lockfiles (private-registry URLs with embedded tokens), so the secret scan must inspect every file.

**Fan-out + verify.** All Agent calls are issued inside one assistant message so they run concurrently. Each is `subagent_type: "general-purpose"`, `model: "sonnet"`, and is passed its charter from `lib.analysis_core.dimensions` plus the shared evidence contract (`file, line, severity, confidence, failure_scenario, title, tldr, fix_hint`); each returns a fenced JSON array of findings. A single verifier Agent then returns `[{id, verdict: CONFIRMED|PLAUSIBLE|REJECTED, reason}]` for every finding, and REJECTED findings are dropped. The skill body itself owns deduping on `file,line,theme`, applying the verifier verdict, and synthesizing the final markdown report from the raw per-dimension findings.

**Dimensions** (charters live in `lib/analysis_core/dimensions.py`):

- `dead` — unused exports, unreachable branches, commented-out blocks (>3 lines), orphan config keys.
- `dup` — copy-paste of ≥5 near-identical lines across 2+ files, parallel class hierarchies.
- `smell` — long methods (>50 lines), long param lists (>4), deep nesting (>4), data clumps.
- `overeng` — interface with one implementer, speculative params, premature generalization, hot-path N+1.
- `overarch` — module-boundary leaks, premature layering, parallel hierarchies, circular imports.
- `cleancode` — SRP/DRY/KISS/YAGNI with evidence; vague names; bare `except: pass`; magic numbers.
- `tokenbudget` — file > 800 lines with low signal, dead-comment blocks, export/consumer skew.
- `slop` — dead else branches, hallucinated API calls, over-defensive try/except, AI-tell phrasing.

**Render.** The engine's markdown is appended to `.dev-kit/inspect-report.md`. There are no PR comments and no source edits. The overall verdict is `Critical` (≥1 HIGH finding), `Major drift` (≥3 MED), `Minor drift`, or `Healthy`.

## Usage

```bash
/dev-kit:inspect [<path>] [--dim dead|dup|smell|overeng|overarch|cleancode|tokenbudget|slop|secret] [--secrets] [--slop]
```

## Output

`.dev-kit/inspect-report.md` — a markdown report with a per-dimension breakdown and an overall verdict (`Critical` / `Major drift` / `Minor drift` / `Healthy`).

The hand-off from each dimension to a downstream skill/phase:

| Dim | Target | Pass |
|---|---|---|
| dead | refactor cleanup pass | [1/4] |
| dup | refactor cleanup pass | [2/4] |
| smell | refactor cleanup pass | [3/4] |
| overeng | refactor cleanup pass | [3/4] |
| overarch | `/dev-kit:plan` → build | full cycle |
| cleancode | refactor cleanup pass | [3/4] |
| tokenbudget | refactor cleanup pass | [1/4]+[3/4] |
| slop | `/dev-kit:prune` | deletion sweep |

## Flags

- `--secrets` — alias for `--dim secret`. AWS / Anthropic / GitHub / Slack / PEM / embedded URI patterns. Replaces the removed `/dev-kit:audit --secrets-only`.
- `--slop` — alias for `--dim slop`. KO+EN banned phrases + structure bank. Replaces the removed `/dev-kit:audit --slop-only`.

## Related

- `/dev-kit:refactor` — rewrite pipeline for findings.
- `/dev-kit:prune` — deletion pipeline, particularly for `slop` findings.
- `/dev-kit:plan` — full re-plan when HIGH findings exceed 0.
- `/dev-kit:review` — per-PR counterpart to this whole-codebase sweep.
- `lib/analysis_core/dimensions.py` — dimension charters and shared engine.

---
*Source: [`skills/inspect/SKILL.md`](../../skills/inspect/SKILL.md)*
