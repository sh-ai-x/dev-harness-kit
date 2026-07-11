---
name: ci-setup
category: bootstrap
description: Install dev-kit's reusable CI workflow templates into a target project. Idempotent; no precondition, no install marker — the 15 EXPECTED_PATHS land as plain files you own. Re-run with `--force` to refresh templates.
when_to_use: |
  - User types `/dev-kit:ci-setup` after `/dev-kit:bootstrap`
  - User wants the same CI shape (branch-policy + validate + test + auto-fix) in a new repo
  - User is preparing a repo for /dev-kit:build (ci-setup is a precondition)
  - Re-run to refresh templates (`--force` flag)
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: opus
disable-model-invocation: false
---

# /dev-kit:ci-setup — Install CI Templates

## Iron Law

**0-arg default OK; `--force` is the only visible flag. Hidden flags: `--target DIR`, `--skip-verify`. Never modifies the dev-kit repo (only writes into target). No precondition, no contract; the templates are plain files you own.**

The skill surfaces **lint warnings** (non-fatal) via `lib/ci_setup.py:lint_installed_workflows()`. Warnings flag known-stale patterns in previously-installed workflows -- e.g. the pre-0.1.3 gate in `templates/ci/.github/workflows/review.yml` that hard-failed in `pull_request` mode on missing verdicts while defaulting to Approve in `workflow_dispatch` mode (an internal inconsistency that produced spurious CI failures whenever the `/dev-kit:*` agents skipped posting a verdict comment). Warnings never block the install; the user acts on them by re-running with `--force` to refresh the template.**

## 3-Phase Orchestration

### Phase 1 — Detect (deterministic, no LLM call)

1.1. Parse arguments: `--target DIR` defaults to `$PWD`; `--force` overwrites existing files; `--skip-verify` skips Phase 3.
1.2. Check `python3 ≥ 3.10` (dev-kit requirement).
1.3. **Delegate file-presence short-circuit to `lib/ci_setup.py:install_ci_config()`** — when every `EXPECTED_PATH` already exists in `target_dir` and `force=False`, it returns an `InstallReport` with all paths in `skipped` and exits without re-touching anything. The skill body surfaces this as "already installed; pass `--force` to refresh" and exits 0.
1.4. Probe target prerequisites: `.git/` (warn if absent — CI is git-themed), `.github/` (create if absent).

### Phase 2 — Install (via `lib/ci_setup.py`)

```bash
python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, 'lib')
from ci_setup import install_ci_config
report = install_ci_config(Path('${TARGET_DIR}'), force=${FORCE})
print(f'created={len(report.created)} overwritten={len(report.overwritten)} skipped={len(report.skipped)} errors={len(report.errors)}')
sys.exit(0 if report.ok and not report.errors else 1)
"
```

2.1. `lib/ci_setup.py:install_ci_config()` resolves the plugin's `templates/ci/` tree (relative to its own `__file__`).
2.2. For each of the 15 `EXPECTED_PATHS` (3 workflow .yml + 1 pre-push hook + 4 scripts + 5 hook files + 1 rules file + 1 test):
  - Skip if exists and `force=False` (idempotent).
  - Overwrite if exists and `force=True`.
  - `shutil.copy2` (preserves mtime for git diff stability).
2.3. `chmod 0o755` on shell scripts + pre-push + validate.py.

### Phase 3 — Verify (deterministic, exit code quoted)

Unless `--skip-verify`:

3.1. `bash -n` on every installed `.sh` and `.githooks/pre-push`.
3.2. `python3 -c "import ast; ast.parse(open(p).read())"` on every installed `.py`.
3.3. `python3 scripts/validate.py` — expect exit 0, stdout contains `OK: CI installation valid`.
3.4. `bash scripts/ci-local.sh` — expect exit 0 (skips test if no `tests/`).
3.5. `act -l 2>/dev/null || echo "act not installed; falling back to scripts/ci-local.sh"` — WARN, not FAIL.

Print summary table (file → outcome: created/overwritten/skipped/error) + pointer to `docs/ci-setup.md`.

## Phase 1.7 -- Lint pass (non-fatal; always runs, even on no-op idempotent re-install)

After install (and after the no-op short-circuit when every EXPECTED_PATH already exists), the skill invokes `lib/ci_setup.py:lint_installed_workflows(target_dir)` and prints every finding as a row in the summary table:

