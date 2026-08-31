# Harness-Effectiveness — Stability Submetric

The `harness_effectiveness` reducer (`lib/harness_effectiveness.py`) gained a
nested **stability** submetric under `components.measurement_integrity.submetrics.stability`
in issue #663. This page documents its contract so operators can read the new
field without diffing the source.

The stability submetric is intentionally **not** a 6th top-level component.
Adding it as a top-level entry would shift `COMPONENT_WEIGHTS` and change the
5-component `overall_score` formula that every existing consumer reads. As a
nested submetric it is opt-in: consumers that don't know about
`schema_version: 2` continue to render the legacy 5-component table unchanged.

## Where to find it

```json
{
  "schema_version": 2,
  "components": {
    "measurement_integrity": {
      "coverage": 0.83,
      "score": 91.0,
      "status": "OK",
      "submetrics": {
        "stability": {
          "coverage": 0.42,
          "score": null,
          "status": "INSUFFICIENT_EVIDENCE",
          "submetrics": {
            "agent_identity_coverage":   {"value": 0.0,   "evidence_event_ids": []},
            "replay_compatibility":      {"value": 100.0, "evidence_event_ids": ["evt_abc123", "evt_def456"]},
            "agent_provider_neutrality": {"value": 0.0,   "evidence_event_ids": []},
            "gate_portability":          {"value": 0.0,   "evidence_event_ids": []},
            "contract_test_pass_rate":   {"value": null,  "evidence_event_ids": []}
          },
          "findings": [
            "contract_test_pass_rate=0.0 (no contract.test events)"
          ],
          "evidence_event_ids": ["evt_abc123", "evt_def456"]
        }
      },
      "findings": [...],
      "evidence_event_ids": [...]
    }
  }
}
```

Consumers that ignore `schema_version` continue to work. Consumers that opt
into `schema_version >= 2` can read the new submetric and ignore it without
breaking the legacy 5-component `overall_score`.

## The five dimensions

Each dimension is rendered as its own key under `submetrics.stability.submetrics`:

| Dimension | What it measures | Inputs |
|---|---|---|
| `agent_identity_coverage` | Fraction of events carrying any of `agent`, `provider`, `model` identity. | top-level event identity fields |
| `replay_compatibility` | Events are recorded with monotonic timestamps so two reducers reading the same JSONL produce the same verdict. | `ts` field ordering |
| `agent_provider_neutrality` | Fraction of `evidence_ref` payloads whose keys contain no `model` / `provider` / `agent` substring. | `evidence_ref` keys (heuristic) |
| `gate_portability` | Fraction of distinct `event_type`s with at least one neutral event (gate fires without needing identity). | per-event-type neutrality |
| `contract_test_pass_rate` | Fraction of `contract.test` events with `outcome=passed`. | `contract.test` events |

### Identity and neutrality are independent

`agent_identity_coverage` and `agent_provider_neutrality` measure two
different properties and must both be satisfiable at once:

- **Identity coverage** is *provenance*: do we know which agent, provider
  and model produced this event? More is better — it is what makes a
  cross-agent comparison possible at all.
- **Neutrality** is *portability*: does the event's `evidence_ref` name a
  specific agent/provider/model, such that a gate verdict computed from
  it would change under an agent swap? Less coupling is better.

A fully-stamped corpus whose `evidence_ref` payloads carry no
agent/provider/model keys therefore scores 100 on **both**.

> **Regression guard.** `_stability()` previously counted an event as
> neutral only when it had *no* identity fields *and* no coupling keys.
> That made the two dimensions mutually exclusive, and because stability
> coverage is `min()` over all five dimensions the score was unreachable
> in both directions: stamping identity drove neutrality (and, through
> `event_type_neutral`, `gate_portability`) to 0%, while omitting it drove
> identity coverage to 0%. Neutrality is now computed from `evidence_ref`
> coupling alone, matching the table above. Pinned by
> `test_stability_scores_when_identity_is_present` and
> `test_neutrality_drops_when_evidence_ref_couples_to_agent`.

## Producing the evidence automatically

The three signals below are emitted by the harness itself, so a normal
CI run populates the submetric without any per-producer wiring.

| Signal | Producer | Notes |
|---|---|---|
| Identity fields | `lib.trace_log.append_event` | Auto-stamps `agent`/`provider`/`model` from `DEV_KIT_AGENT` / `DEV_KIT_PROVIDER` / `DEV_KIT_MODEL` (defaults `""` / `""` / `""` — empty defaults so the reducer treats unset as "no identity recorded" and a non-Claude runner is NOT silently mis-attributed to claude-code). Uses `setdefault`, so an explicit value passed by the caller always wins. |
| `contract.test` | `tests/conftest.py` | `pytest_sessionfinish` appends one event per run. **CI-gated** (`CI=true`) so local `pytest` runs do not inflate a developer's trace log. The filename must stay `conftest.py` — pytest auto-registers hooks from no other name. |
| `ground_truth` on guard events | `hooks/lib/payload-parse.sh::emit_guard_event` | Defaults to `"unknown"` for every outcome (no claim about correctness — a trigger-mirroring default would make the prevention_quality reducer trivially score 100% precision/recall against the same hook that emits the event). The reducer skips `unknown` events, so the metric only scores guards the operator has explicitly classified via `DEV_KIT_GROUND_TRUTH`. Override values are validated against the `unsafe` / `legitimate` / `pending` / `unknown` allowlist; anything else is rejected. |

### Who actually sets `DEV_KIT_AGENT`

