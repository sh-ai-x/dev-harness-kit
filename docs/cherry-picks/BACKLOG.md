# Cherry-pick backlog — ranked

> Per the user-prescribed thin-harness discipline (3 gates + quarterly
> cap ≤ 2). One-page memo per item at `docs/cherry-picks/<NNN-slug>.md`
> (see `TEMPLATE.md`).

## Adopt (rank #1 — user-named, pre-promoted)

| Rank | Slug | Source | Pattern | Verdict memo | Gates |
|---|---|---|---|---|---|
| **1** | `001-dispatch-mode-classifier` | (user-named) | Auto-classify parallel vs. sequential; remove `--parallel` flag; emit `DispatchDecision { mode, reason }` as first build-log line | `001-dispatch-mode-classifier.md` | All 3 by construction (pure-Python, no hooks, no daemons, net-negative-weight) |

## Adopt (rank #2 — next quarter if budget allows)

| Rank | Slug | Source | Pattern | Verdict memo | Gates |
|---|---|---|---|---|---|
| 2 | `002-subagent-circuit-breaker` | skills-library | Subagent task-brief + review-package + 5-round circuit breaker for the parallel path the dispatch classifier chose | `002-subagent-circuit-breaker.md` | Gate 2 (net-negative); Gate 3 (subagent rules) |
| 3 | `003-skill-authoring-principles` | skills-library | `description:` as trigger phrase; frontmatter as the contract | `003-skill-authoring-principles.md` | Gate 2 (pure docs); Gate 3 N/A |
| 4 | `004-quantitative-gate` | spec-first Agent OS | Named numeric thresholds (e.g. Ambiguity ≤ X, Convergence ≥ Y) as a `/dev-kit:gate` skill | `004-quantitative-gate.md` | Gate 2 (small); Gate 3 (in-process, tmux-safe) |

## Defer

(none this quarter)

## Reject

| Source | Pattern | Why reject | Next-best alternative within dev-kit |
|---|---|---|---|
| skills-library | Auto-invoke meta-skill (session-wide Iron Law) | Conflicts with iron law L8 (prose that duplicates state-machine behavior must be trimmed). Bypasses the dispatch classifier. | None needed — dev-kit iron laws are primary. |
| skills-library | Localhost HTTP/WS visual-companion server | Tmux-orphan risk; daemonized process; conflicts with thin discipline. | Use markdown-only brainstorming (text artifacts in project tree). |
| skills-library | 9-runtime packaging | Out of mission scope for dev-kit. | Stay Claude Code + Codex. |
| skills-library | Frequent upstream renames (6 in v5→v6) | Pinning is mandatory; not a cherry-pick pattern, an operational hazard. | `/dev-kit:reference-bump` skill gates upgrades. |
| spec-first Agent OS | Dedicated MCP server | Version-coupled 0.x churn; competes with dev-kit's MCP integration. | Use spec-first *discipline* (cherry-pick candidate) without the MCP. |
| spec-first Agent OS | Persistent loop (`ralph`) | Tmux-fragile as shipped. | If needed, wrap in `tmux-resilient` checkpoint (out of scope this quarter). |
| spec-first Agent OS | Vendor agent minds (interviewer, seed-architect, etc.) | Vendor-bound personas; cherry-pick the discipline, not the personas. | `/dev-kit:interview` skill (already exists in dev-kit). |
| parallel-team | Whole harness | Wrong runtime; mid OS-refactor upstream. | None — not in scope. |
| parallel-team | Model-tier router by task category | Out of mission scope for dev-kit (model choice is per-call). | None — model selection lives at the call site, not at task routing. |
| parallel-team | 54+ lifecycle hooks | Already achieved by dev-kit's 20+ hook surface. | None — no additional cherry-pick needed. |

## Quarterly cap verification

```
git log --since="3 months ago" --grep="cherry-pick"
```

Current count: 0 (no cherry-picks merged yet).

Cap: ≤ 2 per quarter.
