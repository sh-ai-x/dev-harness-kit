---
paths:
  - "CLAUDE.md"
  - "AGENTS.md"
  - "skills/**/SKILL.md"
  - "commands/**"
---

# Reference coexistence rules (dev-harness-kit)

These rules govern how dev-harness-kit interacts with installed
agent-harness plugins from the broader ecosystem.

## Policy (read-only references, not runtime dependencies)

| Category | Disposition |
|---|---|
| Skills library (pinned v6.2.0) | Install, pin, `autoUpdate: false`, neuter the bootstrap. Methodology reading material. |
| Spec-first Agent OS | Install, opt-in per project, **never** auto-invoke. Spec-first loop is a *different* methodology; useful when explicitly chosen. |
| Parallel-team harness | **Skip install.** Wrong runtime family; mid-refactor upstream. |

## Pinning

- Add `"autoUpdate": false` to the skills-library marketplace entry in
  `~/.claude/plugins/known_marketplaces.json`. Upgrades are gated by
  `/dev-kit:reference-bump`, not by background updates.

## Bootstrap neutering

- A SessionStart hook at `/Users/sanghee/hooks/dev-kit-reference-override.sh`
  emits a higher-priority `additionalContext` declaring dev-kit iron laws
  primary and reference-project bootstraps advisory only.
- The hook is wired in `~/.claude/settings.json` under
  `hooks.SessionStart[]` AFTER any reference-project SessionStart so the
  override block appears later in the system prompt.
- The hook is **idempotent** (no files written, no sockets opened, no
  daemons spawned) and re-invocation within a session produces
  byte-identical stdout. Safe across tmux detach/reattach.

## Auto-invocation discipline

- Skill descriptions from reference projects do **not** trigger
  auto-invocation. Dev-kit-prefixed skills own the loop.
- Dispatch is auto-classified by `/dev-kit:build` — no `--parallel` flag.
- `ooo seed` is opt-in per project; `ooo run` requires the
  reference-project MCP server; `ooo ralph` is tmux-fragile and must
  be wrapped in a checkpoint or skipped in tmux contexts.

## Cherry-pick discipline

- See `docs/cherry-picks/BACKLOG.md` for the ranked, gated backlog.
- Quarterly cap: ≤ 2 cherry-picks per quarter, verified by
  `git log --since="3 months ago" --grep="cherry-pick"`.
- Three thin-harness gates per cherry-pick (see `docs/cherry-picks/TEMPLATE.md`):
  1. Pros/cons memo
  2. Net-negative-weight (bytes added ≤ bytes removed)
  3. tmux + long-running safety classification

## Why these rules exist

dev-harness-kit already covers the overlapping surfaces of every
reference category (TDD, debugging, verification, planning, review).
Wholesale adoption adds duplication without capability and ties
behavior to upstream churn. The reference-policy framing keeps dev-kit
lean while preserving the option to absorb specific patterns under
gated discipline.
