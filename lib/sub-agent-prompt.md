<!--
  sub-agent-prompt.md — ACP canonical sub-agent hand-off template.

  This is a TEMPLATE, not a discoverable skill. Living in `lib/`
  (next to acp_dispatch.py) rather than `skills/` keeps it out of the
  slash-command namespace without needing the `_` prefix convention
  (per rules/skill-authoring.md).

  The M (orchestrator) copies this template into every dispatch and
  populates all seven placeholders. tests/test_acp_hand_off.py refuses
  any dispatch prompt missing a placeholder.

  See docs/architecture/acp-harness.md §3 for the contract.
-->

# ACP dispatch: <TIER> on branch `<BRANCH>`

## Tier-assertion (mandatory on your first tool call of this session)

Emit this exact line, verbatim, before any other tool call:

```
[tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>.
```

`<N>` is `1` for M, `2` for T, `3` for L. `<OWNERSHIP_SENTENCE>` is one of:

- M: `the round state and dispatch decisions only`
- T: `ONE PR's lifecycle on branch <BRANCH>`
- L: `read-only investigation for T on branch <BRANCH>; no edits`

If the tier-assertion lint (`hooks/acp-tier-assert.sh`) denies your first
tool call, read the missing-field reason, re-emit the corrected
tier-assertion, then retry.

## Session context (resolved by the orchestrator — do not edit)

| Field | Value |
|---|---|
| `<TASK>` | <TASK> |
| `<BRANCH>` | <BRANCH> |
| `<WORKTREE_PATH>` | <WORKTREE_PATH> |
| `<CWD>` | <CWD> |
| `<PLUGIN_VERSION_TARGET>` | <PLUGIN_VERSION_TARGET> |
| `<LOCK_FILE>` | <LOCK_FILE> |
| `<PARENT_SESSION_CWD>` | <PARENT_SESSION_CWD> |

Your session cwd MUST be `<CWD>` (which equals `<WORKTREE_PATH>`). If your
session started in `<PARENT_SESSION_CWD>` instead, the parent-cwd misfire
gate (`hooks/acp-cwd-discipline.sh`) will deny your Bash calls — re-root
your session before proceeding.

## Locks

`<LOCK_FILE>` is held by M for the duration of your dispatch. Do not touch
any other agent's lock file under `<orch_worktree>/.dev-kit/round-<descriptor>/locks/`.

## Plugin version

Your target version is `<PLUGIN_VERSION_TARGET>` (computed by `bin/version-slot
compute <PR_INDEX>`). Before pushing, run `bin/version-slot check`; if it
exits 1, run `bin/version-slot pin <PR_INDEX>` to re-pin both
`.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`, then commit
the pin, then push with `--force-with-lease`.

## Task

<TASK>

## Done conditions (do not exceed)

When the task above is complete and verified, emit:

```
[tier-done] Tier <N> (<M|T|L>) on branch <BRANCH>: <one-line exit summary>.
```

Then stop. Do not auto-dispatch a follow-up. M decides what runs next.

## Related

- `docs/architecture/acp-harness.md` §2 (tier-cognition), §3 (this template), §4 (version-slot), §5 (cwd-discipline), §6 (round-meta).
- `rules/git-workflow.md` — branch + worktree + PR conventions.
- `rules/session-hygiene.md` — model selection + cache discipline (volatile content stays in prompt tail).