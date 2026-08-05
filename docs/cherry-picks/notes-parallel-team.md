# Deep-read notes — parallel-team harness reference category

> Source category: full agent harness config for a non-Claude-Code
> runtime. Multi-agent orchestrator + 54+ lifecycle hooks + 5 built-in
> MCPs. Currently mid OS-refactor upstream. **Not installed; read via
> GitHub README + docs only.**

## Surface observed (no install — read-only)

- Targets a different runtime family than this repo's primary
  (Claude Code / Codex). Direct integration not possible.
- 11 named agents (orchestrator + several specialists).
- 54+ lifecycle hooks — broad instrumentation across session events.
- 5 built-in MCPs for code search / web context.
- Multi-model router that picks model tier by task category
  (visual-engineering / deep / quick / ultrabrain).
- Marketing tagline: "the one and only agent harness for complex
  codebases." Upstream is mid OS-refactor — API surface is not stable.

## Patterns worth extracting

| Pattern | Notes for cherry-pick |
|---|---|
| Model tier routing by task category | Interesting concept but out of mission scope for dev-kit (which is harness-agnostic; model choice is per-call, not per-task). |
| Multi-agent parallel orchestration | Already addressed by this repo's dispatch-mode classifier (cherry-pick #1). No additional cherry-pick needed. |
| Broad lifecycle hook instrumentation | Already achieved by this repo's 20+ hook surface. No additional cherry-pick. |

## What NOT to cherry-pick

- The whole harness (wrong runtime).
- Any specific agent persona (vendor-bound).
- Built-in MCPs (vendor-bound).

## Net conclusion

The parallel-team category reinforces two existing decisions rather than
adding a new cherry-pick:

1. **Dispatch-mode classifier is correct** (this category's whole value
   prop is parallel orchestration; this repo's classifier covers the
   same need with auto-classification instead of opt-in).
2. **No multi-runtime packaging cherry-pick** (the parallel-team
   category's reach across runtimes is part of why direct integration
   is impossible).

**Backlog delta:** zero new adopt / defer / reject items from this
category. Documented here so it isn't re-investigated next quarter.
