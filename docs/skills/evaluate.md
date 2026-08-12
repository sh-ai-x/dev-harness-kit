> [← Skills index](README.md) · [Project README](../../README.md)

# `evaluate`

**Category:** `eval` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:evaluate` (human-invoked)

> **Implementation:** The workflow-native harness-effectiveness integration is
> implemented by the reducer and producers described in the [design proposal](../proposals/harness-effectiveness/00-index.html).

`evaluate` currently measures whether the agent behaves correctly when running
dev-kit skills across the existing review/security/plan, harness-quality, and
os-quality rubrics. It preserves the legacy D1–D7 Agent Behavior report and
consumes workflow evidence for five effectiveness components. It replays
recorded transcripts and reads structured evidence produced during build and
repair without creating success evidence in a separate workflow. Source:
[`skills/evaluate/SKILL.md`](../../skills/evaluate/SKILL.md).

## When to use it

- The user types `/dev-kit:evaluate` for the normal combined report.
- A Phase 3 batch (or any harness change) is about to land and needs the `harness-quality` rubric gate.
- An env-var, secret, or CI cost change needs the `os-quality` rubric gate.
- A nightly cron auto-call rotates through the registered dimensions.

## Invocation

```bash
/dev-kit:evaluate
/dev-kit:evaluate --harness-quality
/dev-kit:evaluate --os-quality
/dev-kit:evaluate --case <id> --dry-run
```

Effectiveness is part of the default report and is not enabled by a new
option. Its five components are
`prevention_quality`, `first_pass_quality`, `recovery_quality`,
`learning_quality`, and `measurement_integrity`. Missing workflow evidence
will produce `INSUFFICIENT_EVIDENCE`, never a fabricated zero or pass.
`--dry-run` remains a legacy fixture/judge option and must label synthetic
results. See `skills/evaluate/SKILL.md` and the proposal for the runtime
contract.

The report keeps two namespaces: legacy Agent Behavior D1–D7 and
`harness_effectiveness`. Existing verdicts and scales remain backward-compatible;
the new component score is not substituted into the legacy weighted mean.

## Failure modes

- `--harness-quality` / `--os-quality` against a repo with zero `eval/cases/<dim>/` case fixtures returns a single `NO_FIXTURES` verdict — never a clean pass. `lib/eval_runner.run_eval` short-circuits into this verdict the moment it sees a fixture-less dim.
- A per-case judge call that raises (bad API key, rate limit, model rename, etc.) is recorded as ROT with an `error=<msg>` field. The per-case report line surfaces that error next to the score, and `lib/eval_runner.write_report` prefixes the report with an `INFRA_FAILURE` banner when >= 80% of cases are ROT with an error attached — so a judge-infra failure is visually distinct from a genuine behavior regression.

Both behaviors live in `lib/eval_runner.py` and were added to keep `harness` / `os` from silently looking green while nothing is actually graded, and to prevent the common confusion where a single bad API key looks like 100% of cases failing simultaneously.
