# Gate-Dynamic (v1.1.0) — non-deterministic LLM-judge-driven gate selection

This is the v1.1.0 layer that decides, per babysit-pr iteration, which
CI gates can be SKIPPED. The decision is computed by an LLM judge that
inspects the current diff + previous verdicts + the gate catalog,
filtered through 4 hard rules. The result is cached per `head_sha` and
audited at `.dev-kit/gate-dynamic/<sha>.json`.

**You should not need this doc** to use the feature — `/dev-kit:gate-select`
exposes the picker UX (`eval-dynamic`, `apply-dynamic`, `audit-dynamic`).
This doc is the reference for operators wiring `forced_run` overrides
or troubleshooting a flapping judge.

## Hard rules (bypass the LLM)

The orchestrator (`lib/gate_dynamic.apply_hard_rules`) applies 6
deterministic rules AFTER the LLM call. The LLM cannot overrule them.

| Rule | Condition | Effect |
|---|---|---|
| 1. First-push-deterministic | `iteration == 1` | All gates `skip=False` regardless of LLM verdict. |
| 2. Operator override | `forced_run: true` on the gate | `skip=False`. The field is documented as "never SKIP this gate" — it does NOT mean "force the gate to RUN". |
| 3. Critical gate in scope | `gate_name in {review, security}` AND `scope_globs` matches a diff file | `skip=False`. Critical gates are LLM-skipped only when the diff doesn't touch their scope. |
| 4. Low-confidence veto | `LLM confidence < 0.7` | `skip=False`. The LLM is a recommender; low confidence means "I don't know — run the gate". |
| 5. Opt-in required | `gates.<name>.dynamic_eligible != true` | `skip=False`. Default is `False` for every gate — an operator must explicitly opt a gate into LLM-driven skip (see "Per-gate opt-in" below). |
| 6. High-risk veto | `risk_level > RISK_FLOOR` (default 3.0) | `skip=False`. `risk_level` is a 3rd judge axis (0-10, lower_is_better) alongside `gate_skippable` and `confidence`. A missing key in a partial LLM response fails closed (defaults to a sentinel above the floor, not to 0.0), so an incomplete judge response can never bypass this veto. |
| (pre-rule) Coerced-response sanity check | `gate_skippable >= 9 ∧ risk_level <= 1` (same LLM response) | Upgrades `risk_level` to `MISSING_RISK_LEVEL_SENTINEL` before rule #6 runs, so the skip is vetoed. Closes the A08 prompt-injection path: a coerced judge returning max skip + min risk would otherwise pass rule #6. |
| (cache) Hard-rule re-application | `select_gates` cache hit | The cached `GateSkipDecision.decisions` are re-filtered through `apply_hard_rules` before returning. Closes the A01/A06 short-circuit: a pre-rule-#6 cached `skip=True` cannot survive a rule upgrade. |

When the orchestrator is invoked from a standalone
`bin/review-local.sh --dynamic-skip` call (no prior
`babysit-checks.json`, no `previous_verdicts`), all confidences default
to 0.0 → no gates skipped. This is the graceful-degradation path.

## Per-gate opt-in

`lib/gate_dynamic` only considers a gate eligible for LLM-driven skip
if `gates.<name>.dynamic_eligible: true` is set in `.dev-kit/gates.json`.
Default is `False` for all gates — operators opt in per gate.

```bash
# Opt maintenance in for dynamic-skip, scoped to skills/ + lib/.
/dev-kit:gate-select set maintenance dynamic_eligible true
/dev-kit:gate-select set maintenance scope_globs skills/**,lib/**

# Force review to NEVER be skipped, regardless of LLM.
/dev-kit:gate-select set review forced_run true
```

Available fields per gate (schema bump 1.0.0 → 1.1.0):

| Field | Type | Default | Purpose |
|---|---|---|---|
| `dynamic_eligible` | bool | `false` | Opt in to the dynamic-skip layer. |
| `scope_globs` | list[str] | `[]` | File globs the gate's LLM judge covers. Used by hard rule #3. |
| `forced_run` | bool | `false` | Hard rule #2. |

The legacy 3-key shape (1.0.0) auto-upgrades in memory on read — no
operator action needed. Cost / model / skip-when tuning flags were
considered but deferred to v2 — shipping them without a consumer
would be the OE-2 speculative-param pattern (per the maintenance
gate review).

## Cache invalidation

Per-`head_sha` decision JSON lives at `.dev-kit/gate-dynamic/<sha>.json`.
The payload includes a `gates_hash` field (SHA-256 of
`.dev-kit/gates.json` content at decision time). On `load_decision`,
the loader recomputes the hash; mismatch → invalidate → re-run the
judge. An operator who changes `gates.json` (toggles a gate, adds a
new gate, flips `forced_run: true`) will see the next iteration
honor the change even if the head_sha is unchanged.

## Audit trail

Every `select_gates` invocation writes the full LLM prompt + response
+ parsed scores + reasoning to `.dev-kit/gate-dynamic/<sha>.json`.
Shape:

```json
{
  "head_sha": "abc123def",
  "decisions": [
    {"gate_name": "review", "skip": false, "reasoning": "...",
     "confidence": 0.0, "risk_level": 0.0, "raw_score": {}}
  ],
  "llm_raw": {"scores": {...}, "raw": "..."},
  "gates_hash": "<sha256 of gates.json>",
  "decided_at_iso": "2026-09-16T00:00:00Z"
}
```

TTL-based prune: files older than 7 days are removed on every
`select_gates` call. Cheap (~ms); covers abandoned branches that
aren't worktree-managed.

## Interactive mode

`DEV_KIT_GATE_DYNAMIC_INTERACTIVE=1` makes `lib/gate_dynamic.select_gates`
block with `AskUserQuestion` BEFORE applying hard rules + saving. The
operator can confirm, override, or force-run a specific gate. Default
is audit-only — the judge logs decisions, babysit-pr loop doesn't
block.

## Failure modes

1. **LLM unavailable** (no `api_key`, missing template, HTTP failure):
   `select_gates` returns a no-skip decision. All gates `skip=False`.
   The audit JSON has `llm_raw.note: "llm_unavailable"`.
2. **LLM parse failure** (response not JSON, missing axes): same as
   #1 — all confidences default to 0.0, no gates skipped.
3. **`gates.json` corrupt mid-iteration**: `select_gates_dynamic`
   catches the exception and treats the catalog as empty. Hard rule
   #3 (critical-gate-in-scope) cannot fire without `scope_globs`, so
   review + security are skipped-permissive. This is intentional —
   the orphan PR case is better than a wedged babysititer.

## Picker UX

| Sub-command | Effect |
|---|---|
| `eval-dynamic [--head-sha SHA]` | Run the judge, print the per-gate skip recommendation JSON. Read-only. |
| `apply-dynamic [--head-sha SHA]` | Apply: any gate the judge said `skip=false` (with `confidence >= 0.7`) gets `forced_run: true`. |
| `audit-dynamic [--head-sha SHA]` | Show the audit trail at `.dev-kit/gate-dynamic/`. `--tail N` (default 20) trims to N most recent. |

All three live under `/dev-kit:gate-select`. The implementation is
`lib/gate_dynamic.py` + `lib/babysit_pr_reliability.select_gates_dynamic`
+ `bin/review-local.sh --dynamic-skip`.

## Tuning

- **Confidence floor (default 0.7)**: `lib/gate_dynamic.CONFIDENCE_FLOOR`.
  Lower = more aggressive skips; higher = more conservative.
- **Risk ceiling (default 3.0)**: `lib/gate_dynamic.RISK_FLOOR`. A gate
  with `risk_level > RISK_FLOOR` is never skipped (hard rule #6),
  regardless of how high `gate_skippable` or `confidence` score.
  Lower = more conservative (fewer skips allowed); higher = more
  aggressive. A missing `risk_level` key in the LLM response defaults
  to `lib/gate_dynamic.MISSING_RISK_LEVEL_SENTINEL` (11.0, above the
  0-10 scale) so an incomplete response fails closed instead of
  bypassing the veto.
- **Coerced-response sanity check** (A08 fix): when
  `gate_skippable >= 9 AND risk_level <= 1` in the same LLM response,
  treat `risk_level` as missing — upgrade to
  `MISSING_RISK_LEVEL_SENTINEL` so rule #6 vetoes the skip. This pair
  is the signature of a PR-body prompt-injected judge response (a
  genuine judge almost never emits maximum skip + minimum risk
  together); without the check an attacker reaching iteration ≥ 2
  could skip every gate. Thresholds:
  `lib/gate_dynamic.COERCED_RESPONSE_SKIP_FLOOR` (default 9.0) and
  `lib/gate_dynamic.COERCED_RESPONSE_RISK_CEILING` (default 1.0).
- **Cache-hit re-application** (A01/A06 fix): `select_gates` re-runs
  `apply_hard_rules` on a cached decision before returning, so a
  pre-rule-#6 cached `skip=True` cannot survive a rule upgrade. The
  judge is NOT re-invoked (no network); the rule pass is pure.
- **TTL (default 7 days)**: `lib/gate_dynamic.DYNAMIC_AUDIT_TTL_DAYS`.
- **Temperature (default 0)**: `lib/llm_judge.call_judge(temperature=0)`
  is hard-coded in `lib/gate_dynamic.invoke_judge`. Do NOT raise it —
  non-deterministic temperature defeats the per-head_sha cache.
- **Iteration count**: derived from `LoopState.iteration` at STEP 1
.5 fire time. No separate config.

## See also

- `lib/gate_dynamic.py` — the orchestrator.
- `lib/babysit_pr_reliability.select_gates_dynamic` — the SKILL-layer
  wrapper that flattens the per-gate decision to a `frozenset`.
- `lib/gates_state.py` — the schema SSOT (`SCHEMA_VERSION = "1.1.0"`,
  `DYNAMIC_FIELDS_DEFAULT`).
- `eval/prompts/judge-gate-dynamic.md` — the LLM judge rubric.
- `bin/review-local.sh --dynamic-skip` — the standalone invocation
  (used by `bin/babysit-pr-local.sh` when `BABYSIT_DYNAMIC_SKIP=1`).