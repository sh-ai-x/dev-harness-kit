# Portability improvements — 2026-08-23

Source audit: portability review of `dev-harness-kit` for cross-repo adoption
(Claude Code + Codex dual-runtime, 49 skills, 13 hook shells, 15 CI templates).

Score: **8.5 / 10**. Adoption ladder is turnkey (marketplace → bootstrap → ci-setup).
Remaining gaps are mostly drift-prevention and consumer-onboarding documentation.

## Issues filed (see GitHub issues for full bodies)

| # | Improvement | File(s) touched | Effort |
|---|---|---|---|
| 1 | Lock `hooks/hooks.json` (CC) ↔ `.codex-plugin/hooks/hooks.json` (Codex) parity with a regression test | `tests/test_hooks_json_parity.py` (new) | S |
| 2 | Extract `.env` key reader into shared lib so `bin/set-provider.sh` and `lib/ci_setup.py` cannot drift | `lib/read_env_key.py` (new), `bin/set-provider.sh`, `lib/ci_setup.py` | S |
| 3 | Add `ci-doctor` probe for `vars.CI_REVIEW_PROVIDER` ↔ `.env:CI_REVIEW_PROVIDER` mismatch | `lib/ci_setup.py`, `skills/ci-doctor/SKILL.md` | S |
| 4 | Move `token-dashboard-*.html` (1.4 MB) out of repo root or track via `git lfs` | repo root | S |
| 5 | Surface the "add a new provider" recipe in `bin/set-provider.sh --help` (currently a code edit) | `bin/set-provider.sh` | XS |

## Out-of-scope (not filed)

- `README.ko.md` bilingual drift — would require an i18n strategy decision (L5: do not propose options unprompted).
- `templates/init.sh` pytest auto-detection — keep `TEST_CMD` env override, document better.
- 49 skills shipped with the plugin — bootstrap `config` picker already covers enablement.
- `templates/ci/.pytest.ini.example` snippet — fold into the ci-setup Phase 1.7 lint instead of a new file.

## Cross-references

- Iron Law #2 (L2): No fix without reproducing the bug — each issue above
  describes the drift scenario it prevents before listing the implementation.
- Iron Law #3 (L3): Each PR body must quote exit code + test count + build log.
- `rules/git-workflow.md`: every issue → new branch → worktree → PR.
