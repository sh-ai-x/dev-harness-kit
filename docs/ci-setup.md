# `/dev-kit:ci-setup` — Install Dev-Kit's CI Templates

The `/dev-kit:ci-setup` skill installs dev-kit's reusable CI workflow templates, Git hooks, and local-runner scripts into any project that has already been bootstrapped via `/dev-kit:bootstrap`. It exists so the same CI shape — branch-policy guards, three-job validate/test/auto-fix, severity-gated review — can be replicated across every repo in your fleet with one command.

## Post-install checklist

After `/dev-kit:ci-setup` installs the CI templates, do these IN ORDER:

1. **Add GitHub secrets.** The review + security workflows need LLM credentials. Use `gh secret set` from your local terminal:
   ```bash
   gh secret set DEV_KIT_GITHUB_TOKEN --repo <owner>/<repo> --app actions    # PAT scoped to sh-ai-x/dev-harness-kit
   gh secret set MINIMAX_API_KEY --repo <owner>/<repo>                        # (or ANTHROPIC_API_KEY)
   ```
   The first secret is required only if `sh-ai-x/dev-harness-kit` is private.
2. **Enable the pre-push hook** so direct pushes to `main` are blocked client-side:
   ```bash
   git config core.hooksPath .githooks
   ```
3. **Open a feature PR first** that does NOT modify `.github/workflows/*` — this is your smoke test for review + security.
4. **The first PR that ADDS `review.yml`** cannot have the action validated by the severity gate until `review.yml` lands on the default branch. Merge that bootstrap PR first; the gate works on every PR after.

The skill prints this checklist automatically (via `lib/ci_setup.py:POST_INSTALL_CHECKLIST`) when invoked with the `print_checklist=True` kwarg; the staged installer in Phase 4 surfaces it after a successful install.

## When to use it

Run `/dev-kit:ci-setup` once per project (independently of `/dev-kit:bootstrap` and `/dev-kit:build` — none are preconditions for the others). The skill is idempotent, so re-running it is safe (use `--force` to refresh templates after dev-kit upgrades its CI shape).

## What gets installed

The skill copies 15 files from the `templates/ci/` source tree into the target project (was 8 in 0.1.0; added the 4 worktree-rule files in 0.1.1):

| Path | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Branch-policy warn + `pytest` test + `validate.py` validator jobs |
| `.github/workflows/auto-fix-pr.yml` | Auto-fix loop on `changes_requested` review (5-iteration cap, label counter, forbidden-path guard) |
| `.github/workflows/review.yml` | `/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) PR fan-out + severity gate. **Self-aware install step** (0.1.1+): detects self-install vs consumer-install at runtime |
| `.githooks/pre-push` | Client-side block of `git push` to `main`; activate with `git config core.hooksPath .githooks` |
| `scripts/validate.py` | Extracted from dev-kit's own `ci.yml` 5-step validate job; checks install + bash syntax |
| `scripts/test.sh` | `pytest` wrapper (gracefully skips if no `tests/` directory) |
| `scripts/branch-policy.sh` | Mirror of `pre-push` for CI script context |
| `scripts/ci-local.sh` | Local-runner entrypoint: `validate.py` + `test.sh` + optional `act -l` |
| **`hooks/worktree-guard.sh`** (0.1.1+) | PreToolUse (Write\|Edit\|MultiEdit) — hard-block edits in the main checkout |
| **`hooks/task-detector.sh`** (0.1.1+) | UserPromptSubmit — nudge new tasks to a worktree |
| **`hooks/session-start-check.sh`** (0.1.1+) | SessionStart — gentle reminder about the worktree rule |
| **`hooks/lib/worktree-detect.sh`** (0.1.1+) | Shared `--git-dir == --git-common-dir` discriminator for the 3 hooks above |
| **`hooks/hooks.json`** (0.1.1+) | Wires all 3 worktree-rule hooks (plus the original 5) into Claude Code's hook events |
| **`.claude/rules/git-workflow.md`** (0.1.1+) | The worktree rule (every task = new worktree + new session + new branch) |
| **`tests/test_worktree_guard.py`** (0.1.1+) | 14 regression tests covering the worktree rule (blocks/allows/executable bits/etc.) |

After install, the 15 EXPECTED_PATHS files are in place. There is no separate marker; the CI templates are plain files you own.

## How to verify

```bash
bash scripts/ci-local.sh
```

This is the same set of checks GitHub Actions runs in `ci.yml`, but without requiring `nektos/act` or push access. Expected output:

```
=== validate ===
validate.py — repo_root=/path/to/repo
  - installation complete OK (8 files)
  - bash syntax OK (5 scripts clean)
  - test runner OK (bash -n clean)
