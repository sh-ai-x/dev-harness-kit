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
