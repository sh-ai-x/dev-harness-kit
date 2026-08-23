> [← Skills index](README.md) · [Project README](../../README.md)

# `token-analyzer`

**Category:** `audit` · **Alpha:** `analysis` · **Invocation:** `/dev-kit:token-analyzer` (human-invoked)

`token-analyzer` turns the JSONL session transcripts captured by `/dev-kit:log` into a per-repository, last-N-days token-efficiency dashboard: 4-dimension session scoring, 6 anti-pattern warnings, and a USD savings estimate. It is its own skill rather than an `--html` flag on `/dev-kit:log` because `/dev-kit:log` only toggles transcript capture (on/off/status/setup) while this skill *consumes* those transcripts — capture and analysis are different pipeline stages that deserve distinct slash commands.

## When to use it

- The user types `/dev-kit:token-analyzer`.
- The user wants to know where token spend is going in their Claude Code / Codex sessions.
- The user suspects prefix misalignment, redundant `Read` calls, or model-overspec patterns.
- The user wants a pre-release FinOps review of session-level cost.

## How it works

1. Confirm transcripts exist under `logs/claude-code/<branch>/` and/or `logs/codex/<branch>/` (a recursive walk; legacy flat files at the top level are also picked up and bucketed under branch `main`). If neither location has transcripts, the skill points the user at `/dev-kit:log setup` + `/dev-kit:log on` instead of running.
2. Detect the repo name from the most common `cwd` basename across captured sessions, or accept `--repo <name>` to override; if the user did not pass `--repo`, the skill confirms the derived name with the user before running.
3. Invoke `tools/token_efficiency_analyzer.py --repo <name> --days 30` and capture its `[ok] sessions=N files_scanned=M total_cost=$... estimated_savings=$...` summary line.
4. Echo the summary plus the output HTML path to the user, resolved and printed as a **relative**, `./`-prefixed path (never an absolute `/Users/...` path, since the user may be on a different machine, worktree, or symlinked mount).

The skill itself is read-only (`disallowed-tools: Write Edit`); the Python CLI writes the file directly, mirroring how the rest of the dev-kit audit/inspect skills keep their skill body pure and let the driver own I/O. Example emitted lines:

```
[ok] sessions=14  files_scanned=14  total_cost=$1.23  estimated_savings=$0.01  stale_cost=$0.00  transcripts=14
Open: ./docs/observability/dashboard-dev-harness-kit-30d.html
```

The `transcripts=N` field counts per-session sidecar pages written under `<out-stem>.assets/<worktree>/`, linked from the dashboard's **Transcript Index** via relative `<a href>` — navigation is plain links with no JS or server, so a worktree's transcripts load lazily only when clicked. Pass `--no-transcripts` for an index-only run. Do not read the output HTML back into the conversation — it is a binary-ish artifact best opened in a browser.

## Usage

```bash
/dev-kit:token-analyzer [--repo NAME] [--days N] [--logs-dir PATH] [--branch NAME]
                         [--out PATH] [--transcripts | --no-transcripts]
                         [--cost-gate-tokens N] [--cost-gate-usd N]
                         [--pricing-override PATH] [--json]
```

| Flag | Default | Purpose |
|---|---|---|
| `--repo <name>` | (required unless auto-detected from cwd) | Matches `Path(cwd).name` |
| `--days <n>` | `30` | Look-back window |
| `--logs-dir <path>` | `./logs` | Root for `claude-code/` + `codex/` subdirs (recursively walked) |
| `--branch <name>` | _(all)_ | Filter to a single branch (case-insensitive substring on `gitBranch`); empty = no filter |
| `--out <path>` | `docs/observability/dashboard-<repo>-<days>d.html` | Output HTML path (sidecars land in `<out-stem>.assets/`) |
| `--transcripts` / `--no-transcripts` | `--transcripts` (on) | Write per-session full-transcript sidecar pages and link them from the Transcript Index; `--no-transcripts` = index-only, inert Open cells |
| `--cost-gate-tokens <int>` | `200000` | Per-session `input + cache_read` gate; sessions over this trigger a stderr WARN |
| `--cost-gate-usd <float>` | `5.00` | Per-session USD gate; sessions over this trigger a stderr WARN |
| `--pricing-override <path>` | _(none)_ | JSON file overriding the PRICING dict (`{tier: {in, out, cache_write_5m, cache_write_1h, cache_read}}`) |
| `--json` | _(off)_ | Emit machine-readable JSON summary to stdout, skip HTML write; exit code 3 on `cost_gate=bad` |