OK: CI installation valid

=== test ===
... (pytest output, or "skip" if no tests/)
```

Optional: `act -l` lists the discovered workflows if `nektos/act` is installed; the script warns and falls back gracefully if not.

## Hand-off to build

`/dev-kit:ci-setup` and `/dev-kit:build` are independent skills. Neither is a precondition for the other. Install CI templates whenever you want them; run build on whatever cadence you want. There is no gate message to chase.

## FAQ

### Why is my first PR's severity gate showing `::warning::review verdict missing`?

Since 0.1.3 the gate tolerates missing verdicts in BOTH `pull_request` and `workflow_dispatch` modes — empty R or S now produces `::warning::` + default `Approve`, not a hard failure. This is intentional: the human gate (`REVIEW_REQUIRED` / `CHANGES_REQUESTED` on the PR) is what blocks merge, not a single missing agent verdict. Real review feedback (`Changes Requested` / `Blocked`) still exits 1 and blocks the PR. The `::warning::` is informational so you know the AI verdict was empty (action skipped, rate-limited, or transient error) and you can investigate.

For the very first PR that ADDS `.github/workflows/review.yml`, the action still cannot validate the new workflow file against `main` (workflow-validation gate). Merge that bootstrap PR first; subsequent PRs flow through normally.

### Why does the skill complain `DEV_KIT_GITHUB_TOKEN is required for consumer-install`?

That secret is only needed when `sh-ai-x/dev-harness-kit` (the upstream source) is private. If your fork / mirror is public, set `DEV_KIT_GITHUB_TOKEN` to any non-empty placeholder token (e.g. `gh token`) — the install step will short-circuit to a public clone via `git clone https://github.com/...`.



**Q: Will it overwrite my existing `.github/workflows/ci.yml`?**
A: No — re-running without `--force` is idempotent and will skip existing files. Use `--force` to refresh after dev-kit's templates evolve.

**Q: Do I need `nektos/act`?**
A: No. `scripts/ci-local.sh` runs the same validators locally on any POSIX host. `act` is optional — install from <https://nektos.act.dev> if you want full GitHub Actions parity (e.g., Docker-based matrix testing).

**Q: How do I uninstall?**
A: `git rm` the 8 installed files in `.github/`, `.githooks/`, and `scripts/` (or `rm -rf` them if the target repo is freshly built and not yet under version control). The CI templates are intentionally not deeply integrated — they're plain files you own.

**Q: My CI fails on `Install dev-kit plugin` with `DEV_KIT_GITHUB_TOKEN secret is required`. What now?**
A: The dev-harness-kit source repo (`sh-ai-x/dev-harness-kit`) is private. The consumer-install branch of `review.yml` clones it via `git clone https://x-access-token:${DEV_KIT_GITHUB_TOKEN}@github.com/sh-ai-x/dev-harness-kit.git`. To make this work in your CI:

  1. Create a **fine-grained personal access token** at <https://github.com/settings/tokens?type=beta> with:
     - **Resource owner:** `sh-ai-x` (or wherever dev-harness-kit lives)
     - **Repository access:** `sh-ai-x/dev-harness-kit` only
     - **Permissions → Repository permissions:** `Contents: Read-only`
  2. In this consumer repo, go to **Settings → Secrets and variables → Actions → New repository secret**:
     - **Name:** `DEV_KIT_GITHUB_TOKEN`
     - **Value:** paste the fine-grained PAT from step 1

  The install step exposes the secret as `${{ secrets.DEV_KIT_GITHUB_TOKEN }}` in both the `review` and `security` jobs. Without it, the consumer-install branch fails fast (exit 1) with a clear `::error::` message instead of a generic git auth failure.

  If dev-harness-kit is later made public, you can remove the secret and `git clone` will work without credentials. Re-run `/dev-kit:ci-setup --force` to refresh the install step if you want.

**Q: Can I customize a file without losing changes on refresh?**
A: Yes — when `/dev-kit:ci-setup --force` rewrites an `EXPECTED_PATHS` file, it does so verbatim from the template. Customizations live OUTSIDE that set (e.g., extra workflow files in `.github/workflows/`, additional Git hooks beyond `pre-push`). Files outside `EXPECTED_PATHS` are never touched.
