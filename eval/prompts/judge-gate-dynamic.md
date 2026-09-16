# Gate-Dynamic Judge (judge-gate-dynamic, v1.0.0)

Used by `lib/gate_dynamic.py` (called from babysit-pr STEP 1.5 and
`bin/review-local.sh --dynamic-skip`) to recommend, per gate, whether
the gate can be SKIPPED for the current babysit-pr iteration.

The judge does NOT replace the actual gate. It produces a per-gate
recommendation that is then filtered through the 4 hard rules in
`apply_hard_rules`:

  - `iteration == 1` → first push is always deterministic (no skip).
  - `forced_run: true` on the gate → operator override, never skip.
  - gate name in `{review, security}` AND `scope_globs` match the diff
    → critical gate, never skip.
  - `confidence < 0.7` → low-confidence veto, never skip.

The judge returns ONLY the three gate-dynamic axes below. Each axis
is 0-10 (higher = stronger signal). The orchestrator combines
`gate_skippable` (>= 7 = skip recommended) with `confidence` (>=
0.7 raw = eligible for skip).

## Inputs

- `${PARENT_PR}` — the PR number the babysit iteration is iterating on.
- `${HEAD_SHA}` — current HEAD; the cache key for the audit JSON.
- `${DIFF_STAT}` — `git diff --stat` of HEAD vs the prior commit (or
  merge-base for the PR); one line per file.
- `${DIFF_SAMPLE}` — first ~2 KB of unified diff hunk content.
- `${PR_BODY}` — the PR description.
- `${GATE_CATALOG}` — JSON dump of `.dev-kit/gates.json` (the v1.1.0
  schema; each gate carries `dynamic_eligible`, `scope_globs`,
  `forced_run`, etc.).
- `${PREVIOUS_VERDICTS}` — JSON map of gate name to last iteration's
  verdict (`Approve` / `Changes Requested` / `Blocked` / missing).

## Axes (each 0-10)

- `gate_skippable` — How safe is it to skip this gate for the
  current diff? **Higher = safer to skip.** Anchor:
    - 0-3: critical gate (review/security) with diff touching its
      scope_globs — NEVER skip.
    - 4-6: marginal — only skip if previous verdict was Approve AND
      the diff doesn't touch the gate's scope.
    - 7-10: clearly safe — previous Approve + no in-scope diff change.
- `confidence` — How confident is the judge in its recommendation?
  Raw 0-10 scale; the orchestrator normalizes to 0-1 (divides by 10)
  for the `confidence >= 0.7` floor. If uncertain, score LOW (vacuously
  safe — orchestrator vetoes the skip).
- `risk_level` — What is the worst-case risk of skipping this gate
  for this iteration? **Lower = safer.** Polarity is `lower_is_better`
  in `lib/llm_judge.AXIS_POLARITY`; the orchestrator inverts before
  comparing to the skip threshold. Anchor:
    - 0-2: zero risk (diff is unrelated to gate's domain).
    - 3-5: low risk (adjacent scope, no direct impact).
    - 6-8: medium risk (in-scope change, but previous verdict clean).
    - 9-10: high risk (in-scope change + non-clean previous).

## Output JSON

Respond ONLY with a single JSON object:

```json
{
  "gate_skippable": <0-10>,
  "confidence": <0-10>,
  "risk_level": <0-10>,
  "reason": "<one short sentence naming the worst axis>"
}
```

No prose before or after. The `reason` string is what the audit
JSON preserves — keep it under 100 chars.

## Notes on determinism

- This judge is invoked with `temperature=0` (see
  `lib/llm_judge.call_judge`) to pin determinism across iterations
  for the same diff + previous verdicts. With temperature > 0 the
  same input could produce different skip recommendations across
  iterations — defeating the cache-invalidation story.
- The judge does NOT know about hard rules — it just emits the
  raw axes. The hard rules are applied AFTER the LLM call. So a
  low `gate_skippable` for a critical gate is fine; the rule
  layer will still block the skip.