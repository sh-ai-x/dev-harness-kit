# `/dev-kit:ci-setup` — Install Dev-Kit's CI Templates

**Language:** English · [한국어](ci-setup.ko.md)

The `/dev-kit:ci-setup` skill installs dev-kit's reusable CI workflow templates, Git hooks, and local-runner scripts into any project that has already been bootstrapped via `/dev-kit:bootstrap`. It exists so the same CI shape — branch-policy guards, three-job validate/test/auto-fix, severity-gated review — can be replicated across every repo in your fleet with one command.

## Post-install checklist

After `/dev-kit:ci-setup` writes `.dev-kit/ci-config.json`, do these IN ORDER:

1. **Add GitHub secrets + the provider variable.** The review + security workflows need LLM credentials AND a provider selector. From a local terminal:
   ```bash
   gh secret   set DEV_KIT_GITHUB_TOKEN --repo <owner>/<repo> --app actions   # PAT scoped to sh-ai-x/dev-harness-kit
   gh secret   set MINIMAX_API_KEY       --repo <owner>/<repo>                # or ANTHROPIC_API_KEY / DEEPSEEK_API_KEY
   gh variable set CI_REVIEW_PROVIDER    --repo <owner>/<repo> --body minimax
   ```
   `DEV_KIT_GITHUB_TOKEN` is required only if `sh-ai-x/dev-harness-kit` is private. The provider secret + `CI_REVIEW_PROVIDER` pairing is covered in [GitHub variables — provider selection](#github-variables--provider-selection) below.
2. **Install Ruff and enable the Git hooks** so staged Python files are linted and direct pushes to `main` are blocked client-side:
   ```bash
   brew install ruff                              # macOS
   apt install ruff                               # Debian/Ubuntu
   git config core.hooksPath .githooks
   ```
3. **Open a feature PR first** that does NOT modify `.github/workflows/*` — this is your smoke test for review + security.
4. **The first PR that ADDS `review.yml`** cannot have the action validated by the severity gate until `review.yml` lands on the default branch. Merge that bootstrap PR first; the gate works on every PR after.

The skill prints this checklist automatically (via `lib/ci_setup.py:POST_INSTALL_CHECKLIST`) when invoked with the `print_checklist=True` kwarg; the staged installer in Phase 4 surfaces it after a successful install.

## GitHub variables — provider selection

The review + security workflows read `vars.CI_REVIEW_PROVIDER` to choose which LLM provider to call. The variable's value MUST match an existing `*_API_KEY` secret — a mismatch makes `review.yml` exit 1 with `Error: provider secret missing`.

Set it after `/dev-kit:ci-setup`:

```bash
gh variable set CI_REVIEW_PROVIDER --repo <owner>/<repo> --body minimax    # or: anthropic | deepseek
```

| `CI_REVIEW_PROVIDER` | Secret read by workflow | When to pick |
|---|---|---|
| `minimax` (kit default) | `${{ secrets.MINIMAX_API_KEY }}` | Default for kit development and fleet rollouts |
| `anthropic` | `${{ secrets.ANTHROPIC_API_KEY }}` | Pick when the reviewer must be Claude (Opus / Sonnet) |
| `deepseek` | `${{ secrets.DEEPSEEK_API_KEY }}` | Pick for low-cost review on large diffs |

The allowlist (`minimax`, `anthropic`, `deepseek`) is enforced by `review.yml -> workflow_dispatch.inputs.provider.options` and by `bin/set-provider.sh`. Any other value fails the workflow with `Error: unsupported provider`.

Verify both halves:

```bash
gh variable list --repo <owner>/<repo> | grep CI_REVIEW_PROVIDER
gh secret   list --repo <owner>/<repo> | grep -E '(MINIMAX|ANTHROPIC|DEEPSEEK)_API_KEY'
```

The matching local selector is `.env:CI_REVIEW_PROVIDER` (managed via `bin/set-provider.sh <provider>`); the local half is gitignored and per-user, while the GitHub variable is per-repo. The `provider-divergence-check.sh` SessionStart hook nudges when the two disagree.

> The `--setup-secrets` flag of `/dev-kit:ci-setup` reads `CI_REVIEW_PROVIDER`, enumerates the required secret via `required_secrets_for_provider()`, and prompts for each before calling `gh secret set`. Install still succeeds on secret-set failure (warning, not error).

## When to use it

Run `/dev-kit:ci-setup` once per project, after `/dev-kit:bootstrap` and before `/dev-kit:build`. The skill is idempotent, so re-running it is safe (use `--force` to refresh templates after dev-kit upgrades its CI shape).

## What gets installed

The skill copies the CI templates and the canonical hook source tree into the
target project. Workflow/script files are explicit; `hooks/hooks.json`, all
`hooks/**/*.sh` files, and their non-code data (`hooks/references/**`, e.g.
`slop-detector.sh`'s phrase/structure banks) are derived from the plugin
source so hook code — and the data it depends on — is not duplicated under
`templates/ci/`.

This ships the **complete** hook tree (26 `.sh` files, 18 registered
entrypoints as of this writing), not just the worktree-rule subset a prior
version shipped. Several of these are dev-kit-internal hooks (e.g.
`git-guard.sh`'s release-slot check, `linear-autosync.sh`) that fail open in a
consumer repo when their dev-kit-specific preconditions (like
`.claude-plugin/plugin.json` on `origin/main`) aren't present — but they do
run. Deriving the full tree from source (instead of hand-curating a subset)
is the fix for #273/#277/#310: a hand-maintained list silently drops newly
added hooks and their helpers.

| Path | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Branch-policy warn + `pytest` test + `validate.py` validator jobs |
| `.github/workflows/auto-fix-pr.yml` | Auto-fix loop on `changes_requested` review (5-iteration cap, label counter, forbidden-path guard) |
| `.github/workflows/review.yml` | `/dev-kit:review` (3-dim) + `/dev-kit:security` (10-dim) PR fan-out + severity gate. **Self-aware install step**: detects self-install vs consumer-install at runtime |
| `.githooks/pre-push` | Client-side block of `git push` to `main`; activate with `git config core.hooksPath .githooks`. The dev-kit source repo also keeps a Ruff lint gate in the sibling `.githooks/pre-commit`; it is not copied to consumers. |
| `scripts/validate.py` | Extracted from dev-kit's own `ci.yml` 5-step validate job; checks install + marker + bash syntax |
| `scripts/test.sh` | `pytest` wrapper (gracefully skips if no `tests/` directory) |
| `scripts/branch-policy.sh` | Mirror of `pre-push` for CI script context |
| `scripts/ci-local.sh` | Local-runner entrypoint: `validate.py` + `test.sh` + optional `act -l` |
| **`hooks/**/*.sh`** | Complete canonical hook implementation set, including shared helpers |
| **`hooks/hooks.json`** | Canonical registration manifest copied with all hook sources |
| **`rules/git-workflow.md`** | Canonical worktree rule; installed to `.claude/rules/git-workflow.md` for Claude Code discovery |
| **`tests/test_worktree_guard.py`** | regression tests covering the worktree rule (blocks/allows/executable bits/etc.) |
| `tools/_repo_name.py` | Shared `main_repo_root` + `repo_name` helper used by the Linear sync tools |
| `tools/linear_sync.py` | Edit/Write auto-sync entrypoint invoked by `hooks/linear-*.sh` |
| `tools/linear_pr_sync.py` | GH-Actions-driven PR sync entrypoint (workflow picks it up via sparse-checkout) |

After install, the marker file `.dev-kit/ci-config.json` is written at the project root. The marker is the **contract** with `/dev-kit:build` — without it, build refuses to start.

## How to verify

The Claude Code and Codex lifecycle hook definition is shared from
`hooks/hooks.json`. For a local status report, run:

```bash
python3 bin/dev-kit-hooks-status.py
```

In Codex, use `/hooks` after installation or whenever the hook definition
changes to review and trust the plugin hooks. The Git pre-commit and pre-push
hooks are separate from both clients. Install Ruff on the host, then activate
the hook directory:

```bash
brew install ruff                              # macOS
apt install ruff                               # Debian/Ubuntu
git config core.hooksPath .githooks
```

```bash
bash scripts/ci-local.sh
```

This is the same set of checks GitHub Actions runs in `ci.yml`, but without requiring `nektos/act` or push access. Expected output:

```
=== validate ===
validate.py — repo_root=/path/to/repo
  - installation complete OK (8 CI files + 26 hooks)
  - ci-config marker OK
  - bash syntax OK (30 shell files clean)
  - test runner OK (bash -n clean)
OK: CI installation valid

=== test ===
... (pytest output, or "skip" if no tests/)
```

Optional: `act -l` lists the discovered workflows if `nektos/act` is installed; the script warns and falls back gracefully if not.

## Hand-off to build

The skill writes `.dev-kit/ci-config.json` as a marker. `/dev-kit:build` will refuse to start unless this marker exists — no version comparison. If you see the gate message:

```
Pre-flight gate: refuse to start if `.dev-kit/ci-config.json` is absent.
Run `/dev-kit:ci-setup` first.
```

…run `/dev-kit:ci-setup` (or re-run with `--force` if the marker is stale).

## FAQ

### Why is my first PR's severity gate showing `::warning::review verdict missing`?

The gate tolerates missing verdicts in BOTH `pull_request` and `workflow_dispatch` modes — empty R or S now produces `::warning::` + default `Approve`, not a hard failure. This is intentional: the human gate (`REVIEW_REQUIRED` / `CHANGES_REQUESTED` on the PR) is what blocks merge, not a single missing agent verdict. Real review feedback (`Changes Requested` / `Blocked`) still exits 1 and blocks the PR. The `::warning::` is informational so you know the AI verdict was empty (action skipped, rate-limited, or transient error) and you can investigate.

For the very first PR that ADDS `.github/workflows/review.yml`, the action still cannot validate the new workflow file against `main` (workflow-validation gate). Merge that bootstrap PR first; subsequent PRs flow through normally.

### Why does the skill complain `DEV_KIT_GITHUB_TOKEN is required for consumer-install`?

That secret is only needed when `sh-ai-x/dev-harness-kit` (the upstream source) is private. If your fork / mirror is public, set `DEV_KIT_GITHUB_TOKEN` to any non-empty placeholder token (e.g. `gh token`) — the install step will short-circuit to a public clone via `git clone https://github.com/...`.



**Q: Will it overwrite my existing `.github/workflows/ci.yml`?**
A: No — re-running without `--force` is idempotent and will skip existing files. Use `--force` to refresh after dev-kit's templates evolve.

**Q: Do I need `nektos/act`?**
A: No. `scripts/ci-local.sh` runs the same validators locally on any POSIX host. `act` is optional — install from <https://nektos.act.dev> if you want full GitHub Actions parity (e.g., Docker-based matrix testing).

**Q: How do I uninstall?**
A: Delete `.dev-kit/ci-config.json`, then `git rm` the 15 installed files (or `rm -rf` them if the target repo is freshly built and not yet under version control). The CI templates are intentionally not deeply integrated — they're plain files you own.

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

**Q: Why is the marker file versioned?**
A: So `/dev-kit:build` can refuse to run on stale templates after a dev-kit upgrade that changes the CI shape. Re-run `/dev-kit:ci-setup --force` after upgrading dev-kit to pick up new validator logic.

**Q: Can I customize a file without losing changes on refresh?**
A: Yes — when `/dev-kit:ci-setup --force` rewrites an `EXPECTED_PATHS` file, it does so verbatim from the template. Customizations live OUTSIDE that set (e.g., extra workflow files in `.github/workflows/`, additional Git hooks beyond `pre-push`). Files outside `EXPECTED_PATHS` are never touched.
