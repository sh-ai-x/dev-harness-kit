---
name: bootstrap
category: bootstrap
description: 0-arg orchestrator. Writes minimal CLAUDE.md + AGENTS.md + active-hooks.json on a fresh repo. No noise files by default.
alpha: state
when_to_use: |
  - User types `/dev-kit:bootstrap` 1st time on a new project
  - User wants to refresh CLAUDE.md / active-hooks.json
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch
model: opus
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# /dev-kit:bootstrap — One-shot setup (CLAUDE.md + optional CI)

## What it does

Runs the deterministic bootstrap pipeline (sanity -> codebase-map -> hook-matrix -> write-claude-md), then prompts the operator for whether to also install CI. On a fresh repo, the unconditional bootstrap set lands on disk: `CLAUDE.md`, `AGENTS.md`, `.dev-kit/.active-hooks.json`, `iron-laws/index.md`, `guidelines/index.md`, `hooks/index.md`, plus `rules/index.md` if `rules/` exists. CI is opt-in via a single Y/n prompt (or `--skip-ci` / `--yes` flags).

If Y: also runs `lib/ci_setup.py:install_ci_config()` to install the 15 CI workflow templates, pre-push hook, `.dev-kit/ci-config.json` marker, Phase 1.5 pre-flight probe, Phase 1.7 lint, and Phase 3 verify. End state on disk is identical to the legacy `/dev-kit:bootstrap-full` slash.

If N (or `--skip-ci`): prints the unavailable-features list below and exits with code 0. CI can be added later via `/dev-kit:ci-setup --force`.

## Iron Law (no exceptions)
**0-arg default OK.** Hidden flags: `--target DIR` (all sub-stages — sanity, codebase-map, hook-matrix, write-claude-md, and the conditional ci-setup — operate on `<DIR>` instead of `$PWD`; pass `target=` to `install_ci_config()`), `--skip-sanity`, `--skip-map`, `--slim|--full`, `--team`, `--strict`, `--persist-audit`, `--skip-ci` (skip ci-setup, equivalent to answering `n`), `--skip-git-defaults` (skip sub-stage 7 + 8 git-defaults, equivalent to answering `n` on both the prompt and the execution), `--yes` (skip the prompt, default `Y`), `--with-ci` (skip the ci-setup prompt and assume `Y`; preserves the legacy default for operators who always want CI gates), `--force` (overwrite existing CI templates during ci-setup), `--skip-verify` (skip ci-setup Phase 3 verify).

## 9-Step Orchestration (4 auto + 1 prompt + 1 ci-setup + 2 git-defaults + 1 user review)

```
[1] sanity                -> stdout only (file only with --persist-audit)
       | (auto, deterministic regex + glob)
[2] codebase-map          -> section 3 (lazy-loading index; consumed only by --full-claude-md)
       | (auto, Read + Glob + Bash; only consumed by --full-claude-md)
[3] hook-matrix           -> .dev-kit/.active-hooks.json (SSOT)
       | (auto)
[4] write-claude-md lib/write_project_md.py -> CLAUDE.md + AGENTS.md + 4 index.md files
       | (auto)
[5] ci-setup prompt       -> "Also install CI templates (ci-setup)? [y/N]"
       | (N default; auto if --yes/--with-ci; skip if --skip-ci)
[6] ci-setup              -> lib/ci_setup.py:install_ci_config() (only if Y/--yes)
       |-- 1.5 pre-flight probe
       |-- 15 EXPECTED_PATHS + .dev-kit/ci-config.json marker
       |-- 1.7 lint pass (warnings non-fatal)
       |-- 4 post-install checklist
       | (skip if N; skip verify if --skip-verify)
[7] git-defaults prompt   -> "Also configure operator-global git defaults (rebase.autoStash + pull.rebase)? [Y/n]"
       | (Y default; auto if --yes; skip if --skip-git-defaults)
[8] git-defaults          -> bin/setup-git-defaults.sh (only if Y/--yes)
       | (idempotent; --check available; --dry-run available; safe to re-run)
[9] exit -> HOTL review -> next: /dev-kit:build (or /dev-kit:plan for idea -> PRD.md synthesis)
```

