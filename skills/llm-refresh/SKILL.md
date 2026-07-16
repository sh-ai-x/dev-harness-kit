---
name: llm-refresh
category: shortcuts
description: Refresh docs/llm-info/<provider>.json from each vendor's official pricing page. Diff-then-commit; manual like set-provider.sh.
when_to_use:
  - User types /dev-kit:llm-refresh
  - User asks to update / sync Claude/Codex/MiniMax/DeepSeek pricing or model lists
  - Quarterly cadence or before quoting prices in a plan or cost-gate config
  - After a vendor publishes a new model or price change
allowed-tools: Read Bash
disallowed-tools: Write Edit Agent WebFetch
model: sonnet
disable-model-invocation: false
user-invocable: true
---

# /dev-kit:llm-refresh — LLM pricing & model registry refresh

## What it does

Fetches each provider's official public pricing page and writes a normalized payload to
`docs/llm-info/<provider>.json`. The four tracked providers are defined in
`docs/llm-info/sources.json` — Claude/Anthropic, OpenAI Codex/API, MiniMax,
DeepSeek. The skill is the only mechanism that mutates those JSON files; hand
edits are explicitly discouraged (see `docs/llm-info/README.md`).

Updates are **explicit, diff-visible, and user-committed** — same trust model as
`bin/set-provider.sh`. The script never silently rewrites the registry.

## Body

1. From the project root, run:

   ```bash
   python3 skills/llm-refresh/scripts/refresh.py
   ```

   To refresh a single provider, add `--provider claude` (or `codex`, `minimax`,
   `deepseek`). To preview without writing, add `--check`.

2. The script prints one of three lines per provider:

   - `[<id>] no change` — vendor page matches the registry; nothing to do.
   - `[<id>] wrote <path> (<N> models)` — file updated; review the diff.
   - `[<id>] FAIL: <reason>` — fetch or parser failed; investigate before
     trusting the unchanged file.

3. Exit codes are sentinel-friendly (0=ok, 1=--check saw diff, 2=fetch/parse
   fail, 3=usage). Pair `--check` with CI/test harnesses.

4. After a successful run, the user reviews the `git diff` and commits:

   ```bash
   git diff docs/llm-info/<id>.json   # sanity check
   git add docs/llm-info/<id>.json
   git commit -m "chore(llm-info): refresh <provider> pricing snapshot"
   ```

## Trust model

- **Network is delegated, not inline.** The SKILL.md body has `WebFetch` in
  `disallowed-tools` per repo policy. The script uses `urllib.request.urlopen`
  (the same pattern as `lib/llm_judge.py`) instead.
- **No automation.** No cron, no GitHub Action refreshes the file. If a vendor
  changes prices and the user does not run the skill, the registry drifts from
  reality — that is intentional. The README does not contain vendor numbers, so
  the worst case is a stale-but-isolated JSON file.
- **No silent overwrites.** The script diffs the parsed payload against the
  existing file and reports "no change" when equal. Atomic writes only happen
  on a real diff, via `lib/atomic.py:atomic_write_json`.

## Rules

- **READ-ONLY on the SKILL side.** This skill has `Edit` and `Write`
  explicitly disallowed. All file mutations happen in `refresh.py` invoked via
  `Bash`. Do not add inline file-write logic to the SKILL body.
- **No model delegation.** `Agent` is disallowed so the refresh is
  deterministic — the user sees the diff, not a sub-agent's interpretation.
- **One hand-off.** No automated next skill. The user commits manually so the
  refresh always lands behind a reviewable PR.

## Files installed

| Path | Purpose |
|---|---|
| `skills/llm-refresh/SKILL.md` | This file |
| `skills/llm-refresh/scripts/refresh.py` | Fetch + parse + diff + atomic-write CLI. Single entry. |
| `skills/llm-refresh/agents/openai.yaml` | Codex-side `interface` (no separate Codex skill body — Codex reuses this same SKILL.md via `.codex-plugin/plugin.json:skills = "./skills/"`) |
| `docs/llm-info/sources.json` | Provider registry (id → `{url, parser, currency}`) |
| `docs/llm-info/{claude,codex,minimax,deepseek}.json` | SSOT, one per provider |
| `docs/llm-info/README.md` | Pointer + "do not edit by hand" rule |
| `tests/test_llm_refresh.py` | Schema + script-behaviour contracts |

## Next step

After committing a refreshed `docs/llm-info/<id>.json`, the next planned
consumer is `lib/cost_gate.py` (currently inline tier data). Wiring that is a
follow-up PR — this skill does not touch `lib/cost_gate.py` so the diff stays
reviewable.
