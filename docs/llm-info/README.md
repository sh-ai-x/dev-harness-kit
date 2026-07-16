# docs/llm-info/ — LLM pricing & model registry (SSOT)

This directory is the **single source of truth** for LLM pricing, plans, and model availability across Claude/Anthropic, OpenAI Codex, MiniMax, and DeepSeek.

## Files

| File | Purpose |
|---|---|
| `sources.json` | Provider registry: id → `{url, parser, currency}`. Read by the refresh script. |
| `claude.json` | Anthropic Claude pricing + active models + plans |
| `codex.json` | OpenAI Codex / API pricing + active models + ChatGPT plans |
| `minimax.json` | MiniMax pricing + active models |
| `deepseek.json` | DeepSeek pricing + active models |

## Rules

- **Do not edit `*.json` by hand.** They are machine-emitted and version-controlled so a future `git diff` reveals drift from the vendor's published pricing page.
- **Refresh via the skill**: `/dev-kit:llm-refresh [--provider <id>] [--check]`.
- **Currency** is per-provider (`USD` for all four current entries); `currency` is part of each model row's price semantics, not a unit conversion.
- **Deprecation** is tracked via the `models[].deprecated` flag; deprecated models stay in the file with `deprecated: true` for one cycle, then removed.

## Why JSON and not the README

Pricing drifts on every vendor release cycle. Mirroring those facts into README or any Markdown document means re-editing the README every price change — and reviewing README PRs becomes a guessing game. The README, cost-gate configs, and any other consumer reference **`.json` under this directory**, never the numbers themselves.

## Next step

When a vendor announces a pricing or model-list change, run:

```bash
python3 skills/llm-refresh/scripts/refresh.py --provider <id>
```

then review the printed diff, `git add docs/llm-info/<id>.json`, commit, push.