## Sub-stage 1 -- sanity (deterministic, no LLM)

**Iron Law:** never modify files. Read input directory only; emit result to `.dev-kit/sanity-report.md`.

### Gate output

| Result | Condition |
|---|---|
| **PASS** | All required preconditions pass |
| **WARN** | 1~3 WARN (pass-through allowed) |
| **FAIL** | 4+ WARN or 1+ critical -- Plan entry ❌ |

### 7-check audit

| # | Check | Tool | Severity |
|---|---|---|---|
| 1 | `package.json` or `pyproject.toml` exists (manifest) | `Glob` | WARN |
| 2 | `.git/` directory healthy (HEAD exists) | `Bash: git rev-parse --git-dir` | WARN |
| 3 | `docs/` directory has 4 template placeholders | `Glob` | WARN |
| 4 | banned-phrase scan (slop-detector SSOT regex) | `Bash: slop-detector.sh` (read-only) | WARN |
| 5 | secret-scan (credential pattern) | `Bash: secret-scan.sh` (read-only) | **CRITICAL FAIL** |
| 6 | hook bypass detection (DEV_KIT_HOOK_OFF env) | `Bash: env | grep` | WARN |
| 7 | methodology lockfile (lib/methodology.json consistency) | `Read` | WARN |

### Sanity report format

```markdown
# Sanity Report -- dev-harness-kit
- scanned_at: ISO-8601 KST
- target: <absolute path>
- result: PASS / WARN / FAIL
- checks:
  - [PASS] check_1: package.json found
  - [PASS] check_2: .git/ OK
  - [WARN] check_3: docs/DESIGN.md template missing (Bootstrap will create)
  ...
- critical_issues: []
- recommendations:
  - "ok to proceed to /dev-kit:plan"
```

**Rules:** read-only invariant; zero LLM calls; fail fast on 1 critical.

## Sub-stage 2 -- codebase map (deterministic, no LLM)

**Iron Law:** no guessing / padding. Only output from pre-validated tools (glob/cat/jq). On guess, append `STALE: guess` marker + wait for user input.

### Lazy-loading index (default mode)

CLAUDE.md is a slim pointer (no inline tree/manifest/deps/laws). The agent reads
`docs/CODEBASE-MAP.md`, `iron-laws/index.md`, `guidelines/index.md`,
`hooks/index.md`, `rules/index.md` on demand. `--full-claude-md` writes the
full codebase map to `docs/CODEBASE-MAP.md` instead of relying on lazy reads.

### 4-section composition (only when `--full-claude-md`)

`lib/write_project_md.py:render_codebase_map_doc` writes `docs/CODEBASE-MAP.md`:

| Section | Source | Tool |
|---|---|---|
| **Tree** | recursive os.walk (depth 4, exclude `node_modules` `.git` `dist` `__pycache__`) | `os.walk` + path sort |
| **Manifest** | `package.json` / `pyproject.toml` / `go.mod` / `Cargo.toml` (whichever exists) | `Bash: jq` / `Read` |
| **Deps** | lockfile top 10 | `Bash: head -10` |
| **Conventions** | `.editorconfig` / `.eslintrc` / `.prettierrc` / `pyproject.toml [tool.*]` | `Read` |

### Modes

| Mode | Output | Tokens |
|---|---|---|
| default | lazy-loading index in CLAUDE.md | ~100 tokens |
| `--full-claude-md` (opt-in) | `docs/CODEBASE-MAP.md` written (4 sections) | 500~5000 tokens |

**Rules:** determinism (same input -> same output; `jq --sort-keys` + path stable sort); no lockfile mutation; secret mask; `STALE` marker on guess.

