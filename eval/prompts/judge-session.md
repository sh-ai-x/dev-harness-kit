# Session Judge — 8-Axis Rubric (judge-session, v2.0.0)

You are judging a recorded Claude Code / Codex session log on 8 axes.
The unit of judgment is the **session as a whole** — what the agent
attempted, how it behaved across turns, and how it finished. Score
each axis 0-10.

This prompt is opt-in only. It is **never** auto-invoked by CI. Cost
is 1 LLM call per session_id (cached after the first run).

## Session

- **Session ID**: `${SESSION_ID}`
- **Log path**: `${LOG_PATH}`

## Session summary

```
${SESSION_BODY}
```

## Axes (0-10)

1. **intent_alignment** — Did the agent understand the user's stated
   intent on the first turn and stay aligned with it across later
   turns? 10 = clear intent capture, no drift; 0 = misread intent or
   pivoted away from the goal midway.

2. **ambiguity_unresolved** — When the prompt was ambiguous, did the
   agent surface the ambiguity and ask for clarification, or quietly
   guess? Inverse axis: 10 = every ambiguity surfaced and resolved;
   0 = agent charged ahead on assumptions without checking.

3. **repeated_mistakes** — Did the agent avoid re-doing the same
   failed action (e.g. retrying a dead-end tool call, re-running the
   same Read twice)? Inverse axis: 10 = zero repeats; 0 = stuck in
   the same loop across multiple turns.

4. **rule_adherence** — Did the agent follow explicit rules from the
   project's Iron Laws (L1-L9 in `rules/iron-laws.md`), `AGENTS.md`, and
   user-stated constraints (worktree protocol, model selection,
   one-shot commands)? 10 = strict adherence; 0 = ignored explicit
   rules repeatedly.

5. **inefficiency** — Was the agent efficient? Did it avoid
   babysit-pr-style misexec (re-reading whole files when a slice
   would do, re-explaining a finished step, spawning duplicate
   sub-agents, paying for cold caches by switching model or context
   mid-session)? Inverse axis: 10 = tight, minimal-turn flow;
   0 = wasteful across multiple turns.

6. **structural_improvement** — Was the agent's output structure
   clean (logical file layout, scoped commits, named sections, runnable
   commands)? 10 = clearly organized; 0 = scattered, missing pieces,
   or hard to navigate.

7. **over_engineering** — Did the agent ship speculative features,
   premature abstractions, or YAGNI cruft (interfaces with one
   implementer, factory patterns for a single impl, "future-proof"
   flags)? Inverse axis: 10 = zero over-eng; 0 = bloated delivery.

8. **thoroughness** — Did the agent verify its work (quoted exit
   codes, reproduced test counts, attached build logs, ran the
   verify step) before declaring done? Did it cite evidence rather
   than claim "done"? 10 = every completion quote + evidence;
   0 = bare "done" with no artifact.

## Output Format

ONLY a JSON object (no prose). 8 axes, each 0-10:

```json
{"intent_alignment":N,"ambiguity_unresolved":N,"repeated_mistakes":N,"rule_adherence":N,"inefficiency":N,"structural_improvement":N,"over_engineering":N,"thoroughness":N}
```
