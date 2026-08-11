---
name: token-analyzer
category: audit
description: 0-arg token-efficiency dashboard. Runs tools/token_efficiency_analyzer.py over logs/{claude-code,codex}/*.jsonl to produce an HTML report (+ lazy per-worktree transcript sidecars) -- 4-dim session scoring, 6 anti-pattern warnings, USD savings estimate.
alpha: analysis
when_to_use: |
  - User types /dev-kit:token-analyzer
  - User wants to know where token spend is going in their Claude Code / Codex sessions
  - User suspects prefix misalignment, redundant Read, or model overspec patterns
  - Pre-release FinOps review of session-level cost
allowed-tools: Read Write Bash
disallowed-tools: Write Edit
model: haiku
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:token-analyzer -- token efficiency dashboard

Generate an HTML dashboard that turns the JSONL session
transcripts captured by `/dev-kit:log` into a per-repository, last-N-days
view of token spend, session efficiency, and anti-patterns. Distinct
human action ("see the spend picture") with a distinct artifact (an
`.html` file plus a `.assets/` sidecar tree), so it earns its own skill
rather than an `--html` flag on another command.

The dashboard's **Transcript Index** section links each worktree to a
sidecar page under `<out>.assets/<worktree>/index.html`, which in turn
links each session to its full raw transcript
(`<out>.assets/<worktree>/<session>.html`). Navigation is plain
`<a href>` — the browser loads a worktree's transcripts only when
clicked, so drill-down is lazy and works under `file://` (no JS, no
server). Pass `--no-transcripts` for an index-only run.

**Why a separate skill, not a `--html` flag on `/dev-kit:log`**:
`/dev-kit:log` toggles transcript capture (on/off/status/setup); the
analyzer consumes those transcripts. They are different stages of the
same pipeline -- capture vs. analyze -- so the user expects distinct
slash commands.

## What it does

1. Confirm transcripts exist under `logs/claude-code/<branch>/` and/or
   `logs/codex/<branch>/` (recursive walk — legacy flat files at the
   top level are also picked up and bucketed as branch `main`).
   If neither exists, point the user at `/dev-kit:log setup` +
   `/dev-kit:log on` to enable capture first.
2. Detect the repo name from the most common `cwd` basename in the
   captured sessions (or accept `--repo <name>` to override).
3. Invoke `tools/token_efficiency_analyzer.py --repo <name> --days 30`
   and capture its `[ok] sessions=N  files_scanned=M  total_cost=$
   ...  estimated_savings=$...` summary line.
4. Echo the summary + the **relative** output HTML path to the user.
   **Path form contract**: resolve the absolute path actually written
   (`Path(out).resolve()`) against the user's CWD and emit a `./`-prefixed
   relative path. **Never** print an absolute path (`/Users/...`) -- the
   user is on a different machine, in a different worktree, or behind a
   symlinked mount. Example emitted lines:

   ```
   [ok] sessions=14  files_scanned=14  total_cost=$1.23  estimated_savings=$0.01  stale_cost=$0.00  transcripts=14
   Open: ./token-dashboard-dev-harness-kit-30d.html
   ```

   The `transcripts=N` field counts the per-session sidecar pages written
   under `./token-dashboard-dev-harness-kit-30d.assets/<worktree>/`. The
   dashboard links them from its **Transcript Index** section via relative
   `<a href>`, so keep the `.assets/` dir next to the `.html` when sharing.
   Do not try to read the HTML back into the conversation -- it is a
   binary-ish artifact best opened in a browser.

The skill is read-only (`disallowed-tools: Write Edit`); the Python
CLI writes the file directly, mirroring how `/dev-kit:inspect --html`
keeps the skill body pure and lets the driver handle I/O.

## Flags

The CLI accepts:

| Flag | Default | Purpose |
|---|---|---|
| `--repo <name>` | (required unless auto-detected from cwd) | Matches `Path(cwd).name` |
| `--days <n>` | `30` | Look-back window |
| `--logs-dir <path>` | `./logs` | Root for `claude-code/` + `codex/` subdirs (recursively walked) |
| `--branch <name>` | _(all)_ | Filter to a single branch (case-insensitive substring on `gitBranch`). Empty = no filter. |
| `--out <path>` | `token-dashboard-<repo>-<days>d.html` | Output HTML path (sidecars land in `<out-stem>.assets/`) |
| `--transcripts` / `--no-transcripts` | `--transcripts` (on) | Write per-session full-transcript sidecar pages under `<out>.assets/` and link them from the Transcript Index. `--no-transcripts` = index-only, inert Open cells |
| `--cost-gate-tokens <int>` | `200000` | Per-session `input + cache_read` gate; sessions over this trigger stderr WARN |
| `--cost-gate-usd <float>` | `5.00` | Per-session USD gate; sessions over this trigger stderr WARN |
| `--pricing-override <path>` | _(none)_ | JSON file overriding the PRICING dict (shape: `{tier: {in, out, cache_write_5m, cache_write_1h, cache_read}}`) |
| `--json` | _(off)_ | Emit machine-readable JSON summary to stdout, skip HTML write. Exit code 3 on `cost_gate=bad`. |

If the user did not pass `--repo`, derive it from the most common
basename of `cwd` in the captured sessions and confirm with the user
before running.

## Output

One HTML file (default name `token-dashboard-<repo>-30d.html`).
Self-contained: inline `<style>`, no `<script>`, no external assets.
Dark-mode aware. Safe to email, archive, or open from `file://`.
Both `claude-code` and `codex` CLI transcripts are first-class sources;
the Cost Gate evaluates sessions from either source.

Sections (rendered by `tools/token_efficiency_analyzer.py:render_dashboard`):

- **Cost Gate banner** (top of page): green `ok` / amber `warn` / red `bad`
  with the offending session IDs and reasons. Driven by `--cost-gate-tokens`
  and `--cost-gate-usd`.
- **Overview**: 4 metric tiles -- active sessions, total cost, avg
  score (with letter grade badge), avg cache hit ratio.
- **Cost & Token Distribution**: cost by repo (share bar, all repos in
  window -- not just the filtered one) + cost by tool (share bar, with
  yellow banner if `Read` is #1).
- **Cost by Branch**: per-branch share bar across every branch present in
  the window -- sourced from the `gitBranch` wire field with a path
  fallback for legacy flat files. Use `--branch <name>` to focus the rest
  of the report on a single branch.
- **Cost by Worktree (with State column)**: same shape as the Branch panel
  plus a `State` column (`live` / `merged` / `gone` / `main`) for every
  worktree dir on disk under `.worktrees/*/`. `live` = still in
  `git worktree list` and has unique commits vs `origin/main`. `merged`
  = still listed but the branch tip is an ancestor of `origin/main` (safe
  to delete). `gone` = dir exists on disk but is no longer in
  `git worktree list` (worktree was `git worktree remove`'d, dir survived).
  An amber `stale` chip prefixes any Sessions row whose worktree is
  `merged` or `gone`. Use `--worktree <name>` to focus on a single one.
- **Overview, 5th tile (Stale Cost)**: dollar value of every `merged` /
  `gone` session, with the percentage of total in the delta line. Lets
  you gauge the spend left behind by stale worktrees at a glance.
- **Cost by Model & Cache TTL Mix**: per-model spend table + four-bar
  Cache TTL Mix showing `cache_read` / `write 5m` / `write 1h` / `pure miss`
  token share with a TTL pricing caveat.
- **Sessions**: per-session row -- branch, model, start time, input/output/
  tools/cache-hit/cost, score pill **+ letter grade**, warning chips.
- **ROI Actions (ranked by estimated savings)**: deduplicated
  warnings sorted descending by `estimated_save_usd` with priority tag.
- **Actionable Insights & Estimated Savings**: USD savings callout
  (green gradient) split into cache-miss / dup-read / model-downgrade
  sub-reclaims + deduplicated warning blocks.
- **Recommended Optimizations**: do/don't list per warning code --
  green ✓ for the code that fired, muted ✗ for codes that didn't.

## Scoring rubric (4 dimensions, 0-100 weighted, with letter grade)

| Dim | Weight | Formula | Penalizes |
|---|---:|---|---|
| Cache Utilization | 0.40 | stepped: `0..0.50` → `0..50` (1:1), `0.50..0.85` → `50..100`, `≥0.85` → `100` | prefix misalignment |
| Output Density | 0.20 | `min(100, output / total_input * 400)` | read-only sessions |
| Read Redundancy | 0.20 | `max(0, 100 - (max_repeat_reads - 1) * 12.5)` | cartography failure |
| Tool Economy | 0.20 | `max(0, 100 - tools_per_1k_out * 2)` | tool thrashing |

Total = `0.40*cache + 0.20*density + 0.20*redundancy + 0.20*economy`.

Letter grade bands: `A: ≥90`, `B: ≥80`, `C: ≥70`, `D: ≥60`, `F: <60`.
Rendered as a colored badge next to the numeric score in the Overview
tile and every per-session row.

## Pricing model (USD per 1M tokens, per-tier)

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

Anthropic 5m TTL write = 1.25x base input; 1h TTL write = 2.0x base
input. OpenAI has a single cached-input discount (~50% of base input)
and no TTL split, so both cache-write columns equal base input. Override
any tier with `--pricing-override <path>.json`. Unknown model ids fall
back to sonnet pricing AND print a stderr WARN line.

## Warning triggers (6 anti-patterns) with reclaim-axis attribution

Each trigger has the exact emoji-prefixed message from the prompt;
rendered verbatim in the dashboard. Each `Warning` dataclass carries
`estimated_save_usd`, `priority` (1-4), and `reclaim_axis`
(`cache_miss` | `dup_read` | `model_downgrade` | `""`) so the dashboard
can rank ROI actions by dollar value.

| Code | Condition | Fix | Reclaim axis |
|---|---|---|---|
| `CACHE_HIT_LOW` | `cache_hit < 50%` | move volatile data to prompt tail; don't switch models mid-session | `cache_miss` |
| `READ_HEAVY` | `Read` >= 40% of tool cost | pin large files once; build a cartography | `dup_read` |
| `HEAVY_CONTEXT` | `total_input > 500K` in one session | delegate to sub-agents; run `/compact` | `cache_miss` |
| `MODEL_OVERSPEC` | Opus + density score < 20 | downgrade to Sonnet / Haiku | `model_downgrade` |
| `WRITE_NOT_REUSED` | `cache_write > 50K` AND `cache_read < 2*cache_write` | only put re-readable data in front of prompt | `cache_miss` |
| `REPEATED_USER_MSG` | any user message text appears >= 2x | drop finished sub-tasks from context | `cache_miss` |

## Estimated savings (USD) — three reclaim axes

Conservative reclaim model. Only the cache-miss + duplicate-read +
model-downgrade penalty is reclaimed, not the entire bill. Target = 85%
cache hit (Anthropic's recommended minimum) + 0 duplicate reads + Opus
sessions with density<20 swapped to Sonnet.

- **Cache-miss delta** (`cache_miss_reclaim`): shift tokens from
  billable input into `cache_read` until the session hits 85%. Saved =
  `shifted * (input_price - cache_read_price)`.
- **Duplicate-read delta** (`dup_read_reclaim`): `2K tokens * (n - 1)`
  per file read > 1x, at base input price.
- **Model-downgrade delta** (`model_downgrade_reclaim`): for Opus
  sessions with density<20, recompute cost under Sonnet pricing for the
  same token volume and take the diff.

Per-tool cost column is imputed from `n_calls * 2K_tokens *
input_price` (heuristic, not a billing-API call).

## Iron Law

**Quote the summary line in your reply, not a paraphrase.** The CLI
prints `[ok] sessions=N files_scanned=M total_cost=$X.XX
estimated_savings=$Y.YY stale_cost=$Z.ZZ` on success; copy that line
verbatim into the conversation so the user can audit the numbers
without opening the HTML. Do not claim "done" or "passed" without that
line.

**Stdout vs stderr contract.** The `[ok]` summary line goes to **stdout**.
Cost Gate WARN lines (`WARN: session ... input=N > N gate ...`),
unknown-model WARN lines, and worktree-classification WARN lines
(`WARN: worktree '<name>' classification failed ...`) go to **stderr**.
A consumer that parses stdout
must never see a WARN line in it. Exit code 3 means `cost_gate=bad` under
`--json` only; HTML mode always exits 0 unless the log dir is empty (2).

## Hand-off

Previous: `/dev-kit:log setup` + `/dev-kit:log on` (captures the
transcripts this skill consumes).

If `logs/claude-code/` is empty, refuse to run and tell the user to
enable capture first. Re-run at any time -- the analyzer re-reads
disk, no caching, so a fresh log shows up on the next invocation.

For CI / automation, prefer `--json` over the HTML output: it is stable,
machine-readable, and returns exit code 3 when a Cost Gate fires so a
PR pipeline can block on it.

## Related

- `tools/token_efficiency_analyzer.py` -- the CLI driver (stdlib only,
  py_compile-verified)
- `fixtures/make_fixture.py` -- generates 6 synthetic JSONL files
  (one per warning trigger) for regression
- `tests/test_token_efficiency_analyzer.py` -- 13 unit tests covering
  scoring curve, letter grade, per-warning $ attribution, Cost Gate,
  unknown-model warn, pricing override, and end-to-end HTML + JSON
  outputs
- `/dev-kit:log` -- captures the input this skill consumes

Next: open the output HTML in a browser, or share the relative file path
(e.g. `./token-dashboard-<repo>-30d.html`, never absolute) with the user.