## Sub-stage 3 -- hook matrix init (SSOT)

**Iron Law:** all hook active states are decided in one place: `.dev-kit/.active-hooks.json`. `hooks/hooks.json` only registers the matrix reader.

### Output format

```json
{
  "schema_version": "1.0.0",
  "updated_at": "2026-07-04T15:30:00Z",
  "matrix": {
    "bootstrap": { "tdd-guard": false, "bash-guard": false, "secret-scan": "read-only", "slop-detector": false, "stop-verify": false },
    "plan":       { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true },
    "design":     { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true },
    "build":      { "tdd-guard": true,  "bash-guard": true,  "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "review":     { "tdd-guard": false, "bash-guard": false, "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "security":   { "tdd-guard": false, "bash-guard": false, "secret-scan": true,  "slop-detector": true,  "stop-verify": true },
    "ship":       { "tdd-guard": false, "bash-guard": false, "secret-scan": false, "slop-detector": false, "stop-verify": true }
  },
  "override": { "disabled_hooks": [], "strict_mode": false }
}
```

### Hook shell reference

| Hook | Stage ON | Note |
|---|---|---|
| `tdd-guard` | build | active only when lib/methodology/tdd.py is loaded (MUST-48) |
| `bash-guard` | build | patterns like `rm -rf`, destructive git operations |
| `secret-scan` | build / review / security | PostToolUse: credential pattern grep |
| `slop-detector` | build / review / security | KO+EN banned phrases |
| `stop-verify` | plan / design / build / review / security / ship | Stop event: AC claim verification |

**Rules:** all hooks default `exit 0` (MUST-12); `--strict` flag activates `exit 2`; `DEV_KIT_HOOK_OFF=<hook1>,<hook2>` env temporarily disables hooks. Stage transition auto-updates `current_stage` via `lib/state_codec.py`.

## Hook integration (stage=bootstrap)

| Hook | Mode |
|---|---|
| tdd-guard | OFF |
| bash-guard | OFF |
| secret-scan | read-only |
| slop-detector | OFF |
| stop-verify | OFF |

`active-hooks.json` SSOT auto-initialized (MUST-13). With `--strict` all hooks `exit 2`.

## ci-setup prompt (sub-stage 5)

After the unconditional bootstrap set lands on disk, the skill prompts:

```
Also install CI templates (ci-setup)? [y/N]
```

### y branch (default)

Delegates to `lib/ci_setup.py:install_ci_config(force=<--force>)` -- the default is **idempotent** (re-runs on an already-installed repo are no-op). Pass `--force` to overwrite customized CI workflows and the pre-push hook (same code path as `/dev-kit:ci-setup --force`):

- Phase 1.5 pre-flight probe (`gh` deps -> OK/WARN/INFO/SKIP, non-blocking)
- 15 EXPECTED_PATHS installed (`.github/workflows/*.yml`)
- `.dev-kit/ci-config.json` marker written
- Phase 1.7 lint pass (warnings non-fatal)
- Phase 4 post-install checklist

If `--skip-verify` is passed, Phase 3 verify (bash -n, ast.parse, scripts/validate.py, scripts/ci-local.sh) is skipped.

End state matches the legacy `/dev-kit:bootstrap-full` slash exactly.

### n branch (default)

Skips `install_ci_config()`. Prints the unavailable-features list (below) and exits 0. Operators can add CI later via `/dev-kit:ci-setup --force`.

Equivalent to passing `--skip-ci` (no prompt, assume `n`).

### Gate installers available (post-bootstrap)

Run any of these in any order to layer additional gates on top of the minimal bootstrap set:

