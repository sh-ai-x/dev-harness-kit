---
name: cost-gate
category: audit
description: 0-arg cost-gate status. Prints current session spend, threshold distance, and a two-line git-trailer block to include in commits so the PR-level cost flag can aggregate.
alpha: enforcement
when_to_use:
  - User types /dev-kit:cost-gate
  - User wants to know the running session's cost before it hits the warn threshold
  - User is about to commit on a PR branch and needs the Cost-gate trailer
  - User wants visibility into per-session spend without leaving the terminal
allowed-tools: Read Bash
disallowed-tools: Write Edit
model: haiku
user-invocable: true
disable-model-invocation: true
---
> [← Skills index](../../README.md)

# /dev-kit:cost-gate -- read-only cost measurement

Inspect the **live** cost state for the current session. Distinct from
`/dev-kit:token-analyzer` (which is post-hoc over historical JSONL logs).
The cost-gate is observed via this skill during the session itself; it is
**read-only** and never blocks tool calls. There is no cost hook.

## What it does

1. Read the live state file at `$CWD/.dev-kit/.cost-gate/state.json`
   (overridable via `DEV_KIT_COST_GATE_STATE`).
2. Print the running cost, threshold distance, and status (`ok` / `warn`)
   in plain text.
3. Emit the exact two-line git-trailer block the user (or the agent) can
   copy into a commit message so the PR-level aggregator picks it up:

   ```
   Cost-gate: $8.42
   Cost-gate-Session: <session-id>
   ```

The skill is read-only (`disallowed-tools: Write Edit`); the underlying
CLI (`tools/cost_gate_status.py`) writes nothing on its own.

## Why a separate skill, not a flag on `/dev-kit:token-analyzer`

`/dev-kit:token-analyzer` consumes captured JSONL transcripts and renders
a multi-session, multi-day dashboard. `/dev-kit:cost-gate` reads the
**live ledger** for the running session, prints a one-screen status,
and emits the trailer block the PR workflow needs. They are different
stages of the same pipeline (preemptive live vs. post-hoc historical),
and the user expects distinct slash commands.

## Flags

The underlying CLI accepts:

| Flag | Default | Purpose |
|---|---|---|
| `--state PATH` | `$CWD/.dev-kit/.cost-gate/state.json` | State file location |
| `--json` | _(off)_ | Machine-readable JSON to stdout |
| `--html PATH` | _(off)_ | Self-contained HTML status (no JS) |
| `--footer` | _(off)_ | Two-line git trailer for commit messages |
| `--aggregate-pr --bodies-file PATH` | _(off)_ | Aggregate Cost-gate trailers across PR commits |

Threshold overrides (env): `DEV_KIT_COST_WARN_USD`,
`DEV_KIT_PR_COST_FLAG_USD`. Defaults: session warn $5, PR flag $20.

## Output (default text)

```
scope: session  scope_id: sess-abc
status: warn    cost_usd: $5.42
sessions: 1  actual=1  estimated=0
input=12450  output=2100  cache_read=89000
session_warn: $5.00  pr_flag: $20.00
warnings: ['cost $5.42 >= warn $5.00']
state_path: /Users/.../dev-harness-kit/.dev-kit/.cost-gate/state.json
```

## Hand-off

The trailer block printed by `--footer` is the input the PR-level cost
flag (`.github/workflows/cost-flag.yml`) aggregates. If the user commits
on a PR branch, append the two lines to the commit body so the
aggregator picks them up:

```bash
git commit -m "feat: thing" -m "$(python3 tools/cost_gate_status.py --footer)"
```

The aggregator dedupes repeated `Cost-gate-Session` snapshots by keeping
the maximum cumulative value per session.

## Iron Laws

- **Read-only.** This skill does not modify state. The CLI prints; it
  does not write.
- **No blocking.** The cost-gate is observed only. There is no hook and
  no `session_kill` threshold; the cost-gate cannot deny a tool call.
- **Quote the summary line.** The CLI prints `scope=... status=...
  cost_usd=...` on success. Copy that line verbatim into the
  conversation so the user can audit without re-running.
- **Stdout vs stderr contract.** The status summary goes to stdout. This
  skill never emits a deny JSON or non-zero exit code for high cost.

## Related

- `tools/cost_gate_status.py` -- CLI driver (stdlib-only).
- `lib/cost_gate.py` -- pricing table, transcript scanner, threshold
  evaluation, footer parsing, PR aggregation. Independent of
  `tools/token_efficiency_analyzer.py`.
- `.github/workflows/cost-flag.yml` -- PR-level aggregator that applies
  the `cost-flag` label when cumulative cost exceeds $20.

Next: open `state.json` directly to inspect the ledger, or use
`--html PATH` to render a single-file status report.
