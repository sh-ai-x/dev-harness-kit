# Multi-agent orchestration research

Research question: when does an orchestrator + parallel-subagent
architecture outperform a single agent, what orchestration pattern is
most effective, and what does that mean for this repo's Claude
`agents/*.md` and Codex `agents/*.toml` design?

## Sources

- [Anthropic Engineering — "How we built our multi-agent research system"](https://www.anthropic.com/engineering/multi-agent-research-system)
  (primary; fetched 2026-07-30). Documents the orchestrator-worker
  architecture that Claude's Research feature runs on: lead agent
  plans, spins up 3–5 subagents in parallel, synthesizes their
  findings. Performance was 90.2% better than single-agent Claude
  Opus 4 on complex, multi-source tasks. Token cost was ~15x chat.
- [Anthropic Engineering — "Building Effective AI Agents"](https://www.anthropic.com/engineering/building-effective-agents)
  (primary; fetched 2026-07-30). The orchestrator-worker pattern is
  recommended specifically when "subtasks cannot be pre-defined,"
  i.e. when the decomposition itself needs judgment.
- [Anthropic Engineering — "Effective context engineering for AI agents"](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  (primary; fetched 2026-07-30). The isolation mechanism is
  summarization, not process separation: each subagent burns
  tens of thousands of tokens internally but returns only a
  "condensed, distilled summary of its work (often 1,000–2,000
  tokens)."
- [Digital Applied — "Multi-Agent Orchestration: 5 Patterns That Work in 2026"](https://www.digitalapplied.com/blog/multi-agent-orchestration-5-patterns-that-work)
  (secondary; fetched 2026-07-30). Cross-validated the four pattern
  shapes — supervisor, fan-out, pipeline, debate — with measured
  cost multipliers and per-pattern failure modes.

## What Anthropic found (primary)

- **Architecture that worked**: a lead agent delegates to 3–5
  subagents running in parallel, each also using multiple tools
  concurrently. Cut research time by up to 90% on complex, multi-source
  tasks.
- **Each subagent task spec**: "an objective, an output format,
  guidance on the tools and sources to use, and clear task boundaries."
  Vague instructions caused duplicated work or gaps.
- **The actual isolation mechanism**: a subagent may burn tens of
  thousands of tokens internally but returns only a condensed summary,
  ~1,000–2,000 tokens — that compression step is what keeps the
  orchestrator's context clean, not running in a separate process.
- **When orchestrator-workers beats plain parallelization**:
  specifically when "subtasks cannot be pre-defined."
- **When to use neither**: "optimizing single LLM calls... is usually
  enough." Agentic complexity is justified only when the simpler
  approach demonstrably underperforms — it trades latency and cost
  and adds "the potential for compounding errors."
- **Cost**: single-agent research ≈4x chat tokens; multi-agent
  research ≈15x chat tokens. Subagent failures observed include
  spawning for simple queries that didn't need them, subagents
  endlessly searching for sources that don't exist, and picking
  SEO-optimized content over authoritative sources when instructions
  were imprecise.

## Broader 2026 industry pattern survey (secondary)

| Pattern | Shape | Cost vs. single model | Typical failure | Mitigation |
|---|---|---|---|---|
| **Supervisor** | hierarchical delegation, non-overlapping specialists | ~(N+1)x | over-delegation loops, unbounded re-attempts | iteration ceiling (~25 turns, Claude SDK default) |
| **Fan-Out** | parallel scatter-gather | ~Nx | silent partial results from a failed branch | explicit fail-whole vs. return-partial policy |
| **Pipeline** | sequential chain | ~Nx | cascade contamination from a bad mid-stage output | per-stage validation |
| **Debate** | same prompt to N agents, judge arbitrates | ~1.2–2.5x | judge bias, arbitration loops | hard max round count |
| **Swarm** | peer, shared memory | unbounded without caps | runaway spawning, thrashing | population caps |

Cross-pattern discipline: implement "a hard cost cap in the agent
harness — not a post-hoc billing alert."

## Most effective for this repo: Supervisor + Fan-Out

Three reasons, each tied to a citation:

1. **Best-understood failure modes**: over-delegation → fixed with
   an iteration ceiling. Avoids the loop-style risks that Debate
   (arbitration loops) and Swarm (thrashing) need explicit guards
   against.
2. **Anthopic's own bar for orchestrator-workers** —
   "subtasks cannot be pre-defined" — is exactly the target-count
   test this repo applied (`worktree-janitor` survived;
   `skill-auditor`/`session-cost-reviewer` rejected).
3. **Star topology** (orchestrator ↔ subagent, never
   subagent ↔ subagent) structurally avoids the loop-style
   failures other patterns need explicit guards against.

## How this was applied

The decision gate, candidates table, and recommended subagent build
for this repo live in
[`../proposals/review/agent-architecture/multi-agent-design.md`](../proposals/review/agent-architecture/multi-agent-design.md)
(a short orientation, with the substantive proposal YAML at
`multi-agent-design.yaml`). The hand-off envelopes
(dispatch + report) that this research informed are documented inline
in that proposal's "Recommended build" section.
