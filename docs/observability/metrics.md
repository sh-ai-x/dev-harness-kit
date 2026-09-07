# Metric & gate skills

Five skills produce a number you can act on. They split cleanly into three
families — **gates** (block the PR if they fail), **observe-only metrics**
(print or write a report; never block a tool call), and **post-hoc / static
reducer** (replay or walk the repo without I/O).

| Skill | Family | Trigger | Source | Output | When to reach for it |
|---|---|---|---|---|---|
| [`/dev-kit:maintenance`](../skills/maintenance.md) | **Gate** | PR-time | PR diff + `eval/prompts/judge-code-sanity.md` | One **Verdict:** line + CC-1..8 / OE-1..8 / VM-1..4 breakdown. `>=8.0` → Approve, `5.0..7.99` → Changes Requested, `<5.0` → Blocked | Production-code PR opened; you want the code-sanity verdict before requesting review. |
| [`/dev-kit:ci-doctor`](../skills/ci-doctor.md) | Observer (pre-flight) | Pre-PR | `.github/`, `.dev-kit/ci-config.json`, provider file, secrets, `gh auth` | One PASS/FAIL summary across five readiness checks | "If I open a PR now, will CI even start?" |
| [`/dev-kit:security-metrics`](../skills/security-metrics.md) | Observer (static) | Anytime | Source tree only (`Read` / `Grep` / `Glob` / `Bash`) | Deterministic 0–100 OWASP A01–A10 scorecard + evidence Markdown | Quick triage metric before the full `/dev-kit:security` review. |
| [`/dev-kit:evaluate`](../skills/evaluate.md) | Observer (post-hoc) | Post-merge | Replayed transcripts + workflow evidence via `lib/eval_runner.RUBRIC_REGISTRY` | Per-rubric LLM-judge verdict + legacy D1–D7 + the five harness-effectiveness components | After a harness change — programmatic gate on harness-quality and os-quality rubrics before merge. |
| [`/dev-kit:harness-effectiveness`](../skills/harness-effectiveness.md) | Observer (static reducer) | Anytime | Same reducer as `evaluate`'s harness-effectiveness column (`lib.harness_effectiveness.build_report`) | The five-component scorecard (prevention / first-pass / recovery / learning / measurement-integrity) standalone, sub-second, zero API spend | When you want the harness-effectiveness number without running the full `evaluate` judge pass. |

> Only `/dev-kit:maintenance` is a hard gate. The other four emit numbers but
> never block a tool call — they are *observe-only*. `ci-doctor` doesn't block
> either; it answers a yes/no question.

## How they relate

- **`evaluate` ⊃ `harness-effectiveness`.** `evaluate` runs an LLM judge over
  12 cases per dimension and embeds the 5-component scorecard at the bottom.
  `harness-effectiveness` is the same reducer (`lib/harness_effectiveness.build_report`)
  stripped of the LLM judge — sub-second, zero API spend. Run
  `harness-effectiveness` for spot-checks, `evaluate` for the full audit.

- **`maintenance` ⊂ `review-local`.** `/dev-kit:review-local` chains
  `/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance` and aggregates
  the three verdicts into a combined gate. The maintenance gate verdict mapping
  (Approve / Changes Requested / Blocked) is the same in both invocations.

- **`security-metrics` ≠ `security`.** `security-metrics` is a static 0–100
  scorecard (triage); `security` is the deep OWASP Top-10 review with
  evidence-backed findings and a verifier pass. Run `security-metrics` for the
  headline number, `security` for the audit.

- **`ci-doctor` is the only "metric" that answers a yes/no question.** Every
  other skill in this doc emits a number; `ci-doctor` is a flat PASS/FAIL
  checklist across the files the next PR depends on.

## Nightly / scheduled

`evaluate` runs *itself* as a cron on a per-dimension rotation (see
`lib/eval_runner.RUBRIC_REGISTRY`). Nightly calls land in
`.dev-kit/evaluations/` for trend tracking — the gate does not wait for them,
but the trajectory is the long-term signal.

## Deep contracts

Per-skill rubric YAMLs, judge prompts, threshold env vars, and the verifier
pass loop live in each skill's own page — follow the links in the table above.
