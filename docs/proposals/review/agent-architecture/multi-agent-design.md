# Multi-agent design — proposal (orientation)

> The authoritative source for this proposal is
> [`multi-agent-design.yaml`](multi-agent-design.yaml) (the prose this doc
> references), kept human-edited. The rendered HTML sibling
> [`multi-agent-design.html`](multi-agent-design.html) is auto-regenerated
> by `/dev-kit:proposal` for review-friendly presentation. When the two
> disagree, the YAML is the source of truth.

This proposal answers three questions in order:

1. **Decision gate** — what test decides whether a candidate subagent
   deserves a project agent file at all? (A three-check rule:
   solvable by a skill, generates N independent targets, corpus-evidenced.)

2. **Candidates run through the gate** — corpus-evidenced for
   `worktree-janitor` (38/160 sessions manually ran `git worktree
   remove`), rejected for `skill-auditor` and `session-cost-reviewer`
   (no evidence, no recurring target count).

3. **Hand-off contract** — the dispatch and report envelopes that
   orchestrator and subagent speak in. Star topology only, agent-to-agent
   calls are forbidden, summary caps per Anthropic's
   [effective-context-engineering guide](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

The shipped artifacts are `agents/worktree-janitor.md` for Claude
Code and `agents/worktree-janitor.toml` for Codex. Both define the
same read-only auditor for `.worktrees/*`, which classifies every worktree
via `tools/token_efficiency_analyzer.py:classify_all_worktrees()` and
reports removal candidates. Neither runs `git worktree remove`; the
orchestrator reads the report and a human runs the removal command.

For the orchestrated subagent dispatch contract (two envelopes — dispatch
+ report) and the multi-agent architecture research informing this
proposal, see
[`../../architecture/multi-agent-orchestration-research.md`](../../architecture/multi-agent-orchestration-research.md).
