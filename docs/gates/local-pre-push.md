# Local pre-push gates (issue-sync + opt-in stages)

> Issue #833, PR #832 design (closed without merge on 2026-09-14),
> Phase 1: install a local pre-push hook that mirrors the GH Actions
> `issue-sync` gate so a stale-ref push is caught **locally, before
> any LLM judge fires**. Phase 2: opt-in stages under
> `.dev-kit/local-gates.yaml`.

The pre-push hook (`hooks/pre-push.sh`) is the engine. It reads
`.dev-kit/local-gates.yaml` if present and runs each `enabled: true`
opt-in stage. The `issue-sync` stage is **always on** and cannot be
disabled — it is the load-bearing piece (catches the PR #829 failure
mode at zero provider-token cost).

## Per-gate contract

| Stage | Default | Wall-clock | RSS | Purpose | Cost saved per caught failure |
|---|---|---|---|---|---|
| `issue-sync` (always-on) | **on** | ~9s | <10MB | `tools/issue_sync.py pre-push --strict --from-log` reads `git log origin/main..HEAD`, parses every commit body for `#N` references, and runs `gh api` per ref. Closes #N + bare #N against a closed issue fails the push. | **$1-3 of provider tokens** (LLM judges would have fired otherwise) |
| `validate` (opt-in) | off | ~10s | ~50MB | Local mirror of `.github/workflows/validate.yml`. Catches file-shape + dependency drift before push. | ~30s of GH Actions minutes + a red check |
| `lint` (opt-in) | off | ~7s | ~30MB | `ruff check .` for fast lint feedback. | ~20s of GH Actions minutes |
| `test` (opt-in) | off | ~30-120s | ~200-500MB | `pytest tests/ -q --tb=line -x` ignoring `test_review_gate.py` + `test_security_gate.py` (those are the slowest). | ~1-3 min of GH Actions minutes |
| `cache_decay_audit` (opt-in) | off | ~30s | ~200MB | `pytest tests/test_cache_decay.py tests/test_skill_frontmatter.py`. Catches prompt-cache + skill-frontmatter regressions. | ~1 min of GH Actions minutes + provider tokens if the regression would have hit an LLM judge |

**Default Tier A wall-clock** (always-on `issue-sync` only).
**Default Tier A RAM**: <10MB. **Local CPU/memory footprint for
opt-out default**: zero pytest, zero ruff, zero validate. The
opt-in stages are exactly that — opt-in.

## Why these stages, and not others

The brief asked for a per-gate cost contract. The four opt-in stages
match the deterministic gates `.github/workflows/*.yml` already run;
moving them locally trades local CPU for GH Actions minutes. The
stages we explicitly do **not** mirror locally:

- **Branch-policy** — already enforced by `hooks/git-guard.sh`
  (PreToolUse Bash, blocks direct push to main). Duplicating it in
  `pre-push.sh` would re-implement the same check.
- **Cost-flag** — adds a PR label; doesn't save provider tokens.
- **`/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance`**
  — LLM judges. They stay in GH Actions because that's where
  provider tokens are intentionally spent (the comments land on the
  PR there). Local mirror would either skip the comments (defeats
  the purpose) or duplicate the spend (no token saving).
- **Provider-token spend on stale pushes** — the **whole reason**
  `issue-sync` is always-on locally. Caught locally → LLM judges
  never fire → $0 token spend on a broken push.

## Install / uninstall

```bash
# Install (idempotent, worktree-aware — uses --git-common-dir):
bin/install-pre-push.sh

# Install with a copy instead of a symlink (rare; cross-fs):
INSTALL_MODE=copy bin/install-pre-push.sh

# Uninstall:
bin/install-pre-push.sh --uninstall

# Skip for one push (hotfix-only escape):
git push --no-verify
```

The install script is safe to re-run: an unchanged target exits 0
without re-writing. A divergent target (e.g. a previous symlink to a
different file) is replaced after printing a clear status. Worktree
installations resolve `--git-common-dir` and symlink into the shared
hooks directory so a single install covers all worktrees.

## Behavior contract

1. **Default branch is `main`.** `git push` to `main` is blocked by
   `hooks/git-guard.sh` (PreToolUse Bash). The pre-push hook runs
   before git itself evaluates the push, so a stale-ref push to a
   feature branch is caught even earlier than the gate.
2. **Audit-trail PRs.** When the `audit-trail` label is on a PR,
   the **remote** `issue-sync.yml` runs in `--lenient` mode (warn on
   `Issue #N (closed)`, hard-fail on `Closes #N (closed)`). The
   **local** pre-push hook always runs `--strict` — audit-trail
   preservation is a remote-side concern; local commits shouldn't
   carry closed refs at all.
4. **No network on `--offline`.** `tools/issue_sync.py pre-push
   --offline` skips `gh api` and emits one warning per ref. Useful
   when `gh auth status` fails (developer can re-push after
   `gh auth login`).
5. **`--no-verify` is hotfix-only.** Documented but discouraged per
   the `feedback-pre-commit-auto-fix-loop.md` policy.

## Where the source lives

| File | Purpose |
|---|---|
| `hooks/pre-push.sh` | Hook shell. Always-on `issue-sync` stage; opt-in stages wired against `.dev-kit/local-gates.yaml`. |
| `bin/install-pre-push.sh` | Idempotent install (symlink or `INSTALL_MODE=copy`). |
| `tools/issue_sync.py` | Parser + `pre-push` subcommand (single source of truth between local + remote). |
| `.dev-kit/local-gates.yaml.example` | Copy-to-enable template for the four opt-in stages. |
| `.github/workflows/issue-sync.yml` | Remote gate; calls the same `pre-push` subcommand. Honors `audit-trail` label. |
| `tests/test_pre_push_hook.py` | Hook regression (3 fixtures: clean / fail-issue-sync / opt-in-on). |
| `tests/test_issue_sync_pre_push.py` | `pre-push` subcommand end-to-end (subprocess + mocked `gh`). |
| `tests/test_issue_sync_keyword_set.py` | v1.0.0 → v1.1.0 `Issue #N` keyword expansion fixture. |

## Success metric

- Pre-LLM provider-token spend on caught stale-ref pushes: **$0**
  (was $1-3 baseline; the local hook catches before GH Actions).
- PRs merged with a red `issue-sync` gate: **0** (was 1 baseline — PR #829).
- Wall-clock per push on a clean tree: **<10s typical**, **<50MB
  RSS**, no extra Python process spawn beyond the parser.

## Out of scope

- Removing any remote GH Actions gate.
- Replacing LLM judges (review / security / maintenance stay in GH Actions).
- Wholesale CI runtime optimization (this is about token spend, not wall-clock).
- Branch-protection hardening (Phase 3: required-checks toggle is a
  separate change with its own rollback path).