The `append_event` auto-stamp above only fires when its caller's process
env already carries `DEV_KIT_AGENT`. Before this note was added, no
producer set it, so `agent_identity_coverage` measured `0.0` on every
real worktree even though the plumbing existed. Two producers now set it:

- `hooks/hooks.json` (Claude Code) and `.codex-plugin/hooks/hooks.json`
  (Codex) prefix each event-emitting hook's command line with
  `DEV_KIT_AGENT=claude-code` / `DEV_KIT_AGENT=codex` respectively — the
  manifests are runtime-specific by construction, so the stamp is exact,
  not a guess. `tools/portability_check.py` and
  `tests/test_hooks_json_parity.py` normalize the prefix away before
  comparing CC/Codex hook signatures, so it isn't flagged as manifest
  drift.
- `lib/execute.py::_emit_effectiveness_event` stamps `agent` from
  `DEV_KIT_BUILD_AGENT` (the same env var `_agent_command` already reads
  to pick the step runner), normalized to the `claude-code`/`codex`
  convention above.

`hooks/trace-session-end.sh`'s `evidence_ref.hook_event` also used to be
hardcoded to `"SessionEnd"` regardless of whether the script fired via
`Stop` or `SessionEnd` — it now reads `hook_event_name` from the hook
payload, so the Stop-vs-SessionEnd terminal-event firing rate (which
drives `subject_observability`, see `docs/quality/harness-effectiveness-stability.md`'s
sibling submetric) is diagnosable from the trace log itself.

`prevention_quality`, `first_pass_quality`, `recovery_quality`, and
`learning_quality` are unaffected by this wiring — they need real
`/dev-kit:build` activity, operator-supplied `DEV_KIT_GROUND_TRUTH`
labels, and the (unbuilt) Phase-4 shadow-mode cohort respectively, none
of which this change adds.

### Causal chaining in the executor

`_first_pass` pairs a write with its first verification via
`first.parent_id == write.event_id`. `lib/execute.py::_step_post_collect`
therefore parents `verify.passed` to the `write.observed` event id it
just captured — **not** to `step.started`. Parenting both to the
lifecycle anchor leaves `causally_linked` permanently `False`, which
collapses `first_pass_quality` to `10.0` (`ROT`) *and* pulls it into
`overall_score` at weight 0.25, where previously a `None` score dropped
out of both numerator and divisor. Pinned by
`test_verify_passed_is_parented_to_write_observed` and
`test_first_pass_quality_reflects_honest_verify_evidence` (the executor
emits `verify.passed` with `required_checks_passed=False, independent=False`
plus `evidence_provenance="self-reported"` and `checks_run=[]` because
no independent pytest/lint/build runner actually executes there — the
event exists for the causal chain, but the score reflects honest
self-reported evidence, not a fabricated 100%).

`recovery_quality` needs a `heal.attempted` event between the
`verify.failed` and the terminal `verify.passed`. The executor has no
retry path today, so it emits no `heal.attempted` and
`recovery_quality` remains `INSUFFICIENT_EVIDENCE` on the executor
trajectory. Wiring that signal is deferred to the self-fix loop.

## Missing evidence → `INSUFFICIENT_EVIDENCE`, never `0.0`

A submetric that lacks enough evidence renders as:

- `coverage < 0.90` → `status: "INSUFFICIENT_EVIDENCE"`, `score: null`
- `coverage >= 0.90` and partial data → renders a numeric `score` and the
  per-dimension `findings` strings name which dimensions are missing.

The reducer never collapses missing evidence into `0.0`. A `0.0` means
"measured zero", not "no measurement". See `INSUFFICIENT_EVIDENCE` in
`lib/harness_effectiveness.py` for the exported sentinel and `tests/test_harness_stability.py`
for the contract tests (14 cases).

## Schema bumps

| Surface | Before | After | Reason |
|---|---|---|---|
| `harness_effectiveness.build_report` `schema_version` | `1` | `2` | Advertises the new submetric |
| `lib.trace_log.EVENT_SCHEMA_VERSION` | `1` | `1` (unchanged) | `agent` / `provider` / `model` are additive top-level fields; `validate_event` already accepts unknown keys |
| `COMPONENT_WEIGHTS` (5-component) | sums to 1.0 | sums to 1.0 (unchanged) | Stability is a submetric, not a 6th component |

## Backward compatibility

- 5-component `overall_score` (sum of `COMPONENT_WEIGHTS * component_score`)
  is bit-identical when no consumer reads the new submetric.
- A consumer that ignores unknown `schema_version` continues to operate.
- A consumer that wants the stability report reads
  `components.measurement_integrity.submetrics.stability` (opt-in).
- The `learning_quality` zero-weight entry remains in the rendered output so
  visibility is unchanged.

## Why "nested submetric" instead of "6th component"

The original proposal
([`docs/proposals/harness-effectiveness/00-index.yaml`](../proposals/harness-effectiveness/00-index.yaml))
left the choice open between "submetric under `measurement_integrity`" and
"versioned sixth component". The submetric shape was chosen because the 6th
component shape would break the 5-component `overall_score` contract for every
existing consumer on day one.

## Related

- `lib/harness_effectiveness.py` — reducer source, with `STABILITY_IDENTITY_FIELDS`,
  `_stability()`, and `INSUFFICIENT_EVIDENCE` constants.
- `tests/test_harness_stability.py` — 14 contract tests.
- `skills/harness-effectiveness/SKILL.md` — operator-facing wrapper around the
  same reducer.
- `docs/skills/harness-effectiveness.md` — pre-existing 5-component doc (now
  extended by the SKILL.md update on this PR).
