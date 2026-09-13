# ADR-0024 — Ralph loop control, evidence, and Context Diet

- **Status:** accepted for the first implementation slice
- **Date:** 2026-09-13
- **Decision owners:** dev-harness-kit maintainers

## Context

Ralph coordinates research, proposal, plan, build, babysit, and ship, but
each child loop owns its own state and safety valves. A Ralph restart must not
replay a completed child stage, infer a successful side effect from a killed
process, or turn missing telemetry into a passing metric. The previous
implementation also allowed an empty LLM verdict to be reported as a green
audit record; the CI source now fails closed for that case.

## Decision

Ralph is a cross-skill control plane with three durable projections:

1. **Control:** a versioned checkpoint records the run, attempt, stage,
   sequence, deadline, terminal reason, and recovery action. A process death
   leaves an interrupted attempt and resumes only from the last durable
   boundary.
2. **Evidence:** stage start, finish, retry, failure, recovery, and terminal
   events carry an attempt id, parent run id, bounded failure excerpts, and
   artifact references. The progress markdown is a derived operator view.
3. **Measurement:** pure reducers expose numerator, denominator, coverage,
   status, and evidence event ids. Missing evidence is
   `INSUFFICIENT_EVIDENCE`, never success and never an invented zero.

The ownership boundaries are fixed:

| Surface | Owner | Ralph behavior |
|---|---|---|
| Static permissions, TDD, worktree, secrets, stop verification | hooks / Iron Laws | Reference only; never reimplement or override |
| Build step dispatch and three-cycle self-fix | build / execute | Join child output and events to the Ralph attempt |
| Poll, repair, no-progress, and MAX_ITERS | babysit-pr | Import child outcome; never reset its counters |
| Review, security, maintenance verdicts | their workflows + `pr_verify` | Missing verdict remains blocking evidence |
| Tag and merge boundary | ship + human operator | Ralph may land at `USER_MERGE_REQUIRED`, never auto-merge |
| Cross-skill sequencing, lease, deadline, recovery, run metrics | Ralph | Only this plane is Ralph-owned |

The `/do` route envelope is immutable for a run and carries intent and
directive references, route identity/version, scope/worktree, mode, team
toggle, policy version, and bounded context metadata. `DEV_KIT_TEAM` is
orthogonal to `DEV_KIT_MODE`; no route may introduce `team` as a mode value.

## Context Diet contract

Handoffs contain intent references, checkpoints, the latest failure signature,
at most 32 artifact references, and a compact summary. They use
`artifact_ref`, `summary_ref`, or `minimal_inline` context modes. Prompts and
full transcripts are not persisted or reinjected. Excerpts are redacted and
capped at 4 KiB, summaries at 2 KiB, event payloads at 16 KiB, and derived
progress views at 64 KiB. Missing provider usage is insufficient evidence,
not zero usage.

## Failure and recovery contract

- A child non-zero exit records exit code, bounded stderr, duration, failure
  class, and an attempt id before the state snapshot advances.
- A stale lease or process death is `interrupted`, then
  `RECOVERY_REQUIRED` unless a safe checkpoint proves the next stage.
- Duplicate dispatch keys are idempotent; they cannot imply a successful
  push, tag, review, or merge.
- `USER_MERGE_REQUIRED` is a healthy terminal that still requires a human
  merge. `RECOVERY_REQUIRED` requires inspection of the event and state
  evidence before re-entry.

## Metrics

The first implementation reports, without changing routing policy:

- stage success and recovery rates;
- retry and no-progress counts;
- terminal evidence coverage;
- artifact handoff rate and full-context reinjection rate;
- usage coverage, cache-hit ratio, replay ratio, and payload-cap violations;
- guard decision coverage and missing-verdict counts.

Metrics do not authorize tools, change retry caps, approve a PR, change mode,
or merge code. Baseline-dependent policy changes require a separate accepted
decision after at least 30 complete runs and 14 days of evidence.

## Consequences

This keeps safety decisions static and reviewable while making Ralph failures
recoverable and measurable. It adds small durable writes at stage boundaries
and temporarily requires compatibility readers for legacy state. Those costs
are accepted because a compact, inspectable failure record is safer than
replaying an opaque prompt history.

## References

- `iron-laws/index.md`
- `hooks/index.md`
- `rules/git-workflow.md`
- `skills/ralph/lib/ralph_state.py`
- `skills/ralph/lib/ralph_chain.py`
- `lib/trace_log.py`
- `lib/harness_effectiveness.py`
- `docs/proposals/review/ralph-loop-engineering/loop-control-metrics.yaml`