## Output

One self-contained HTML file (default `docs/observability/dashboard-<repo>-30d.html`): inline `<style>` only, no `<script>`, no external assets, dark-mode aware. Both `claude-code` and `codex` transcripts are first-class sources; the Cost Gate evaluates sessions from either. Sections, rendered by `tools/token_efficiency_analyzer.py:render_dashboard`:

- **Cost Gate banner** — green `ok` / amber `warn` / red `bad`, with offending session IDs and reasons, driven by `--cost-gate-tokens` and `--cost-gate-usd`.
- **Overview** — 4 metric tiles: active sessions, total cost, avg score (with letter-grade badge), avg cache hit ratio.
- **Cost & Token Distribution** — cost by repo (share bar, all repos in window) + cost by tool (share bar, amber banner if `Read` is #1).
- **Cost by Branch** — per-branch share bar across every branch in the window, sourced from the `gitBranch` wire field with a path fallback for legacy flat files; `--branch <name>` focuses the rest of the report.
- **Cost by Worktree (with State column)** — same shape plus a `State` column (`live` / `merged` / `gone` / `main`) for every worktree dir under `.worktrees/*/`: `live` = still in `git worktree list` with unique commits vs `origin/main`; `merged` = still listed but the branch tip is an ancestor of `origin/main` (safe to delete); `gone` = dir survives on disk but is no longer in `git worktree list`. An amber `stale` chip prefixes any Sessions row whose worktree is `merged` or `gone`; `--worktree <name>` focuses on one.
- **Overview, 5th tile (Stale Cost)** — dollar value of every `merged` / `gone` session, with its percentage of total.
- **Cost by Model & Cache TTL Mix** — per-model spend table + a four-bar Cache TTL Mix (`cache_read` / `write 5m` / `write 1h` / `pure miss`) with a TTL pricing caveat.
- **Sessions** — per-session row: branch, model, start time, input/output/tools/cache-hit/cost, score pill + letter grade, warning chips.
- **ROI Actions (ranked by estimated savings)** — deduplicated warnings sorted descending by `estimated_save_usd` with a priority tag.
- **Actionable Insights & Estimated Savings** — USD savings callout split into cache-miss / dup-read / model-downgrade sub-reclaims + deduplicated warning blocks.
- **Recommended Optimizations** — do/don't list per warning code, green check for codes that fired, muted for codes that didn't.

### Scoring rubric (4 dimensions, 0–100 weighted, with letter grade)

| Dim | Weight | Formula | Penalizes |
|---|---:|---|---|
| Cache Utilization | 0.40 | stepped: `0..0.50` → `0..50` (1:1), `0.50..0.85` → `50..100`, `≥0.85` → `100` | prefix misalignment |
| Output Density | 0.20 | `min(100, output / total_input * 400)` | read-only sessions |
| Read Redundancy | 0.20 | `max(0, 100 - (max_repeat_reads - 1) * 12.5)` | cartography failure |
| Tool Economy | 0.20 | `max(0, 100 - tools_per_1k_out * 2)` | tool thrashing |

Total = `0.40*cache + 0.20*density + 0.20*redundancy + 0.20*economy`. Letter grade bands: `A: ≥90`, `B: ≥80`, `C: ≥70`, `D: ≥60`, `F: <60` — rendered as a colored badge in the Overview tile and every per-session row.

### Pricing model (USD per 1M tokens, per-tier)

| Tier | in | out | cache_write_5m | cache_write_1h | cache_read |
|---|---:|---:|---:|---:|---:|
| opus        | 15.0000 | 75.0000 | 18.7500 | 30.0000 | 1.5000 |
| sonnet      |  3.0000 | 15.0000 |  3.7500 |  6.0000 | 0.3000 |
| haiku       |  0.8000 |  4.0000 |  1.0000 |  1.6000 | 0.0800 |
| gpt-5-codex |  1.2500 | 10.0000 |  1.2500 |  1.2500 | 0.6250 |
| gpt-5       |  1.2500 | 10.0000 |  1.2500 |  1.2500 | 0.6250 |
| gpt-4.1     |  2.5000 | 10.0000 |  2.5000 |  2.5000 | 1.2500 |
| gpt-4o      |  2.5000 | 10.0000 |  2.5000 |  2.5000 | 1.2500 |
| o3          | 10.0000 | 40.0000 | 10.0000 | 10.0000 | 5.0000 |
| o4-mini     |  1.1000 |  4.4000 |  1.1000 |  1.1000 | 0.5500 |

Anthropic 5m TTL write = 1.25x base input; 1h TTL write = 2.0x base input. OpenAI has a single cached-input discount (~50% of base input) and no TTL split, so both cache-write columns equal base input. Any tier can be overridden with `--pricing-override <path>.json`. Unknown model ids fall back to sonnet pricing and print a stderr WARN line.

### Warning triggers (6 anti-patterns) with reclaim-axis attribution

Each trigger has the exact emoji-prefixed message from the prompt, rendered verbatim in the dashboard. Each `Warning` carries `estimated_save_usd`, `priority` (1–4), and `reclaim_axis` (`cache_miss` | `dup_read` | `model_downgrade` | `""`) so ROI actions can be ranked by dollar value.

| Code | Condition | Fix | Reclaim axis |
|---|---|---|---|
| `CACHE_HIT_LOW` | `cache_hit < 50%` | move volatile data to prompt tail; don't switch models mid-session | `cache_miss` |
| `READ_HEAVY` | `Read` ≥ 40% of tool cost | pin large files once; build a cartography | `dup_read` |
| `HEAVY_CONTEXT` | `total_input > 500K` in one session | delegate to sub-agents; run `/compact` | `cache_miss` |
| `MODEL_OVERSPEC` | Opus + density score < 20 | downgrade to Sonnet / Haiku | `model_downgrade` |
| `WRITE_NOT_REUSED` | `cache_write > 50K` AND `cache_read < 2*cache_write` | only put re-readable data in front of the prompt | `cache_miss` |
| `REPEATED_USER_MSG` | any user message text appears ≥ 2x | drop finished sub-tasks from context | `cache_miss` |

### Estimated savings (USD) — three reclaim axes

A conservative reclaim model: only the cache-miss + duplicate-read + model-downgrade penalty is reclaimed, not the entire bill. Target = 85% cache hit (Anthropic's recommended minimum) + 0 duplicate reads + Opus sessions with density<20 swapped to Sonnet.

- **Cache-miss delta** (`cache_miss_reclaim`): shift tokens from billable input into `cache_read` until the session hits 85%; saved = `shifted * (input_price - cache_read_price)`.
- **Duplicate-read delta** (`dup_read_reclaim`): `2K tokens * (n - 1)` per file read more than once, at base input price.
- **Model-downgrade delta** (`model_downgrade_reclaim`): for Opus sessions with density<20, recompute cost under Sonnet pricing for the same token volume and take the diff.

Per-tool cost is imputed from `n_calls * 2K_tokens * input_price` — a heuristic, not a billing-API call.

## Iron Law

Quote the summary line in your reply, not a paraphrase: the CLI prints `[ok] sessions=N files_scanned=M total_cost=$X.XX estimated_savings=$Y.YY stale_cost=$Z.ZZ` on success; copy it verbatim so the user can audit the numbers without opening the HTML. Do not claim "done" or "passed" without that line.

Stdout vs stderr contract: the `[ok]` summary line goes to stdout. Cost Gate WARN lines, unknown-model WARN lines, and worktree-classification WARN lines go to stderr — a consumer parsing stdout must never see a WARN line in it. Exit code 3 means `cost_gate=bad` under `--json` only; HTML mode always exits 0 unless the log dir is empty (exit 2).

## Related

- `tools/token_efficiency_analyzer.py` — the CLI driver (stdlib only, py_compile-verified).
- `fixtures/make_fixture.py` — generates 6 synthetic JSONL files, one per warning trigger, for regression.
- `tests/test_token_efficiency_analyzer.py` — 13 unit tests covering the scoring curve, letter grade, per-warning $ attribution, Cost Gate, unknown-model warn, pricing override, and end-to-end HTML + JSON outputs.
- `/dev-kit:log` — captures the transcripts this skill consumes.
- [cost-gate](cost-gate.md) — the live, single-session counterpart to this post-hoc, multi-session dashboard.

---
*Source: [`skills/token-analyzer/SKILL.md`](../../skills/token-analyzer/SKILL.md)*
