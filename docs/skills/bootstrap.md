> [← Skills index](README.md) · [Project README](../../README.md)

# `bootstrap`

**Category:** `bootstrap` · **Alpha:** `state` · **Invocation:** `/dev-kit:bootstrap` (human-invoked)

`bootstrap` is the canonical one-shot setup for a fresh dev-harness-kit project. It runs the unconditional bootstrap pipeline (sanity, codebase-map, hook-matrix, write-claude-md), then prompts the operator for whether to also install CI templates. Pass `--yes` to auto-accept CI or `--skip-ci` to decline. With Y (default), end state on disk matches the legacy `/dev-kit:bootstrap-full` slash — three SSOT files plus the 15 CI workflow templates plus pre-push hook plus `.dev-kit/ci-config.json` marker.

## When to use it

- The user runs `/dev-kit:bootstrap` for the first time on a new project.
- The user wants to refresh `CLAUDE.md` / `active-hooks.json`.

## How it works

Bootstrap runs the unconditional pipeline then optionally ci-setup and git-defaults, in a 9-step orchestration (4 auto steps, 1 prompt, 1 ci-setup, 2 git-defaults, 1 exit):

1. **Sanity** (deterministic, no LLM) — a 7-check audit: manifest presence (`package.json`/`pyproject.toml`), `.git/` health, `docs/` template placeholders, a banned-phrase scan (slop-detector SSOT regex), a secret-scan (credential pattern — this is the one **CRITICAL FAIL** check, all others are WARN), a hook-bypass detection (`DEV_KIT_HOOK_OFF=*` env), and a methodology lockfile consistency check (`lib/methodology.json`). Result is PASS (all pass), WARN (1-3 warnings, pass-through allowed), or FAIL (4+ warnings or 1+ critical — blocks Plan entry). Output goes to stdout only; a file (`.dev-kit/sanity-report.md`) is written only with `--persist-audit`.
2. **Codebase map** (deterministic, no LLM) — CLAUDE.md is a slim pointer; the codebase map is lazy-loaded via `docs/CODEBASE-MAP.md` (only written with `--full-claude-md`). The full map (Tree via `os.walk` depth 4, Manifest, Deps top-10, Conventions) is rendered by `lib/write_project_md.py:render_codebase_map_doc`. CLAUDE.md's references block always points to this file regardless.
3. **Hook matrix init** — writes `.dev-kit/.active-hooks.json` as the single source of truth for which hooks (`tdd-guard`, `bash-guard`, `secret-scan`, `slop-detector`, `stop-verify`) are active per stage (bootstrap/plan/design/build/review/security/ship). `hooks/hooks.json` only registers the matrix reader; all activation decisions live in the JSON.
4. **write-claude-md** — `lib/write_project_md.py` writes `CLAUDE.md` and `AGENTS.md` (a 1-line pointer to CLAUDE.md for CLIs that read AGENTS.md) atomically, sections §1-§5.
5. **ci-setup prompt** — after the unconditional bootstrap set lands, the skill prompts `Also install CI templates (ci-setup)? [y/N]`. Default is N. Pass `--yes` or `--with-ci` to skip the prompt (assume Y) or `--skip-ci` to skip and print the unavailable-features list.
6. **ci-setup** (only on Y) — delegates to `lib/ci_setup.py:install_ci_config(force=True)` (Phase 1.5 pre-flight probe plus 15 EXPECTED_PATHS plus `.dev-kit/ci-config.json` marker plus Phase 1.7 lint plus Phase 4 post-install checklist). With `--skip-verify`, Phase 3 verify is skipped. Without `--force`, re-runs are no-op.
7. **git-defaults prompt** — after ci-setup, the skill prompts `Also configure operator-global git defaults (rebase.autoStash + pull.rebase)? [Y/n]`. Default is Y. Pass `--yes` to skip both prompts (assume Y) or `--skip-git-defaults` to skip both the prompt and the execution (equivalent to answering `n`).
8. **git-defaults** (only on Y) — delegates to `bin/setup-git-defaults.sh`, an idempotent single-source-of-truth allowlist (`SETTINGS=()`) that writes the operator-global git keys via `git config --global`. Idempotent on re-run; supports `--check` (preview missing keys) and `--dry-run` (preview mutations). The operator's real `~/.gitconfig` is the documented scope; tests override `HOME`/`XDG_CONFIG_HOME` so they never touch the real file.

9. **Exit** — pointer to `/dev-kit:build <first-feature>` to start the canonical plan -> build loop, or `/dev-kit:ci-doctor` for post-install drift verification.

Hidden flags (no visible option prompts — MUST-NOT-13): `--skip-sanity`, `--skip-map`, `--slim|--full`, `--team`, `--strict`, `--persist-audit`, `--skip-ci` (skip ci-setup, equivalent to answering `n`), `--skip-git-defaults` (skip sub-stage 7 + 8 git-defaults, equivalent to answering `n` on both the prompt and the execution), `--yes` (skip the ci-setup + git-defaults prompts, default `Y`), `--force` (overwrite existing CI templates), `--skip-verify` (skip ci-setup Phase 3 verify). With `--strict`, all hooks default to `exit 2` instead of `exit 0`.

## Usage

```bash
/dev-kit:bootstrap [--skip-sanity] [--skip-map] [--slim|--full] [--team] [--strict] [--persist-audit]
```

| Flag | Effect |
|---|---|
| *(0-arg)* | Runs the full pipeline and prompts for ci-setup then git-defaults; default is Y on both (full setup, matches legacy `/dev-kit:bootstrap-full`). |
| `--skip-sanity` | Skips the sanity sub-stage. |
| `--skip-map` | Skips the codebase-map sub-stage. |
| `--slim` / `--full` | Controls CLAUDE.md verbosity mode. |
| `--full-claude-md` | Writes the full 4-section codebase map to `docs/CODEBASE-MAP.md` instead of the lazy-loading index. |
| `--team` | Team-mode variant (hidden flag). |
| `--strict` | All hooks default to `exit 2` instead of `exit 0`. |
| `--persist-audit` | Also writes `.dev-kit/sanity-report.md`. |
| `--skip-ci` | Skips ci-setup. Prints the unavailable-features list. |
| `--skip-git-defaults` | Skips the git-defaults prompt + execution; no `.gitconfig` is written. |
| `--yes` | Skips both the ci-setup and git-defaults prompts; assumes Y. |
| `--force` | Overwrites existing CI templates during ci-setup. |
| `--skip-verify` | Skips ci-setup Phase 3 verify. |

## Output

Three files on a fresh repo from the unconditional bootstrap set: `CLAUDE.md`, `AGENTS.md`, `.dev-kit/.active-hooks.json`. With `--persist-audit`, also `.dev-kit/sanity-report.md`. With `--full-claude-md`, also `docs/CODEBASE-MAP.md`. With Y or `--yes` on the ci-setup prompt, additionally: 15 CI workflow templates in `.github/workflows/` + `.dev-kit/ci-config.json` marker + pre-push hook.

## Related

- [ci-setup](ci-setup.md) — the standalone half (used by `--skip-ci` flow; also reachable directly as `/dev-kit:ci-setup --force`).
- `/dev-kit:plan` — opt-in idea → PRD.md synthesis; not the default next stage.

---
*Source: [`skills/bootstrap/SKILL.md`](../../skills/bootstrap/SKILL.md)*
