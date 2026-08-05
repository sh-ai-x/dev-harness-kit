# Deep-read notes — spec-first Agent OS reference category

> Source category: spec-first "Agent OS." ~21 skills + named agent
> "minds" + dedicated MCP server. Loop: interview → seed → run →
> evaluate → evolve. Read once for pattern extraction only.

## Surface observed

- 21 skills, each triggered by a slash-prefix invocation (user-invocable,
  not description-triggered). No session-wide bootstrap injection.
- 9 named agent "minds" exposed as skills or MCP tools.
- One MCP server (`<category>-mcp`) exposing tools like
  `<category>_auto`, `evolve_step`, `session_status`. Required for
  `run` / `evolve` / `auto` modes.
- Loop primitives: `interview` (socratic agent) → `seed` (architect
  produces a `seed.yaml` spec) → `run` (executes the spec) →
  `evaluate` (3-stage gate: mechanical / semantic / multi-model
  consensus) → `evolve` (loop until ontology converges).
- Named gates with quantitative thresholds (e.g. Ambiguity ≤ 0.2,
  Convergence ≥ 0.95, Drift ≤ 0.3).
- Persistent loop (`ralph`) that runs until verified. Does **not**
  survive tmux detach without explicit checkpointing.
- 0.x version line, version-coupled MCP server (MCP must match plugin
  version).

## Patterns worth extracting

| Pattern | Notes for cherry-pick |
|---|---|
| Named quantitative gates (Ambiguity / Convergence / Drift) | Could become a `/dev-kit:gate` skill — concrete numeric thresholds the model must hit before proceeding. In-process, tmux-safe. Cherry-pick candidate. |
| Spec-first discipline (seed → run → evaluate) | Opt-in workflow that complements dev-kit's plan → build → verify. Could surface as a `/dev-kit:spec-first` skill that wraps the reference project's `seed` skill. |
| MCP server coupling | Worth noting as a pattern but not adopting the MCP itself (version-coupled, 0.x line). |

## What NOT to cherry-pick

- The MCP server (version-coupled, 0.x churn, would compete with dev-kit's
  MCP integration).
- The `ralph` persistent loop (tmux-fragile as-shipped).
- The `interview`/`seed` agent minds themselves (vendor-specific personas;
  cherry-pick the *concept* of spec-first discipline, not the personas).
