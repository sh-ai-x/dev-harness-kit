---
name: evaluate
category: eval
description: 0-arg eval extension. Replays transcripts and consumes workflow evidence against registered rubrics, preserving legacy Agent Behavior D1–D7 and reporting five harness-effectiveness components plus the nested measurement-integrity submetric (issue #663). /dev-kit:evaluate.
alpha: enforcement
when_to_use:
  - User types /dev-kit:evaluate
  - After a Phase 3 batch lands (or any harness change), to gate on harness-quality
  - After any change that touches env vars, secrets, or CI cost reporting, to gate on os-quality
  - nightly cron auto-call (per-dim rotated)
allowed-tools: Read Grep Glob Bash Agent
disallowed-tools: Write Edit
model: opus
disable-model-invocation: false
user-invocable: true
safety:
  safety_valve: 1
  convergence: per-case axis mean >= 8.0
  dedup_metric: identical-case-score=2
  user_interrupt: true
---
> [<- Skills index](../../README.md)

# /dev-kit:evaluate — Eval extension (Phase 3)

Runs the existing eval dimensions and adds workflow-evidence-based
harness-effectiveness reporting. Backward-compatible: the existing review,
security, plan, harness-quality, os-quality, and D1–D7 contracts remain
unchanged. The effectiveness result is gated on structured evidence (via
`lib.eval_runner.RUBRIC_REGISTRY`), which is the deterministic enforcement hook
that prevents the LLM judge from scoring against an unknown rubric.

The `alpha: enforcement` declaration is required by Iron Law L6 — the
deterministic registry lookup is the part the model cannot self-impose.

## Modes

- `/dev-kit:evaluate` (default): run the existing per-dim eval, legacy D1–D7,
  and the five harness-effectiveness components in one report. The
  `measurement_integrity` component now carries a nested `stability`
  submetric (issue #663) covering agent / model / provider swap
  behaviour; missing evidence there is reported as `INSUFFICIENT_EVIDENCE`,
  not `0.0`. `build_report` returns `schema_version: 2` to advertise
  the new submetric — consumers that ignore unknown versions continue
  to work unchanged.
- `/dev-kit:evaluate --harness-quality`: register `harness-quality` rubric +
  judge prompt with `RUBRIC_REGISTRY`, then run per-dim eval against the
  `harness` DIM_AXES tuple.
- `/dev-kit:evaluate --os-quality`: register `os-quality` rubric + judge
  prompt, then run per-dim eval against the `os` DIM_AXES tuple.
- `/dev-kit:evaluate --harness-quality --os-quality`: both, in sequence.
- `/dev-kit:evaluate --case <case_id>`: restrict to a single case fixture.
- `/dev-kit:evaluate --dry-run`: skip the LLM judge; mock each case at
  7.0 / DRIFT_WARNING (same shape as the legacy `--dry-run`).

The effectiveness components are not enabled by a new option. They consume
workflow evidence already produced by TraceLog, repair-coordinator events,
guard/verification producers, and existing eval artifacts. Missing evidence is
reported as `INSUFFICIENT_EVIDENCE`; it is never inferred from a transcript or
converted to a passing/zero score.

## Rubric registry (deterministic enforcement)

`lib/eval_runner.RUBRIC_REGISTRY` is the registry this skill writes to. The
call path is:

```
--harness-quality -->
    RUBRIC_REGISTRY.register(
        name="harness-quality",
        rubric_yaml_path="eval/rubrics/harness-quality.yaml",
        judge_prompt_path="eval/prompts/judge-harness-quality.md",
    )
    judge against DIM_AXES["harness"]

--os-quality -->
    RUBRIC_REGISTRY.register(
        name="os-quality",
        rubric_yaml_path="eval/rubrics/os-quality.yaml",
        judge_prompt_path="eval/prompts/judge-os-quality.md",
    )
    judge against DIM_AXES["os"]
```

If the flag is passed but the corresponding YAML or judge prompt is missing
on disk, the skill must exit non-zero with a clear message — never silently
score against an unknown rubric.

## Cross-validate (3-judge variance gate)

Each per-case eval can fan out to three judges. When the population variance
of their per-axis means exceeds `0.5`, the result sets
`escalate: true` via `lib/analysis_core/cross_validate.cross_validate_scores`.
A report with any `escalate: true` block requires human review before being
treated as a clean PASS.

The threshold (`ESCALATE_VARIANCE_THRESHOLD = 0.5`) lives in
`lib/analysis_core/cross_validate.py` — single source of truth.

## Verdict (per-case axis mean)

Same thresholds as the legacy eval:
- **OK** at >= 8.0
- **DRIFT_WARNING** 5.0-7.9
- **ROT** < 5.0

A run that contains any `escalate: true` block is reported with verdict
`ESCALATED` regardless of axis mean — escalation beats the mean, because
"the judges disagree" is itself a defect signal.

## Output

- Per-dim markdown report written to `.dev-kit/eval-report.md` (legacy shape).
- Per-dim JSON result block (legacy shape) on stdout, plus an
  `escalations` list when cross-validate disagrees.
- Exit 0 if all cases OK (or DRIFT_WARNING + no escalations). Exit 1
  on any ROT or escalation.

## Backward-compat

- `--harness-quality` and `--os-quality` are NEW flags. Without them,
  the skill behaves exactly like the pre-Phase-3 eval.
- `RUBRIC_REGISTRY` is class-level and starts empty. Existing call
  sites that do not import or call `register()` are unaffected.
- The harness-effectiveness reducer gains a nested `stability` submetric
  under `components.measurement_integrity.submetrics.stability` (issue
  #663). `build_report` returns `schema_version: 2`. The 5-component
  `overall_score` contract is preserved — `COMPONENT_WEIGHTS` still
  sums to 1.0 and no top-level weight is added for stability. Missing
  stability evidence surfaces as `status: INSUFFICIENT_EVIDENCE` with
  `score: null` (never `0.0`). `INSUFFICIENT_EVIDENCE` is exported
  from `lib.harness_effectiveness` as a sentinel string constant.

## Next step

- After a passing harness-quality + os-quality run: hand off to
  `/dev-kit:ship` if this is a release branch, or `/dev-kit:status`
  to confirm the eval cycle is green.
- After a failed run: triage via `/dev-kit:babysit-pr` if the failure
  is on a PR, or re-run `/dev-kit:evaluate` after fixing the failing case.
