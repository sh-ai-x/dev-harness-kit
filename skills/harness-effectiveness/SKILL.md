---
name: harness-effectiveness
category: eval
description: 0-arg harness-effectiveness report. Wraps `lib.harness_effectiveness.build_report` and prints the five-component (prevention / first-pass / recovery / learning / measurement-integrity) scorecard as JSON + a one-line status verdict. The measurement-integrity component also reports a nested submetric (issue #663) covering agent / model / provider swap behaviour.
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
| `measurement_integrity` | 0.15 | TraceLog event_id uniqueness, schema-version compliance, dedup; nested `stability` submetric (issue #663) reports agent / model / provider swap behaviour |

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

## Stability submetric (issue #663)

Nested under `components.measurement_integrity.submetrics.stability`. It is a
sixth dimension that is *not* a top-level weight — the five-component
`overall_score` formula is unchanged. The submetric carries:

- `coverage` (float 0..1) — minimum of the five dimension sub-coverage ratios
- `score` (`None` when coverage < 0.90 or evidence is missing; otherwise a
  float in [0, 100]). `None` is distinct from `0.0` — the reducer never
  collapses missing evidence into a zero score.
- `status` — one of `OK` / `DRIFT_WARNING` / `ROT` / `INSUFFICIENT_EVIDENCE`
- `findings` — list of one-line reasons (e.g. "agent/provider/model
  identity not recorded on any event")
- `evidence_event_ids` — event_ids the submetric saw, sorted + deduped
- `submetrics` — per-dimension breakdown:
  - `agent_identity_coverage`: fraction of events carrying agent / provider / model
  - `replay_compatibility`: fraction of events with monotonic timestamps
  - `agent_provider_neutrality`: fraction of events free of agent/provider/model keys in evidence_ref
  - `gate_portability`: fraction of distinct event_types with at least one neutral event
  - `contract_test_pass_rate`: fraction of `contract.test` events with outcome=`passed`

A new `INSUFFICIENT_EVIDENCE` constant is exported from
`lib.harness_effectiveness`; consumer code should compare against the
constant instead of hardcoding the string. `build_report` also bumps
`schema_version` from 1 → 2 so consumers can detect the new submetric
without breaking the 5-component contract.

## Output

The full reducer JSON is printed to stdout (one line per field, pretty-printed). Exit code is 0 on every successful invocation — this skill does not gate; the gating decision belongs to the caller. The one-line status verdict is **always** present in the JSON; callers that want a hard fail condition can check `status == "ROT"` and exit 1 themselves.

```
$ /dev-kit:harness-effectiveness
{
  "schema_version": 2,
  "contract_version": "harness-effectiveness-v1",
  "event_count": 81,
  "overall_score": null,
  "status": "INSUFFICIENT_EVIDENCE",
  "components": {
    "prevention_quality":    { "score": null, ..., "findings": ["ground_truth label missing for guard actions"] },
    "first_pass_quality":    { "score": null, ..., "findings": ["no write with first verification evidence"] },
    "recovery_quality":      { "score": null, ..., "findings": ["no verification errors observed"] },
    "learning_quality":      { "score": null, ..., "findings": ["comparable treatment and control cohorts missing"] },
    "measurement_integrity": {
      "score": null,
      "submetrics": {
        "schema_completeness": ...,
        "attribution_completeness": ...,
        "dedupe_integrity": ...,
        "event_coverage": ...,
        "stability": {
          "coverage": 0.0,
          "score": null,
          "status": "INSUFFICIENT_EVIDENCE",
          "findings": ["agent/provider/model identity not recorded on any event"],
          "evidence_event_ids": ["e1", "e2", ...],
          "submetrics": { ... }
        }
      }
    }
  }
}
```

When the harness emits the required evidence, the table fills in — no skill or lib change needed.

## Implementation

The skill body is the reducer. There is no algorithm loop, no LLM call, no fixture. The contract is the deterministic one already documented at `lib/harness_effectiveness.py:build_report`; the `alpha: enforcement` declaration pins that contract as Iron Law L6 (the part the model cannot self-impose).

The slash command (canonical `commands/harness-effectiveness.md`) is a thin wrapper around this reducer. Both `bin/install-commands.sh --claude-only` and `bin/install-commands.sh --codex-only` install the slash to `.claude/commands/` and `.codex/commands/` respectively.

## Backward-compat

- `lib/harness_effectiveness.build_report` is unchanged for the 5-component
  contract: `COMPONENT_WEIGHTS` still sums to 1.0, the `overall_score` formula
  is the same, and `components` / `overall_score` / `status` / `event_count`
  / `contract_version` still exist. `schema_version` bumps from 1 → 2 to
  advertise the nested `stability` submetric; consumers that ignore unknown
  versions continue to work unchanged. Callers that import it directly still work.
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
- `tests/test_harness_stability.py` — 14 hermetic tests covering the stability
  submetric (issue #663): model swap, provider swap, replay compatibility,
  missing evidence, and backward compat.
- `/dev-kit:evaluate` — runs the 5-component reducer + the 12-case judge pass in one report. Use that when you want the full picture; use this skill when you want the reducer in isolation.
- `commands/harness-effectiveness.md` — the slash command registered by `bin/install-commands.sh`.

## Next step

After a passing run (no `ROT` components), hand off to `/dev-kit:status` to confirm the eval cycle is green. After a `ROT` component, triage via `/dev-kit:build-debug` on the affected producer (the reducer emits the `finding` string; the producer is the TraceLog emitter responsible for that evidence class). For the full eval pass instead, use `/dev-kit:evaluate` unchanged.