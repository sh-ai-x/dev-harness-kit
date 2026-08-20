> [← Skills index](README.md) · [Project README](../../README.md)

# `ci-setup`

**Category:** `bootstrap` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:ci-setup` (human-invoked)

`ci-setup` installs dev-kit's reusable CI workflow templates — 15 expected paths covering GitHub Actions workflows, pre-push hooks, scripts, and rule files — into a target project. It is idempotent, gated purely by the presence of `.dev-kit/ci-config.json` (no version comparison), and its marker file is the hard precondition `/dev-kit:build` checks for before it will start.

## When to use it

- The user types `/dev-kit:ci-setup` after `/dev-kit:bootstrap`.
- The user wants the same CI shape (branch-policy + validate + test + auto-fix) in a new repo.
- The user is preparing a repo for `/dev-kit:build` (ci-setup is a precondition).
- The user wants to re-run to refresh templates (`--force` flag).
- The user wants to backfill `installed_dev_kit_version` + `template_shas` into an existing v1.0.0 marker (no-op idempotent re-install).

## How it works

A 3-phase orchestration via `lib/ci_setup.py`:

**Phase 1 — Detect** (deterministic, no LLM call): parse arguments (`--target DIR` defaults to `$PWD`; `--force` overwrites; `--setup-secrets` prompts for and configures repo secrets; `--skip-verify` skips Phase 3); check `python3 ≥ 3.10`; delegate the presence short-circuit to `install_ci_config()`, which returns a no-op report when the marker and every expected path already exist and `force=False`; probe `.git/` (warn if absent) and `.github/` (create if absent); hard-verify `.dev-kit/ci-config.json` after write via round-trip JSON parse + non-empty dict check, reporting corruption as an error rather than swallowing it silently.

**Phase 1.5 — Pre-flight probe** (silent when `gh` is absent): read-only checks against `gh auth status`, `gh repo view`, and `gh secret list --json name` for `DEV_KIT_GITHUB_TOKEN`, `MINIMAX_API_KEY`, and `ANTHROPIC_API_KEY`, each returning OK/WARN/INFO/SKIP. A failed probe never blocks install.

**Phase 2 — Install**: for each of the 15 `EXPECTED_PATHS`, skip if it exists and `force=False`, overwrite if `force=True`, copy via `shutil.copy2` (preserves mtime for git diff stability); `chmod 0o755` on shell scripts, the pre-push hook, and `validate.py`; write the `.dev-kit/ci-config.json` marker atomically.

**Phase 1.7 — Lint pass** (non-fatal, always runs, even on a no-op idempotent re-install): `lint_installed_workflows()` flags known-stale patterns in previously-installed workflows (e.g. a pre-0.1.3 gate in `review.yml` that hard-failed on missing verdicts in `pull_request` mode) as warnings, never errors; the user acts on findings by re-running with `--force`.

**Phase 3 — Verify** (unless `--skip-verify`): `bash -n` on every installed `.sh` and the pre-push hook; `ast.parse` on every installed `.py`; `python3 scripts/validate.py` (expects `"OK: CI installation valid"`); `bash scripts/ci-local.sh` (expects exit 0); an `act -l` check that WARNs (not fails) if `act` is missing.

**Phase 4 — Post-install checklist**: printed on success when opted in, with `OWNER/REPO` auto-filled from `git remote get-url origin`. With `--setup-secrets` (Phase 4b), the skill reads the provider from `CI_REVIEW_PROVIDER` (env → `.env` → `.env.example` → default `minimax`), enumerates required secrets via `required_secrets_for_provider()`, prompts for each with `AskUserQuestion`, and calls `set_repo_secrets()` to run `gh secret set` — install still succeeds even if secret-setting fails, surfaced as a warning.

## Usage

```bash
/dev-kit:ci-setup [--force] [--setup-secrets] [--target DIR] [--skip-verify] [--provider NAME]
```

| Flag | Effect |
|---|---|
| *(0-arg)* | Idempotent install/no-op against `$PWD`. |
| `--force` | Overwrites existing files inside `EXPECTED_PATHS` only. |
| `--setup-secrets` | Interactively configures required repo secrets via `gh secret set`. |
| `--target DIR` | Installs into a directory other than `$PWD` (hidden flag). |
| `--skip-verify` | Skips Phase 3 verification (hidden flag). |
| `--provider NAME` | Overrides the CI review provider (hidden flag). |

Failure exit codes: `1` = arg error, `2` = marker present + no `--force`, `3` = copy failure, `4` = verify failure.

## Output

`.dev-kit/ci-config.json` — the marker/contract `/dev-kit:build` requires before it will start. Plus the 15 installed paths:

| Path | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Branch-policy warn + test + validate jobs |
| `.github/workflows/auto-fix-pr.yml` | Auto-fix loop on `changes_requested` review (5-iter cap) |
| `.github/workflows/review.yml` | `/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) PR fan-out + severity gate |
| `.githooks/pre-push` | Client-side hook that blocks a direct `git` `push` targeting main |
| `scripts/validate.py` | Checks install + marker + bash syntax |
| `scripts/test.sh` | Pytest wrapper (skips gracefully if no `tests/`) |
| `scripts/branch-policy.sh` | Mirror of the pre-push hook for CI script context |
| `scripts/ci-local.sh` | Local-runner entrypoint |
| `hooks/worktree-guard.sh` | PreToolUse Write/Edit block on main checkout |
| `hooks/session-start-check.sh` | SessionStart reminder when started in main checkout |
| `hooks/lib/worktree-detect.sh` | Shared `--git-dir`/`--git-common-dir` discriminator |
| `hooks/hooks.json` | Wires the 4 hook files into the right event matchers |
| `.claude/rules/git-workflow.md` | Branch / worktree / PR conventions |
| `tests/test_worktree_guard.py` | Regression tests for the 4 rule hooks + hooks.json wiring |

## Related

- [bootstrap](bootstrap.md) — typically run before this skill.
- [bootstrap](bootstrap.md) — composes `bootstrap` + this skill into one call.
- [`ci-update`](ci-update.md) — for **selective** refresh of templates after dev-kit ships new versions (4-state diff with backup). Use `--force` here only for a full reset; for partial refresh prefer `/dev-kit:ci-update --apply`.
- [`ci-doctor`](ci-doctor.md) — read-only audit; surfaces `templates current` WARN rows when refresh is needed.
- `/dev-kit:build` — refuses to start without the `.dev-kit/ci-config.json` marker this skill writes.
- `docs/quality/ci-setup.md` — full usage docs.

---
*Source: [`skills/ci-setup/SKILL.md`](../../skills/ci-setup/SKILL.md)*
