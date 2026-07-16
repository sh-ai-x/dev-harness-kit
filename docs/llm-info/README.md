# docs/llm-info/ — LLM pricing & model registry (SSOT)

This directory is the **single source of truth** for LLM pricing, plans, and model availability across Claude/Anthropic, OpenAI Codex, MiniMax, and DeepSeek. Every value in every `*.json` is **verified against the vendor's own public pricing page** (see "Verification" below); the refresh skill re-verifies on demand.

## Files

| File | Purpose |
|---|---|
| `sources.json` | Provider registry: id → `{url, parser, currency}`. Read by `refresh.py`. |
| `claude.json` | Anthropic Claude pricing + active models + plans |
| `codex.json` | OpenAI Codex / API pricing + active models + plans (consumer ChatGPT plans out of scope) |
| `minimax.json` | MiniMax pricing + active models (CNY; was thought to be USD before 2026-07-17 audit) |
| `deepseek.json` | DeepSeek pricing + active models + cache-hit rate |

## Verification (initial bootstrap, 2026-07-17)

Every JSON value was hand-extracted from the vendor's official docs page during the initial bootstrap. The refresh script's parser logic is then **frozen against a locally-saved fixture** of the same page (see `skills/llm-refresh/tests/fixtures/`) so future regressions surface as a failing test, not as a silent data drift.

| Provider | Verified page (initial) | Currency | Fixture saved |
|---|---|---|---|
| claude   | https://platform.claude.com/docs/en/about-claude/pricing | USD  | ✓ `anthropic_pricing.html` |
| codex    | https://developers.openai.com/api/docs/pricing            | USD  | ✓ `openai_pricing.html` |
| minimax  | https://platform.minimaxi.com/docs/guides/pricing-paygo.md| CNY  | ✓ `minimax_pricing.md` |
| deepseek | https://api-docs.deepseek.com/quick_start/pricing        | USD  | ✓ `deepseek_pricing.html` |

## Rules

- **Do not edit `*.json` by hand.** They are machine-emitted and version-controlled so a future `git diff` reveals drift from the vendor's published pricing page.
- **Refresh via the skill**: `/dev-kit:llm-refresh [--provider <id>] [--check]`.
- **Currency** is per-provider (USD for three, CNY for MiniMax). The `currency` field guards against accidental unit mix-ups.
- **Deprecation** is tracked via the `models[].deprecated` flag.

## Why JSON and not the README

Pricing drifts on every vendor release cycle. Mirroring those facts into README or any Markdown document means re-editing the README every price change — and reviewing README PRs becomes a guessing game. The README, cost-gate configs, and any other consumer reference `.json` under this directory, never the numbers themselves.

## Next step

When a vendor announces a pricing or model-list change, run:

```bash
python3 skills/llm-refresh/scripts/refresh.py
```

then review the printed diff, `git add docs/llm-info/*.json`, commit, push.

For a sanity check before committing, append `--check` to see the diff without writing.