```
| Path                                              | Outcome   |
|---------------------------------------------------|-----------|
| .github/workflows/review.yml                      | warning:  |
|   stale pull_request hard-fail gate ... (0.1.3+)  |           |
```

The lint is purely advisory (`InstallReport.warnings`, not `errors`). The install still succeeds; the user is expected to act on findings -- in the gate-tolerance case, by re-running with `--force` to copy the patched template over the stale workflow file.

The lint pass catches patterns that local `validate.py` + `ci-local.sh` both pass (they don't exercise the GitHub Actions gate), so a clean local run is no longer the only green-light signal.

## Phase 1.5 -- Pre-flight probe (silent when gh is absent)

Before Phase 2 runs, the skill probes the consumer's environment and prints
one of OK / WARN / INFO / SKIP / FAIL per dependency. All calls are
read-only (gh repo view, gh secret list --json name, gh auth status); the
skill never prints secret values. A failed probe NEVER blocks install.

| Probe target          | gh command                              | Returns                |
|-----------------------|------------------------------------------|------------------------|
| gh auth status        | gh auth status                           | OK / SKIP              |
| Repo reachable        | gh repo view OWNER/REPO --json name      | OK / WARN              |
| DEV_KIT_GITHUB_TOKEN  | gh secret list --json name               | OK / WARN              |
| MINIMAX_API_KEY       | gh secret list --json name               | OK / WARN              |
| ANTHROPIC_API_KEY     | gh secret list --json name               | OK / INFO (opt-in)     |

When gh is absent or unauthenticated, every probe returns SKIP and the
skill prints a one-line note. The user can still install; the post-install
checklist alone guides them.

## Phase 4 -- Post-install checklist (printed on success when opted in)

After install_ci_config() returns ok=True AND print_checklist=True, the
skill prints the canonical 5-step checklist (see lib/ci_setup.py:POST_INSTALL_CHECKLIST).
OWNER/REPO is auto-filled from `git remote get-url origin` if a remote is
configured; otherwise the literal placeholder is shown so the user can
edit it. The checklist NEVER blocks -- it is guidance only.

## Rules

- **Idempotent by default** — re-running without `--force` writes zero files.
- **`--force` overwrites** ONLY files inside `EXPECTED_PATHS`. Never delete user-created files outside that set.
- **Never modifies dev-kit's own repo** — only writes into the target.
- **Refuse to install onto a non-directory** — raise clearly.
- **Failure exit codes**: 1 = arg error, 2 = `--force` required + absent, 3 = copy failure, 4 = verify failure.

## Hand-off

- For full usage docs: see `docs/ci-setup.md`.

## Files Installed (15 expected paths)

| Path | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Branch-policy warn + test + validate jobs |
| `.github/workflows/auto-fix-pr.yml` | Auto-fix loop on `changes_requested` review (5-iter cap) |
| `.github/workflows/review.yml` | `/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) PR fan-out + severity gate |
| `.githooks/pre-push` | Client-side block of `git push` to main (activation: `git config core.hooksPath .githooks`) |
| `scripts/validate.py` | Extracted from dev-kit's `ci.yml` 5-step validate job; checks install + bash syntax |
| `scripts/test.sh` | Pytest wrapper (gracefully skips if no `tests/`) |
| `scripts/branch-policy.sh` | Mirror of `pre-push` for CI script context |
| `scripts/ci-local.sh` | Local-runner entrypoint: `validate.py` + `test.sh` + optional `act -l` |
| `hooks/worktree-guard.sh` | PreToolUse Write/Edit block on main checkout (fails closed when jq missing) |
| `hooks/task-detector.sh` | UserPromptSubmit gentle nudge when intent + main checkout |
| `hooks/session-start-check.sh` | SessionStart reminder when started in main checkout |
| `hooks/lib/worktree-detect.sh` | Shared `--git-dir`/`--git-common-dir` discriminator for all rule hooks |
| `hooks/hooks.json` | Wires the 4 hook files into the right event matchers |
| `.claude/rules/git-workflow.md` | Branch / worktree / PR conventions (Iron Law rule text) |
| `tests/test_worktree_guard.py` | Regression tests for the 4 rule hooks + hooks.json wiring |

## Iron Law (repeated, for emphasis)

**Idempotent. Marker-driven. Never modifies dev-kit's own repo.**
