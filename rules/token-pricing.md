---
paths:
  - "tools/token_efficiency_analyzer.py"
  - "tools/**/pricing*.py"
  - "tools/**/*pricing*.json"
---

# Token pricing rules (dev-harness-kit)

These rules govern how token-pricing data is sourced, cited, and updated.
They apply to every constant that bills tokens — the PRICING rows,
`docs/llm-info/*.json`, the `--pricing-override` JSON shape, and every
loader that reads them.

## Iron Laws

1. **Pricing comes from official provider docs, never from memory.**
   Anthropic / OpenAI / MiniMax / DeepSeek publish rate sheets and revise
   them without notice. A constant copied from training data is an unpaid
   guess — the day after the provider ships a new tier it is wrong. The
   PRICING rows, the `docs/llm-info/*.json` files, the `--pricing-override`
   JSON file, and any skill-body pricing tables MUST each carry an inline
   citation to the page where the number was read (URL + ISO-8601 fetch
   date).
2. **The SSOT is `docs/llm-info/<provider>.json`. Refresh it via
   `/dev-kit:llm-refresh` — never edit inline rows in any consumer.**
   Before 2026-07-17 two inline `PRICING` dicts (`tools/token_efficiency_analyzer.py`
   and `lib/cost_gate.py`) drifted independently. Both now import
   `llm_pricing:pricing_for()` which loads from `docs/llm-info/`. New code
   MUST go through `lib/llm_pricing`; never re-introduce a third inline
   copy.
