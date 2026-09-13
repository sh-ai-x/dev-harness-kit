---
name: do
category: plan
description: Unified intent-to-workflow router with versioned envelopes and bounded context handoffs.
alpha: state
when_to_use:
  - User types /dev-kit:do <intent>
  - User wants one entrypoint from intent through verification
  - An existing workflow must receive a compact, auditable handoff
allowed-tools: Read Skill
disallowed-tools: Agent WebFetch
model: opus
disable-model-invocation: true
user-invocable: true
---
> [← Skills index](../../README.md)

## What it does

`/dev-kit:do` resolves a user's intent once, emits a versioned route envelope,
and delegates to the existing skill that owns the work. It is a thin router,
not a second policy engine: static hooks, Iron Laws, and each child skill's
contract remain authoritative. `/do` is a shorthand for the canonical
`/dev-kit:do` entrypoint.

## Inputs and critical-spec hold

Read only the current request and the minimum repository metadata needed for
routing. Do not re-inject a previous transcript. The request must identify:

1. the intended change or outcome;
2. the target scope (repository, path, PR, or an explicitly named artifact);
3. the evidence that will make the outcome verifiable (test, review, report,
   or another concrete acceptance signal).

If any of these are missing, or if more than one owner matches, stop with a
`HOLD` / `NEEDS_HUMAN` envelope. Report the missing field or ambiguous owners
and wait for a clarified request. Do not guess a route, silently widen scope,
or launch an interactive planning interview from an incomplete request.

## Route resolution

Resolve `mode`, `team_toggle`, and `worktree` from the existing project,
session, and environment sources. Do not write configuration as a side effect
of routing. Valid mode values are `full`, `lite`, and `undev`; team is an
independent `on` / `off` toggle. When mode is `undev`, return `HOLD` with
`reason=mode_disabled` and do not delegate.

Use the following ownership registry. The table maps an intent to an existing
owner; it does not copy that owner's policy or turn its description into a
new loop.

| `route_id` | Owner | Intent boundary |
|---|---|---|
| `research` | `skills/research/SKILL.md` | cited evidence is the requested outcome |
| `plan` | `skills/plan/SKILL.md` | a PRD or implementation plan is the requested outcome |
| `build-debug` | `skills/build-debug/SKILL.md` | reproduce, isolate, or diagnose a defect |
| `build` | `skills/build/SKILL.md` | implement an already-scoped plan |
| `review` | `skills/review/SKILL.md` | correctness, architecture, or diff review |
| `security` | `skills/security/SKILL.md` | security assessment or remediation review |
| `babysit-pr` | `skills/babysit-pr/SKILL.md` | monitor or repair an open PR |
| `ship` | `skills/ship/SKILL.md` | release/tag gate after owner checks |
| `ralph-attended` | `skills/ralph/SKILL.md` | explicitly end-to-end, gated loop execution |
| `mode-config` | `skills/mode/SKILL.md` | change or inspect `DEV_KIT_MODE` |
| `team-config` | `skills/team/SKILL.md` | change or inspect `DEV_KIT_TEAM` |

The existing owner's preconditions still decide whether the delegated request
can proceed. A request that mixes independent outcomes must be narrowed or
sent to the explicit Ralph route; `/do` does not fan out into an unbounded
workflow.

The deterministic resolver is implemented by `lib/do_router.py`. The skill
runtime may invoke it as a read-only JSON decision point:

```bash
python3 -m lib.do_router \
  --intent "<outcome>" --target "<scope>" \
  --acceptance "<verification>" --root .
```

The command returns exit `0` for `DELEGATED`, `4` for a specification/owner/
mode `HOLD`, and never writes mode, team, or worktree configuration.

## Versioned route envelope

Emit this envelope before delegation. Values shown with angle brackets are
resolved per request; the schema and route versions are stable identifiers.

```json
{
  "schema_version": 1,
  "run_id": "<stable-run-id>",
  "request_id": "<request-id>",
  "intent_ref": "<opaque-intent-reference>",
  "directive_ref": "docs/core/DEV-HARNESS-CORE.md",
  "directive_version": "1.0.0",
  "route_id": "<one-registry-route>",
  "route_version": "1.0.0",
  "owner": "skills/<owner>/SKILL.md",
  "scope": {"target": "<explicit-target>", "acceptance": "<evidence>"},
  "worktree": {"kind": "current", "ref": "<worktree-reference>"},
  "mode": "full",
  "team_toggle": "off",
  "policy_version": "static-ssot-v1",
  "context_budget": {
    "schema_version": 1,
    "context_mode": "artifact_ref",
    "max_excerpt_chars": 4096,
    "max_summary_chars": 2048,
    "max_artifact_refs": 32,
    "max_event_bytes": 16384,
    "full_transcript_reinjection": false
  }
}
```

For a hold, keep the envelope shape and set `status` to `HOLD`,
`route_id` to `unresolved`, `owner` to `human`, and include a bounded
`missing_specs` or `ambiguity` field. The route cannot be changed after a
run has emitted a delegated envelope.

## Bounded handoff

Pass the owner only the envelope plus a handoff built by the existing helper
when `lib/context_budget.py` is available:

```python
from lib.context_budget import build_handoff, validate_handoff

handoff = build_handoff(
    intent_ref=envelope["intent_ref"],
    checkpoint=checkpoint_ref,
    failure_signature=failure_signature,
    artifact_refs=required_artifact_refs,
    summary=compact_summary,
    input_tokens=measured_input_tokens,
    output_tokens=measured_output_tokens,
    replay_tokens=measured_replay_tokens,
)
validate_handoff(handoff)
```

The handoff may contain an intent reference, checkpoint, latest failure
signature, required artifact references, and a compact redacted summary. It
must not contain `prompt`, `prompts`, `transcript`, `transcripts`, or
`messages`. If the helper cannot validate the bounds, stop with
`INSUFFICIENT_EVIDENCE`; do not create an inline fallback limiter.

Record `usage_status=measured` only when provider token data is present.
Otherwise record `usage_status=missing` and preserve
`INSUFFICIENT_EVIDENCE`; never substitute zero. `replay_tokens` measures
bounded artifact/summary reuse and is not permission to replay a transcript.

## Delegation and output

1. Resolve and emit exactly one envelope.
2. Invoke the selected owner with the envelope and bounded handoff.
3. Return the owner's status, evidence references, and verification output.
4. If the owner reports a guard block, missing evidence, recovery state, or
   human gate, preserve that status and stop.

Ralph owns only cross-skill sequencing, run-level budgets, recovery, and joins
when `ralph-attended` is selected. Build, babysit, review, security, plan, and
ship retain their internal loops and enforcement. The static guard sources
listed in [`docs/core/DEV-HARNESS-CORE.md`](../../docs/core/DEV-HARNESS-CORE.md)
remain the authority; this skill only references them.

## Next step

After `DELEGATED`, follow the selected owner's next step. After `HOLD` or
`NEEDS_HUMAN`, provide the missing specification or resolve the ambiguity and
invoke `/dev-kit:do` again.
