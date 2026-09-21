# /dev-kit:sot-harness-writer

Interview-based Single Source of Truth (SOT) harness document writer.

## What it does

Runs 5 interview rounds (one per SOT dimension: project context,
verification, context, safety, lifecycle). Each round surfaces 2-3
evidence-backed recommendations from the agent-harness-playbook
research; the user accepts/rejects/customizes. The output is a
complete SOT harness document with traceability — every decision
cites a source URL.

The 5 dimensions are derived from the canonical 5-subsystem
decomposition of an agent harness
[src:https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html;ts:2026-08-04;type:primary]
and Anthropic's effective-harnesses article
[src:https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents;ts:2026-08-04;type:primary].

## Usage

```bash
/dev-kit:sot-harness-writer <1-line-idea>
```

The skill runs 5 rounds; each round surfaces 2-3 evidence-backed
recommendations. Pick `accept`, `reject`, or `customize` (with
explanation). The output is a SOT document with traceability.

## Output

- `.dev-kit/hand-off/sot-harness-<session>.md` — the SOT document,
  always preceded by a YAML frontmatter that marks it as a SOT
  hand-off (`handoff_kind: sot`, `status: locked | held`) so the
  plan skill's consume gate routes it via `--from-sot` (issue #898).
- `.dev-kit/decision-log-sot-harness/<session>.md` — per-round Q+A

## Iron Laws (MUST-19)

1. **5 rounds are mandatory.** Skipping a round = `status: held`.
2. **Each round surfaces at least 2 evidence-backed recommendations.**
3. **`safety_valve: 5` round cap.** Cap reached = `status: held`.
4. **`accept | reject | customize` only.** Customize must explain why.
5. **Every accepted recommendation is traceable.** No source = invalid.

## Acceptance Criteria

- A1: All 5 dimensions have a locked decision
- A2: Every accepted recommendation cites a source URL
- A3: Rejected recommendations have a reason
- A4: Open questions are explicit
- A5: Implementation phases are sequenced by dependency

## Handoff

After the SOT doc is written:

```bash
# Convert to a build-ready plan
/dev-kit:plan --from-sot .dev-kit/hand-off/sot-harness-<session>.md

# Or go straight to implementation
/dev-kit:build --from-sot .dev-kit/hand-off/sot-harness-<session>.md
```

## See also

- [SKILL.md](../../skills/sot-harness-writer/SKILL.md) — full skill spec
- [lib/sot_harness_engine.py](../../lib/sot_harness_engine.py) — the synthesizer
- [tests/test_sot_harness_engine.py](../../tests/test_sot_harness_engine.py) — 23 tests (4 TestRounds + 2 TestRecLookup + 5 TestValidate + 6 TestSynthesize + 4 TestSafeSessionId + 2 TestWrite)
- [agent-harness-playbook](https://github.com/sh-ai-x/agent-harness-playbook) — the source of all recommendations
