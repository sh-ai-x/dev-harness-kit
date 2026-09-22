---
paths:
  - "**/*"
stale_after: 2027-05-31
---

# Session hygiene rules (dev-harness-kit)

These rules govern *how Claude Code sessions behave* in this repo — model
selection, prompt-cache lifecycle, and tool-call economy. They apply to
every session that makes tool calls, regardless of branch or task.

## Iron Laws

1. **No model or CLAUDE.md swap mid-session.** A single token shift in
   the system prompt invalidates the entire prompt cache. Pick the model
   and the ruleset *before* the first tool call, then ride them out.
   Re-reading CLAUDE.md = billable re-input + cold cache.
2. **Cartography, not re-reads.** Build a structural map of any large
   file once. Subsequent turns reuse the entry point (line range,
   section header, or a `Grep`-narrowed read), never re-read the whole
   file from offset 0. Each "read from line 1 again" double-bills input.
3. **Volatile content stays in the prompt tail, never the prefix.**
   Timestamps, run IDs, ephemeral session IDs, today's date — anything
   that changes per turn — belongs at the *end* of the user message.
   A volatile prefix silently busts the cache on every follow-up turn.
4. **`/compact` and sub-agent delegation, not `/clear` + new session.**
   `/clear` drops the cached prefix and forces a cold re-read on the
   next turn. Reach for `/compact` first; spawn a sub-agent for noisy
   exploration; only escalate to `/clear` when the prefix is genuinely
   unrecoverable.
5. **Match the model to the task.** Opus is reserved for design, spec
   authoring, and architectural judgement. Bug fixes, single-line edits,
   refactors, and ad-hoc Q&A run on Sonnet (default) or Haiku.
   "Important" ≠ "Opus"; a typo fix on Opus shows up immediately in
   `tools/token_efficiency_analyzer.py`'s per-session cost column.
6. **Never re-inject cached context as a user message.** Repeating a
   tool result, file excerpt, or prior-turn output verbatim into the
   next user prompt double-bills input tokens *and* pushes useful
   cached prefix out of the cache. Reference by anchor
   (`see Read on lines 42–87`, `the failing test above`) instead.

## Why these exist

- The prompt cache hit ratio is the single largest cost lever in
  Claude Code. A 50% → 70% shift on a single session scales linearly
  with every session that shares a stable prefix.
- `/clear` resets billable input on the very next turn — a stealth
  cost double-count relative to `/compact`.
- Model mismatch (Opus on a typo fix, Sonnet on a system-design
  decision) is the most common unnecessary-cost signal in
  `tools/token_efficiency_analyzer.py` and `/dev-kit:token-analyzer`.

## Enforcement

| Rule | Mechanism |
|---|---|
| 1 — model/CLAUDE.md swap | `/dev-kit:token-analyzer` cache-hit column; flagged on `CACHE_HIT_LOW` |
| 2 — repeated full-file reads | `/dev-kit:token-analyzer` `READ_HEAVY` warning + per-tool cost tile |
| 3 — volatile prefix | `/dev-kit:token-analyzer` `CACHE_HIT_LOW` + per-session `input vs cache_read` |
| 4 — `/clear` reflex | Reviewer flag on PR summary; no automated hook |
| 5 — model overspec | `/dev-kit:token-analyzer` `MODEL_OVERSPEC` warning |
| 6 — repeated user-message context | `/dev-kit:token-analyzer` `REPEATED_USER_MSG` warning |

## Related

- `tools/token_efficiency_analyzer.py` — per-session cost dashboard.
- `.claude/rules/git-workflow.md` — branch + worktree protocol.
- `iron-laws/index.md` — project Iron Laws (L1-L8).
