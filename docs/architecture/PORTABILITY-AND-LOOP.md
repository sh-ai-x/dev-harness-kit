# Portability contract (residual)

The portability contract is enforced by `tests/test_hooks_json_parity.py`
(regression) — it normalizes the `DEV_KIT_AGENT` env prefix and diffs CC/Codex
hook signatures. No separate `tools/` CLI is shipped.

The older `PORTABILITY-AND-LOOP.md` (this file) previously documented a
`tools/loop_engine.py iterate/verify` loop protocol that has since been
removed (2026-10 prune). That work was an adapter experiment and never
shipped to consumers; see the git history for the removed contract.
