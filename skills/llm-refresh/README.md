# /dev-kit:llm-refresh — Skill README

> Refresh the vendor-tracked pricing and model registry under
> `docs/llm-info/<provider>.json` from each provider's official pricing
> page. Slash command: `/dev-kit:llm-refresh`.

## What this skill does

Fetches the official public pricing page for each tracked LLM provider
(Anthropic, OpenAI, MiniMax, DeepSeek), parses the live page, and writes
the result into `docs/llm-info/<provider>.json`. The same JSON is the
single source of truth consumed by:

- `lib/cost_gate.py` (drives `/dev-kit:cost-gate`)
- `tools/token_efficiency_analyzer.py` (drives `/dev-kit:token-analyzer`)
- any future consumer that bills Claude/OpenAI/MiniMax/DeepSeek tokens

`docs/llm-info/<provider>.json` values are always **USD per million
tokens**. The MiniMax file used to publish in CNY; values were
pre-converted at FX 7.00 during the initial bootstrap and the original
CNY rate is recorded in each row's `notes` field.

## File layout

```
skills/llm-refresh/
├── SKILL.md                 # slash command frontmatter + body
├── README.md                # this file
├── agents/
│   └── openai.yaml          # Codex dual-publish interface
└── scripts/
    └── refresh.py           # the single executable entry point
```

The four `docs/llm-info/<provider>.json` files it produces are tracked
separately:

```
docs/llm-info/
├── README.md
├── sources.json             # provider registry {url, parser, currency}
├── claude.json
├── codex.json
├── minimax.json
└── deepseek.json
```

## Invocation

### Slash command (human)

```
/dev-kit:llm-refresh             # refresh every provider
/dev-kit:llm-refresh --provider claude    # one provider
/dev-kit:llm-refresh --check             # diff only; do not write
/dev-kit:llm-refresh --json              # machine-readable summary
```

### Direct CLI (debug + scripts)

```bash
# from the repo root
python3 skills/llm-refresh/scripts/refresh.py
python3 skills/llm-refresh/scripts/refresh.py --provider codex --check
python3 skills/llm-refresh/scripts/refresh.py --json --sources /custom/path/sources.json
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | all providers up to date (with `--check`) OR all writes succeeded |
| 1 | `--check` saw at least one diff (no writes happened) |
| 2 | fetch or parse failure for at least one provider |
| 3 | usage error (unknown provider id, missing `sources.json`) |

## Trust model

- **User-initiated, never auto.** No cron, no CI workflow re-runs
  the refresh. The user runs the skill manually after seeing a vendor
  announcement. Same explicit-intent pattern as `bin/set-provider.sh`.
- **No silent overwrites.** The script diffs the parsed payload
  against the existing file and reports "no change" when equal. Atomic
  writes only happen on a real diff, via `lib/atomic.atomic_write_json`.
- **Parser failure is loud, not silent.** Each per-provider parser
  raises `ValueError` (or `RuntimeError` on fetch) when the live page's
  structure drifts. The error message names the failing parser; rerun
  with `--check` to confirm.
- **WebFetch is intentionally not used.** The repo's hook policy
  disallows `WebFetch`; refresh.py uses `urllib.request.urlopen` with
  a Mozilla-class User-Agent header instead (the same pattern as
  `lib/llm_judge.py`).

## How to add a new provider

1. Append one row to `docs/llm-info/sources.json`:
   ```json
   {
     "id": "<provider_id>",
     "label": "<Human name>",
     "url": "https://vendor.example.com/pricing",
     "parser": "<parser_kind>",
     "currency": "USD"
   }
   ```
2. Add a parser function to `skills/llm-refresh/scripts/refresh.py`
   named `parse_<parser_kind>` with the signature
   `parse_<parser_kind>(content: str, meta: dict) -> dict`.
   The returned dict must match the schema documented in
   `docs/llm-info/README.md` (top-level keys `provider`, `label`,
   `source_url`, `fetched_at`, `currency`, `models`, `plans`).
3. Add the parser to the `PARSERS` dict in the same file.
4. Run the skill with `--provider <provider_id>` against the live page,
   review the printed diff, and commit `docs/llm-info/<provider_id>.json`.

## How to handle a vendor price change

1. `python3 skills/llm-refresh/scripts/refresh.py --provider <id> --check`
   — preview the diff without writing.
2. Compare the diff to the vendor's published change. Confirm only
   the expected prices moved.
3. If a parser fix is needed (page restructured), edit the parser,
   re-run `--check`, and ensure the output matches the vendor's
   current page.
4. Drop `--check` to write the file.
5. `git diff docs/llm-info/<id>.json` — sanity-check the JSON.
6. `git add docs/llm-info/<id>.json` + commit. The PR description
   must include "pricing re-verified against <URL> on <YYYY-MM-DD>"
   per `rules/token-pricing.md`.

## Re-verify cadence

`rules/token-pricing.md` requires a fresh re-verification before
**every release** (the `/dev-kit:bump` flow). For day-to-day work,
re-run on:

- Vendor announcement of a price change
- Vendor announcement of a new model row
- A drift warning surfaced by `/dev-kit:token-analyzer` ("unknown model")

## Related files

- `docs/llm-info/` — the JSON SSOT this skill maintains.
- `lib/llm_pricing.py` — the consumer that reads `docs/llm-info/*.json`.
- `lib/cost_gate.py` — pricing consumer for `/dev-kit:cost-gate`.
- `tools/token_efficiency_analyzer.py` — pricing consumer for `/dev-kit:token-analyzer`.
- `rules/token-pricing.md` — Iron Laws that govern every rate change
  (citation rule, no-pricing-from-memory, etc.).
- `tests/test_llm_pricing.py` — loader contract tests (cross-consumer
  parity between `cost_gate` and the analyzer).
- `tests/test_llm_refresh.py` — schema + script CLI contract tests.

## Why this skill exists

Without `docs/llm-info/`, both `lib/cost_gate.py` and
`tools/token_efficiency_analyzer.py` would carry their own inline
`PRICING` dict that drifts independently from each other and from
the vendor. This skill is the single edit point. The skill is intentionally
slow, explicit, and human-gated so a silent misbilling is impossible.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
