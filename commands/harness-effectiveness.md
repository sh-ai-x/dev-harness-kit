---
description: Print the harness-effectiveness scorecard (5-component reducer) without running the full /dev-kit:evaluate judge pass. Cheap, sub-second, no API spend.
argument-hint: ""
---

# /dev-kit:harness-effectiveness

Prints the 5-component harness-effectiveness scorecard by invoking
[`lib.harness_effectiveness.build_report`](../lib/harness_effectiveness.py)
against the worker's current worktree. Deterministic — no LLM judge, no
transcript replay, no case fixture. Output is pretty-printed JSON with
`overall_score`, `status`, and the five components (`prevention_quality`,
`first_pass_quality`, `recovery_quality`, `learning_quality`,
`measurement_integrity`). Components without evidence report
`score: null` + `status: INSUFFICIENT_EVIDENCE` + a `findings` string.

The body of this command lives in
[`skills/harness-effectiveness/SKILL.md`](../skills/harness-effectiveness/SKILL.md).
The command is a thin wrapper.

## Invocation

```bash
/dev-kit:harness-effectiveness
```

No arguments. The reducer reads `.dev-kit/trace/*.jsonl` (the TraceLog
events the harness has emitted into the current worktree) and writes the
JSON report to stdout.

## Args

None. The slash is 0-arg by design. The reducer accepts no flags.

## Output

Pretty-printed JSON to stdout. Exit 0 always. The caller checks
`status == "ROT"` (per component) or `overall_score < threshold` if it
wants a hard fail.

## Examples

```bash
# Quick check before opening a PR:
/dev-kit:harness-effectiveness | jq '.status, .overall_score'

# Surface a single failing component:
/dev-kit:harness-effectiveness | jq '.components.first_pass_quality'
```

## Related

- `skills/harness-effectiveness/SKILL.md` — full skill contract.
- `lib/harness_effectiveness.py` — the reducer implementation.
- `lib/trace_log.py` — the TraceLog event source the reducer consumes.
- `/dev-kit:evaluate` — runs this reducer + the 12-case judge pass in one report.