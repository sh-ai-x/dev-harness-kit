# Skills index

This index lists every skill shipped by the `dev-kit` plugin. Click into any skill to read its full `SKILL.md`; every `SKILL.md` has a back-link at the top to return here.

The full path of each entry is `skills/<dir>/SKILL.md`. Use `find skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l` to confirm the current inventory.

## By category

### `audit` (9)

| Skill | α | Description |
|---|---|---|
| [`ci-doctor`](ci-doctor/SKILL.md) | `enforcement` | Read-only CI readiness audit. Prints one PASS/FAIL summary across files, marker, provider file, secrets, and gh auth. Hand-off answer to "would CI succeed on my next PR?" |
| [`ci-triage`](ci-triage/SKILL.md) | `enforcement` | Triage failing GitHub Actions runs across recent commits, dedupe against a persisted case store, judge new failures against a model/context/harness taxonomy with a required repro + regression test, and record them witho… |
| [`code-viz`](code-viz/SKILL.md) | `state` | 0-arg generic plugin-architecture visualizer. Walks any target repo, emits self-contained HTML with multi-level views (architecture / code / skill / hook / tools-lib / external) + domain pillar map (DB · Cloud · API · M… |
| [`cost-gate`](cost-gate/SKILL.md) | `enforcement` | 0-arg cost-gate status. Prints current session spend, threshold distance, and a two-line git-trailer block to include in commits so the PR-level cost flag can aggregate. |
| [`docs-maintenance`](docs-maintenance/SKILL.md) | `analysis` | Audit repository documentation, remove superseded guidance, and refresh the README without recording volatile inventory facts. |
| [`hook-doctor`](hook-doctor/SKILL.md) 🔒 | `enforcement` | Diagnose failed Claude Code or Codex hooks, repair safe cache and registration drift, and report the exact restart step. |
| [`inspect`](inspect/SKILL.md) | `analysis` | 0-arg read-only code health audit. 8-dim fan-out (dead, dup, smell, overeng, overarch, cleancode, tokenbudget, slop) -> markdown report. |
| [`learn`](learn/SKILL.md) | `state` | Distill source text into a new SKILL.md with approval gate. |
| [`prune-propose`](prune-propose/SKILL.md) | `state` | 0-arg skill — usage telemetry dump + per-skill delete proposal. User approves each deletion explicitly. |
| [`token-analyzer`](token-analyzer/SKILL.md) | `analysis` | 0-arg token-efficiency dashboard. Runs tools/token_efficiency_analyzer.py over logs/{claude-code,codex}/*.jsonl to produce an HTML report (+ lazy per-worktree transcript sidecars) -- 4-dim session scoring, 6 anti-patter… |

### `bootstrap` (3)

| Skill | α | Description |
|---|---|---|
| [`bootstrap`](bootstrap/SKILL.md) | `state` | 0-arg orchestrator. Writes minimal CLAUDE.md + AGENTS.md + active-hooks.json on a fresh repo. No noise files by default. |
| [`ci-setup`](ci-setup/SKILL.md) | `enforcement` | Install dev-kit's reusable CI workflow templates into a target project. Idempotent via `.dev-kit/ci-config.json` presence, no version gate. Hand-off to /dev-kit:build. |

### `build` (8)

| Skill | α | Description |
|---|---|---|
| [`build`](build/SKILL.md) | `state` | 0-arg. Per-step sub-agent delegation + self-fix loop (MUST-36~38). Uses harness-runner engine. TDD + verify + debug integrated. |
| [`build-debug`](build-debug/SKILL.md) 🔒 | `enforcement` | 4-phase systematic debugging. No fix proposal before Phase 1 (reproduce) completes (MUST-L2). Root-cause-first Iron Law. |
| [`build-refactor`](build-refactor/SKILL.md) 🔒 | `enforcement` | 4-pass cleanup (dead → dup → naming → coverage). No cleanup without regression test (MUST-L1 + L4). |
| [`build-tdd`](build-tdd/SKILL.md) 🔒 | `enforcement` | Red-Green-Refactor cycle. Active when methodology=tdd (default). No production code without a failing test. tdd-guard hook enforces. |
| [`build-verify`](build-verify/SKILL.md) 🔒 | `enforcement` | verification-before-completion. No "done" without quoted exit code + test count + build log (MUST-L3, hook stop-verify). |
| [`prune`](prune/SKILL.md) | `analysis` | 0-arg slop-removal chain. One slash wraps inspect → 3-pass delete sweep → review. Gated phases for deleting AI slop and dead features (not refactoring). |
| [`refactor`](refactor/SKILL.md) | `analysis` | 0-arg cleanup chain. One slash wraps inspect -> build-refactor -> review. 3 gated phases with quoted exit codes between each. |
| [`research-plan-build`](research-plan-build/SKILL.md) | `state` | 3-phase binder (research → plan → implement). Enforces non-skippable phases; cites lib/analysis_core/ for the research half; emits templates/research.md and templates/plan.md. |

### `config` (2)

| Skill | α | Description |
|---|---|---|
| [`config`](config/SKILL.md) | `state` | skill + hook + methodology picker (multiSelect). |
| [`linear`](linear/SKILL.md) | `state` | Optional Linear task tracker. Reconcile the current repository task with a canonical project and non-duplicate issue. Auto-syncs on every Claude Code edit when configured. |

### `design` (5)

| Skill | α | Description |
|---|---|---|
| [`interview`](interview/SKILL.md) | `enforcement` | 5-field safety-contract interview that gates plan emission. Drives `lib.interview_engine` through one Ralph loop, enforces `safety_valve=8`, `narrowed_delta`, `dedup_metric` (identical-ambiguity-cycle=2), and `user_inte… |
| [`proposal`](proposal/SKILL.md) | `state` | 0-arg HTML renderer for design proposals / plans. Renders any docs/proposals/<main>/<sub>.yaml to docs/proposals/<main>/<sub>.html for pre-implementation review. |
| [`research`](research/SKILL.md) | `enforcement` | 0-arg research gate. Run Phase 0-3 escalation (cache / direct / multi / human) + verify() + enforce_citations(). /dev-kit:research <claim> [--max-phase N]. |
| [`sot-harness-writer`](sot-harness-writer/SKILL.md) | `state` | Interview-based Single Source of Truth harness document writer (5 rounds × 2-3 evidence-backed recommendations, full traceability, hands off to /dev-kit:plan). |
| [`valuate`](valuate/SKILL.md) | `enforcement` | Plan-value gate. Scores a plan on 6 axes via LLM judge and returns proceed / revise / hold / kill. Verdict envelope persists to .dev-kit/valuations/<plan-id>.json. |

### `eval` (1)

| Skill | α | Description |
|---|---|---|
| [`evaluate`](evaluate/SKILL.md) | `enforcement` | 0-arg eval extension. Replays transcripts and judges against registered rubrics (harness-quality, os-quality, plus legacy review/security/plan). /dev-kit:evaluate [--harness-quality] [--os-quality] [--case <id>] [--dry-… |

### `plan` (1)

| Skill | α | Description |
|---|---|---|
| [`plan`](plan/SKILL.md) | `state` | 0-arg plan stage. Take 1-line idea → PRD.md + phases/<name>/{index.json, step<N>.md} in 5 gates. Quantified value (cost/LTV) + ambiguity loop (0-10) replace the old 5-question grill-me. |

### `review` (1)

| Skill | α | Description |
|---|---|---|
| [`review`](review/SKILL.md) | `analysis` | Parallel multi-dimension code review with a false-positive filter. Fans out to per-dim experts (correctness, security, architecture) that run in parallel and return evidence-backed findings; a verifier pass confirms/rej… |

### `security` (2)

| Skill | α | Description |
|---|---|---|
| [`security`](security/SKILL.md) | `enforcement` | Full OWASP Top 10 2025 fan-out (A01–A10) with a verifier pass. Ten parallel subagents, one per category, return evidence-backed findings; a verification pass confirms or rejects each before a per-category breakdown tabl… |
| [`security-metrics`](security-metrics/SKILL.md) | `enforcement` | Deterministic 0–100 OWASP Top 10 scorecard with Markdown evidence table. |

### `ship` (4)

| Skill | α | Description |
|---|---|---|
| [`babysit-pr`](babysit-pr/SKILL.md) | `state` | 0-arg PR babysitter. Polls `gh pr checks`, fetches failing run logs, applies minimal fixes, commits + pushes, and re-iterates until review verdict = Approve and all required checks pass. Hard cap on iterations to preven… |
| [`babysit-pr-local`](babysit-pr-local/SKILL.md) | `state` | 0-arg local-mode PR babysitter. Pre-push pytest gate + local LLM judge verdict loop; replaces `gh pr checks --watch` with `bin/review-local.sh`. Additive sibling of `babysit-pr`. |
| [`bump`](bump/SKILL.md) | `state` | Explicit version bump of `.claude-plugin/plugin.json` + push of `chore/bump-vX.Y.Z`. Mirrors the auto-bump in `.github/workflows/version-bump.yml` but user-triggered for race recovery and pre-PR explicit bumps. |
| [`ship`](ship/SKILL.md) | `state` | 0-arg. Release tag emit. Gate check only (hooks auto). Requires Review verdict=Approve + main-block pass. |

### `shortcuts` (3)

| Skill | α | Description |
|---|---|---|
| [`codex-cache-update`](codex-cache-update/SKILL.md) | `analysis` | Refresh the dev-kit Codex marketplace checkout and synchronize the versioned plugin cache. Use when Codex reports the marketplace is current but the installed cache may be stale, or after a dev-kit merge. |
| [`llm-refresh`](llm-refresh/SKILL.md) | `analysis` | Refresh docs/llm-info/<provider>.json from each vendor's official pricing page via WebFetch extraction. Diff-then-commit; manual like set-provider.sh. |
| [`log`](log/SKILL.md) | `state` | Toggle /log setup|on|off|status — install/remove loghooks from ~/dev/loghooks into the current project's Claude/Codex settings. |

### `status` (1)

| Skill | α | Description |
|---|---|---|
| [`status`](status/SKILL.md) | `state` | HOTL visualization. Current loop progress + cumulative cycles + hand-off chain + eval score on one screen. |

## Alphabetical

| # | Skill | Category | α | Invocable |
|---|---|---|---|---|
| 1 | [`babysit-pr`](babysit-pr/SKILL.md) | `ship` | `state` | human |
| 1a | [`babysit-pr-local`](babysit-pr-local/SKILL.md) | `ship` | `state` | human |
| 2 | [`bootstrap`](bootstrap/SKILL.md) | `bootstrap` | `state` | human |
| 3 | [`build`](build/SKILL.md) | `build` | `state` | human |
| 4 | [`build-debug`](build-debug/SKILL.md) | `build` | `enforcement` | model |
| 5 | [`build-refactor`](build-refactor/SKILL.md) | `build` | `enforcement` | model |
| 6 | [`build-tdd`](build-tdd/SKILL.md) | `build` | `enforcement` | model |
| 7 | [`build-verify`](build-verify/SKILL.md) | `build` | `enforcement` | model |
| 8 | [`bump`](bump/SKILL.md) | `ship` | `state` | human |
| 9 | [`ci-doctor`](ci-doctor/SKILL.md) | `audit` | `enforcement` | human |
| 10 | [`ci-setup`](ci-setup/SKILL.md) | `bootstrap` | `enforcement` | human |
| 11 | [`ci-triage`](ci-triage/SKILL.md) | `audit` | `enforcement` | human |
| 12 | [`code-viz`](code-viz/SKILL.md) | `audit` | `state` | human |
| 13 | [`codex-cache-update`](codex-cache-update/SKILL.md) | `shortcuts` | `analysis` | human |
| 14 | [`config`](config/SKILL.md) | `config` | `state` | human |
| 15 | [`cost-gate`](cost-gate/SKILL.md) | `audit` | `enforcement` | human |
| 16 | [`docs-maintenance`](docs-maintenance/SKILL.md) | `audit` | `analysis` | human |
| 17 | [`evaluate`](evaluate/SKILL.md) | `eval` | `enforcement` | human |
| 18 | [`hook-doctor`](hook-doctor/SKILL.md) | `audit` | `enforcement` | model |
| 19 | [`inspect`](inspect/SKILL.md) | `audit` | `analysis` | human |
| 20 | [`interview`](interview/SKILL.md) | `design` | `enforcement` | human |
| 21 | [`linear`](linear/SKILL.md) | `config` | `state` | human |
| 22 | [`llm-refresh`](llm-refresh/SKILL.md) | `shortcuts` | `analysis` | human |
| 23 | [`log`](log/SKILL.md) | `shortcuts` | `state` | human |
| 24 | [`plan`](plan/SKILL.md) | `plan` | `state` | human |
| 25 | [`proposal`](proposal/SKILL.md) | `design` | `state` | human |
| 26 | [`prune`](prune/SKILL.md) | `build` | `analysis` | human |
| 27 | [`prune-propose`](prune-propose/SKILL.md) | `audit` | `state` | human |
| 28 | [`refactor`](refactor/SKILL.md) | `build` | `analysis` | human |
| 29 | [`research`](research/SKILL.md) | `design` | `enforcement` | human |
| 30 | [`research-plan-build`](research-plan-build/SKILL.md) | `build` | `state` | human |
| 31 | [`review`](review/SKILL.md) | `review` | `analysis` | human |
| 32 | [`security`](security/SKILL.md) | `security` | `enforcement` | human |
| 32a | [`security-metrics`](security-metrics/SKILL.md) | `security` | `enforcement` | human |
| 33 | [`ship`](ship/SKILL.md) | `ship` | `state` | human |
| 34 | [`sot-harness-writer`](sot-harness-writer/SKILL.md) | `design` | `state` | human |
| 35 | [`status`](status/SKILL.md) | `status` | `state` | human |
| 36 | [`token-analyzer`](token-analyzer/SKILL.md) | `audit` | `analysis` | human |
| 37 | [`valuate`](valuate/SKILL.md) | `design` | `enforcement` | model |
