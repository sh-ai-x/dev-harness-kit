---
name: harness-effectiveness
category: eval
description: 0-arg harness-effectiveness report. Wraps `lib.harness_effectiveness.build_report` and prints the five-component (prevention / first-pass / recovery / learning / measurement-integrity) scorecard as JSON + a one-line status verdict.
when_to_use:
  - User types /dev-kit:harness-effectiveness
  - Operator wants the 5-component metric without running the full 12-case `/dev-kit:evaluate` judge pass
  - After a Phase 3 batch lands, to inspect effectiveness in isolation before the full eval
  - When `.dev-kit/eval-report.md` shows `INSUFFICIENT_EVIDENCE` for the effectiveness table and the operator wants to see the raw JSON behind each finding
allowed-tools: Read Bash
disallowed-tools: Edit Write
model: sonnet
disable-model-invocation: false
user-invocable: true
alpha: enforcement
---

# /dev-kit:harness-effectiveness — workflow-effectiveness scorecard

## What it does

Invokes the deterministic `lib.harness_effectiveness.build_report(root)` reducer and prints the resulting JSON. The reducer consumes structured TraceLog events (guard actions, first-write verification, repair events, control/treatment cohorts, measurement-integrity signals) already emitted by the harness; **no LLM judge runs**, no transcript replay, no case fixture. Missing evidence is reported as `INSUFFICIENT_EVIDENCE` per component — it is never converted to a passing or zero score.

This is the standalone counterpart to the table that `/dev-kit:evaluate` embeds at the bottom of `.dev-kit/eval-report.md`. The difference is intent: `/dev-kit:evaluate` runs the 12-case judge pass + the 5-component report together; `/dev-kit:harness-effectiveness` runs only the 5-component reducer (cheap, sub-second, no API spend).

## Inputs (resolved at runtime, NOT user args)

| Variable | Source |
|---|---|
| `ROOT` | `Path(".").resolve()` — the worker's current worktree |
| `EVENTS` | `lib.trace_log.read_events(root)` — every JSONL event the harness has emitted into the worktree's TraceLog |
| `COMPONENT_WEIGHTS` | `lib.harness_effectiveness.COMPONENT_WEIGHTS` — single source of truth |

No flags. The slash is 0-arg by design.

## Five components

| Component | Weight | What it scores |
|---|---:|---|
| `prevention_quality` | 0.20 | guard block-rate vs. ground-truth-labelled guard events |
| `first_pass_quality` | 0.20 | write → first-verification-pass rate |
| `recovery_quality` | 0.25 | median iterations to recover from a verification error |
| `learning_quality` | 0.20 | treatment-vs-control cohort divergence after guard intervention |
| `measurement_integrity` | 0.15 | TraceLog event_id uniqueness, schema-version compliance, dedup |

Each component returns:
```
{
  "score": <float|null>,            # null when coverage < 0.90 or no evidence
  "weight": <float>,                # from COMPONENT_WEIGHTS
  "status": "OK" | "DRIFT_WARNING" | "ROT" | "INSUFFICIENT_EVIDENCE",
  "coverage": <float 0..1>,
  "submetrics": { ... },
  "evidence_event_ids": [ ... ],
  "findings": [ "<one-line reason>" ],
  "config_version": "harness-effectiveness-v1"
}
```

`overall_score` is `null` when **any** component is `null` (i.e. when **any** component reports `INSUFFICIENT_EVIDENCE`). Otherwise it is the weighted sum of component scores.

## Output

The full reducer JSON is printed to stdout (one line per field, pretty-printed). Exit code is 0 on every successful invocation — this skill does not gate; the gating decision belongs to the caller. The one-line status verdict is **always** present in the JSON; callers that want a hard fail condition can check `status == "ROT"` and exit 1 themselves.

```
$ /dev-kit:harness-effectiveness
{
  "schema_version": 1,
  "contract_version": "harness-effectiveness-v1",
  "event_count": 81,
  "overall_score": null,
  "status": "INSUFFICIENT_EVIDENCE",
  "components": {
    "prevention_quality":    { "score": null, ..., "findings": ["ground_truth label missing for guard actions"] },
    "first_pass_quality":    { "score": null, ..., "findings": ["no write with first verification evidence"] },
    "recovery_quality":      { "score": null, ..., "findings": ["no verification errors observed"] },
    "learning_quality":      { "score": null, ..., "findings": ["comparable treatment and control cohorts missing"] },
    "measurement_integrity": { "score": null, ..., "findings": ["duplicate event_id"] }
  }
}
```

When the harness emits the required evidence, the table fills in — no skill or lib change needed.

## Implementation

The skill body is the reducer. There is no algorithm loop, no LLM call, no fixture. The contract is the deterministic one already documented at `lib/harness_effectiveness.py:build_report`; the `alpha: enforcement` declaration pins that contract as Iron Law L6 (the part the model cannot self-impose).

The slash command (canonical `commands/harness-effectiveness.md`) is a thin wrapper around this reducer. Both `bin/install-commands.sh --claude-only` and `bin/install-commands.sh --codex-only` install the slash to `.claude/commands/` and `.codex/commands/` respectively.

## Backward-compat

- `lib/harness_effectiveness.build_report` is unchanged. Callers that import it directly still work.
- `/dev-kit:evaluate` keeps emitting the same embedded table — this skill is an additive shortcut, not a replacement.
- Existing `tests/test_harness_effectiveness.py` continues to cover the reducer; no test renames.

## Forbidden shortcuts

- ❌ Filling `INSUFFICIENT_EVIDENCE` components with `0.0` so `overall_score` renders a number.
- ❌ Caching the reducer output between calls — every invocation re-reads TraceLog from disk (the events may have changed since the last call).
- ❌ Adding `--json` / `--pretty` flags — the reducer already returns pretty-printable JSON; the caller pipes through `jq` if it wants to slice.
- ❌ Writing into `.dev-kit/eval-report.md`. The reducer does not touch any report file; only `/dev-kit:evaluate` writes that.

## Hook alignment

- `stop-verify=ON` — every "effectiveness is healthy" claim must be backed by the quoted `overall_score` + per-component `status` lines from the JSON (MUST-L3).
- `secret-scan=ON` — unchanged; this skill does not write.
- `slop-detector=ON` — unchanged.
- `worktree-guard=ON` — reads only; no Edit/Write, so no guard trigger.

## Output language

All stdout/stderr messages in **English only**.

## Related

- `lib/harness_effectiveness.py` — `build_report` (the reducer) + the five private `_prevention` / `_first_pass` / `_recovery` / `_learning` / `_integrity` component builders.
- `lib/trace_log.py` — `read_events` + `validate_event` (the schema the reducer depends on).
- `eval/rubrics/harness-effectiveness.yaml` — the spec the components conform to.
- `eval/prompts/judge-harness-effectiveness.md` — the LLM-judge prompt used only by `/dev-kit:evaluate --harness-quality` (NOT used here — this skill is reducer-only).
- `tests/test_harness_effectiveness.py` — 137 hermetic tests covering the reducer.
- `/dev-kit:evaluate` — runs the 5-component reducer + the 12-case judge pass in one report. Use that when you want the full picture; use this skill when you want the reducer in isolation.
- `commands/harness-effectiveness.md` — the slash command registered by `bin/install-commands.sh`.

## Next step

After a passing run (no `ROT` components), hand off to `/dev-kit:status` to confirm the eval cycle is green. After a `ROT` component, triage via `/dev-kit:build-debug` on the affected producer (the reducer emits the `finding` string; the producer is the TraceLog emitter responsible for that evidence class). For the full eval pass instead, use `/dev-kit:evaluate` unchanged.