- `/dev-kit:ci-setup [--force]`               — install project gates (CI workflow templates + pre-push + scripts + `.dev-kit/ci-config.json` marker)
- `/dev-kit:harness-mode fast|full|custom|show` — adjust session local-hook gates (`.dev-kit/harness-mode.session.json`)
- `/dev-kit:gate-select show|pick`            — unified 3-dimension picker (project + session + AI-judge); one-shot read or interactive multi-question install
- `/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance` — AI-judge skills (run locally via Skill; wire into CI via ci-setup's `review.yml`)

## git-defaults prompt (sub-stage 7)

After ci-setup (Y/n), the skill prompts:

```
Also configure operator-global git defaults (rebase.autoStash + pull.rebase)? [Y/n]
```

### Y branch (default)

Delegates to `bin/setup-git-defaults.sh` — idempotent, reads each key via
`git config --global --get`, skips if already at the expected value, otherwise
writes the value and prints `✓ set <key>=<value>`. Safe to re-run; the script
is the single source of truth for which keys belong in the operator's
`~/.gitconfig`.

Settings applied (see `SETTINGS=()` in `bin/setup-git-defaults.sh`):

| Key | Value | Why |
|---|---|---|
| `rebase.autoStash` | `true` | `git pull --rebase` refuses to start on a dirty tree without it; this is the canonical fix from `git config --global rebase.autoStash true`. |
| `pull.rebase` | `true` | Makes `git pull` default to rebase so the dev-kit workflow + `hooks/git-guard.sh` behave predictably. |

Operators can preview or re-apply at any time:

```bash
bin/setup-git-defaults.sh              # apply (idempotent)
bin/setup-git-defaults.sh --check      # see what's missing
bin/setup-git-defaults.sh --dry-run    # preview
```

### n branch

Skips `bin/setup-git-defaults.sh`. Operators can run it manually any time using the commands above. Equivalent to passing `--skip-git-defaults` (no prompt, assume `n`).

## What is unavailable without ci-setup

If you answer `n` (or pass `--skip-ci`), the following features are unavailable until ci-setup runs separately. Install any of these later via `/dev-kit:ci-setup [--force]`, `/dev-kit:harness-mode`, `/dev-kit:gate-select pick`, or the relevant AI-judge skill.

- `/dev-kit:ci-doctor` (drift detection) -- requires `.dev-kit/ci-config.json` marker
- `/dev-kit:bump` version-bump workflow -- requires pre-push hook
- 15 CI workflow templates in `.github/workflows/` (`validate.yml`, `test.yml`, `auto-fix.yml`, etc.)
- Pre-push hook (`.git/hooks/pre-push`)
- `PreCompletionChecklistMiddleware` (PR-level cost flag aggregation)
- `/dev-kit:evaluate` harness-quality gate (depends on ci-setup-installed workflows)

For a single-pane view of all three gate dimensions (project, session, AI-judge) plus a one-shot install command, run `/dev-kit:gate-select show` and then `/dev-kit:gate-select pick`.

## Rules (no exceptions)

- **0-arg UX (MUST-21)**: zero args. Branching via `when_to_use` auto-match.
- **HOTL (MUST-29)**: steps 1~4 auto. Step 5 ci-setup prompt is a single Y/n with no further sub-prompts.
- **YAGNI**: no extra option prompts (MUST-NOT-13). Only hidden flags.
- **No-over-engineering (MUST-25)**: defaults handle 80%. Extra features require ADR.
- **Minimal file footprint**: unconditional set is 6~7 files (CLAUDE.md, AGENTS.md, `.dev-kit/.active-hooks.json`, 4 index.md). With ci-setup, +15 CI workflows + `.dev-kit/ci-config.json` + pre-push hook.

## Next step

After bootstrap + ci-setup (Y): run `/dev-kit:build <first-feature>` to start the canonical plan -> build loop. `/dev-kit:ci-doctor` is also available for post-install drift verification (issue #212-D1).

After bootstrap (N): run `/dev-kit:ci-setup --force` whenever you are ready to add CI. `/dev-kit:plan` is opt-in and only for idea -> PRD.md synthesis -- it is NOT the default next stage.