3. **Re-verify before every release.** A `bump` version PR that ships a
   pricing-rate change without a fresh official-source quote is a silent
   misbilling. The PR description for any change to docs/llm-info/*.json
   (or to a single legacy row in `lib/llm_pricing:LEGACY_FALLBACK`) MUST
   include "pricing re-verified against <URL> on <YYYY-MM-DD>" with the
   link to the live page, not a Wayback snapshot.
4. **Unknown model ids are a signal, not noise.** When a captured session
   shows a model id that does not match any row in the loader's merged
   PRICING, the loader emits `WARN: unknown model '<id>' ...` to stderr.
   Triage these the same week they appear: read the provider's current
   rate sheet, add the row via `/dev-kit:llm-refresh --provider <id>`,
   ship the same PR. Leaving an unknown model on the floor means we
   silently bill it at sonnet default.

## Mandatory workflow (any pricing change)

1. Open the provider's official page (URLs in the citation table below).
2. Confirm the rate you want to edit. For multi-tier families
   (e.g. gpt-5.6-sol/terra/luna) confirm **every** tier — a partial
   update silently misroutes whichever tier you forgot.
3. Run `python3 skills/llm-refresh/scripts/refresh.py --provider <id>`
   (or simply `llm-refresh` without `--check` to write the file).
   The script overwrites `docs/llm-info/<id>.json` via
   `lib.atomic.atomic_write_json`.
4. Add a `TestParser*` case (in `tests/test_llm_refresh.py`) only when
   the parser's logic itself changes — these tests pin the parser to a
   saved fixture, not to a single rate. New model rows in the JSON do
   NOT require parser-test changes; they need only the loader contract
   tests in `tests/test_llm_pricing.py`.
5. Quote the official-source URL AND the run's exit code + test count
   in the PR body.

## Source-of-truth registry (`docs/llm-info/sources.json`)

When updating the table below, do not delete an old URL — supersede it
with the new one and a "superseded <YYYY-MM-DD>" note. A reviewer
should always be able to walk back the history of a rate.

| Provider | URL (registry; parser pulls this) | Models tracked | Re-verify cadence |
|---|---|---|---|
| Anthropic | `https://platform.claude.com/docs/en/about-claude/pricing` | Fable 5 / Mythos 5 / Opus 4.x / Sonnet 4.x / Haiku 4.5 | every release |
| OpenAI    | `https://developers.openai.com/api/docs/pricing`            | gpt-5.6-* / gpt-5.5 / gpt-5.4 / gpt-5.3-codex / o-* / gpt-image-* | every release |
| MiniMax   | `https://platform.minimaxi.com/docs/guides/pricing-paygo.md`| MiniMax-M3 / MiniMax-M2.x (CNY) | every release |
| DeepSeek  | `https://api-docs.deepseek.com/quick_start/pricing`         | v4-Flash / v4-Pro | every release |
| Anthropic cache-TTL contract | same as Anthropic pricing page; the multipliers 5m = 1.25× input, 1h = 2.0× input, cache_read = 0.10× are documented there | n/a (universal) | every release |

## Loader / consumer architecture

```
   docs/llm-info/<provider>.json     (SSOT, refreshed via /dev-kit:llm-refresh)
                |
                v
        lib/llm_pricing.py          (single loader; per-provider cache constants;
                |                    CNY -> USD conversion; longest-prefix-first
                |                    substring match; legacy fallback under JSON)
        +-------+-------+
        |               |
        v               v
   cost_gate.py   token_efficiency_analyzer.py
   (cost-gate     (token-analyzer
    skill)         skill)
```

`tools/token_efficiency_analyzer.py:PRICING` and `lib/cost_gate.py:PRICING`
no longer exist as inline dicts. The module-level PRICING is now built
from `llm_pricing.LEGACY_FALLBACK` overlaid with JSON-loaded rows at
import time; `reload_pricing_from_ssot()` re-reads the JSON if a refresh
just landed in the middle of a long dashboard run.

## OpenAI notes (carried forward — DO NOT delete without confirming the
new URL still exists and lists the same families):

- The consolidated pricing page lives at `https://developers.openai.com/api/docs/pricing`
  (the legacy `https://openai.com/api/pricing/` redirects to a marketing
  page that omits long-context / batch / flex columns).
- Per-model cards at `https://developers.openai.com/api/docs/models/<model>`
  list context-window, modalities, and the long-context rate.
- OpenAI has a single cached-input discount (~50 % of base input) and
  no separate TTL for cache writes — both `cache_write_5m` and
  `cache_write_1h` columns in PRICING mirror base input pricing.

Anthropic notes:

- 5-minute cache-write is 1.25 × base input; 1-hour cache-write is 2.0 ×.
- Cache-read is 0.10 × base input (cheap; the lever for prompt-cache ROI).

MiniMax notes:

- MiniMax publishes only one cache-write rate (single TTL). Mirror it
  into both `cache_write_5m` and `cache_write_1h` columns in PRICING.
- All rates are in CNY; `lib/llm_pricing.CNY_PER_USD = 7.00` converts
  to USD at load time. The 7.00 anchor is the constant rate baked into
  the original token-pricing constants (the legacy 0.30 USD per Mtok
  is exactly 2.10 CNY / 7.00). A live FX poll is out of scope (would
  silently drift per Iron Law #2).

DeepSeek notes:

- v4-Flash and v4-Pro each carry a `notes` string with an explicit
  `Cache hit: $X.YZ/MTok` rate; the loader reads that string and emits
  `cache_read = <rate>` directly (no multiplier-based fallback).
  - OpenAI notes (carried forward)

## Lessons we already paid for

- **`gpt-5` is a substring of `gpt-5.6-*`.** Putting the shorter key
  first in the matcher silently stole every 5.6-* id at 4× cheaper
  legacy pricing. Longest-prefix-first is mandatory when sharing a
  hierarchical namespace. The shared loader enforces this.
- **Two PRICING dicts drift.** `tools/token_efficiency_analyzer.py`
  used to have its own PRICING and so did `lib/cost_gate.py`. Both
  silently misbilled sessions until 2026-07-17 — that's why they now
  both go through `lib/llm_pricing.py`.
- **Capture-side filtering can strip token metadata before storage.**
  If the analyzer reports `model = ""` and zero tokens for sessions
  from a provider, the fix is upstream in the capture script (e.g.
  `tools/save_log.py:_codex_has_event_text` keeps only conversation
  text and drops `payload.info.model` / `payload.info.token_usage`).
  The analyzer cannot recover data that was never written.
- **Fixtures pin parsers, not rates.** A pricing rate can change
  monthly; a parser matching the page structure is a contract that
  changes much less often. Tests under `skills/llm-refresh/tests/fixtures/`
  pin the parser once and let the rate drift until someone re-runs
  `/dev-kit:llm-refresh` to refresh the JSON.

## Forbidden patterns

- ❌ Hardcoding a rate from training-data memory in the PR description
  without an inline URL citation in the changed SSOT file.
- ❌ Adding a third inline `PRICING = {...}` dict anywhere outside
  `lib/llm_pricing.py`. (Two already migrated; the lesson is "use the
  loader, not a copy".)
- ❌ Editing `docs/llm-info/<id>.json` by hand without running
  `/dev-kit:llm-refresh` first — the script must remain the only writer.
- ❌ Shipping a `pricing-override` JSON in the repo without a date
  stamp and a `superseded-by <URL+date>` pointer.
- ❌ Skipping the FX-constant note when changing `CNY_PER_USD`.

## Related

- `lib/llm_pricing.py` — single loader; `LEGACY_FALLBACK` (only the
  fallback tier) lives here, never in callers.
- `docs/llm-info/` — four-provider SSOT (claude / codex / minimax / deepseek).
- `skills/llm-refresh/` — `/dev-kit:llm-refresh` and the per-provider parser fixtures.
- `tools/token_efficiency_analyzer.py` — consumer (now imports `llm_pricing`).
- `lib/cost_gate.py` — consumer (now imports `llm_pricing`).
- `tests/test_llm_pricing.py` — loader contract + cross-consumer parity.
- `tests/test_cost_gate.py:TestIsolation` — assert `lib/cost_gate` and
  `tools/cost_gate_status` never import `tools/token_efficiency_analyzer`.
- `/dev-kit:token-analyzer` — consumes these rates to bill sessions.
- `/dev-kit:llm-refresh` — refreshes the SSOT.
- `rules/session-hygiene.md` — Iron Law #5 (match the model to the
  task) is the spend-side counterpart to this cost-side rule.
