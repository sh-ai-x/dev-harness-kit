> [← Skills index](README.md) · [Project README](../../README.md)

# `harness-effectiveness`

**Category:** `eval` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:harness-effectiveness` (human-invoked)

`harness-effectiveness` is the standalone, sub-second, zero-API-spend wrapper around `lib/harness_effectiveness.build_report` — the same deterministic 5-component reducer that `/dev-kit:evaluate` already embeds at the bottom of its report. The wrapper exists so operators can spot-check the metric mid-session without paying for the 12-case LLM judge pass.

## When to use it

- The user types `/dev-kit:harness-effectiveness` for the standalone reducer (no judges).
- `/dev-kit:evaluate` is overkill — the operator only wants the 5-component table, not the full 12-case report.
- A harness change has landed and the operator wants to see whether the effectiveness coverage moved before opening the next eval run.
- `.dev-kit/eval-report.md` shows `INSUFFICIENT_EVIDENCE` for the effectiveness table and the operator wants to see the raw JSON behind each `findings` string.

## How it works

The skill is 0-arg and delegates entirely to `lib/harness_effectiveness.build_report(root)`. The reducer reads the worktree's TraceLog events (`lib/trace_log.py:read_events`) and emits the same five components `lib/eval_runner.py` already uses — `prevention_quality`, `first_pass_quality`, `recovery_quality`, `learning_quality`, `measurement_integrity`. The output is pretty-printed JSON to stdout with the reducer's `status` field as the one-line verdict; missing evidence surfaces as `score: null` + `status: INSUFFICIENT_EVIDENCE` per component, **never** as a fabricated zero or pass.

## The five components

| Component | Weight | What it scores |
|---|---:|---|
| `prevention_quality` | 0.20 | guard block-rate vs. ground-truth-labelled guard events |
| `first_pass_quality` | 0.20 | write → first-verification-pass rate |
| `recovery_quality` | 0.25 | median iterations to recover from a verification error |
| `learning_quality` | 0.20 | treatment-vs-control cohort divergence after guard intervention |
| `measurement_integrity` | 0.15 | TraceLog `event_id` uniqueness, schema-version compliance, dedup |

`overall_score` is `null` when **any** component is `null`. Otherwise it is the weighted sum of component scores. The full spec lives at [`eval/rubrics/harness-effectiveness.yaml`](../../eval/rubrics/harness-effectiveness.yaml) and the design rationale is at [`docs/proposals/harness-effectiveness/00-index.html`](../proposals/harness-effectiveness/00-index.html).

## Invocation

```bash
# Default — print full JSON to stdout:
/dev-kit:harness-effectiveness

# Slice a single component or the status line:
/dev-kit:harness-effectiveness | jq '.status, .overall_score'
/dev-kit:harness-effectiveness | jq '.components.first_pass_quality'
```

Exit code is 0 on every successful invocation. This skill does not gate; the caller checks `status == "ROT"` (per component) or `overall_score < threshold` if it wants a hard fail.

## Difference vs `/dev-kit:evaluate`

| | `/dev-kit:evaluate` | `/dev-kit:harness-effectiveness` |
|---|---|---|
| Runs the 5-component reducer | ✅ (default mode) | ✅ (only thing it runs) |
| Runs the 12-case LLM judge pass | ✅ (default mode) | ❌ |
| Writes `.dev-kit/eval-report.md` | ✅ | ❌ |
| Wall-clock cost | ~30 s (LLM judges dominate) | sub-100 ms (deterministic only) |
| API spend | yes (12 judge calls) | none |
| Cross-validate (3-judge variance gate) | ✅ | ❌ (deterministic — no judges to disagree) |

Use `/dev-kit:evaluate` for the full report (judges + 5-component); use `/dev-kit:harness-effectiveness` to iterate on the reducer in isolation.

## When NOT to use it

- The user wants the 12-case judge pass — that's `/dev-kit:evaluate` (or `/dev-kit:evaluate --harness-quality` to register the harness-quality rubric).
- The user wants harness-quality + os-quality rubrics on top of the 5 components — also `/dev-kit:evaluate`.
- The TraceLog is empty. The reducer will return `INSUFFICIENT_EVIDENCE` for all five components, which is correct but not actionable. Run a build first (`/dev-kit:build`) so the harness emits the required evidence.

## Related

- [`skills/harness-effectiveness/SKILL.md`](../../skills/harness-effectiveness/SKILL.md) — full skill contract.
- [`lib/harness_effectiveness.py`](../../lib/harness_effectiveness.py) — the reducer implementation.
- [`lib/trace_log.py`](../../lib/trace_log.py) — the TraceLog event source the reducer consumes.
- [`eval/rubrics/harness-effectiveness.yaml`](../../eval/rubrics/harness-effectiveness.yaml) — the spec the components conform to.
- [`eval/prompts/judge-harness-effectiveness.md`](../../eval/prompts/judge-harness-effectiveness.md) — the LLM-judge prompt used only by `/dev-kit:evaluate --harness-quality`.
- [`docs/proposals/harness-effectiveness/00-index.html`](../proposals/harness-effectiveness/00-index.html) — the design proposal (the canonical "why this metric").
- [`docs/skills/evaluate.md`](evaluate.md) — the parent eval skill; the harness-effectiveness table there is the same reducer.
- `commands/harness-effectiveness.md` — the slash command source. `bin/install-commands.sh` already syncs this to `.claude/commands/` + `.codex/commands/` at SessionStart.