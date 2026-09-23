# Skills index

This index lists every skill shipped by the `dev-kit` plugin. Click into any skill to read its full `SKILL.md`; every `SKILL.md` has a back-link at the top to return here.

**51 skills** across 13 categories (49 human-invocable, 2 model-invoked). The full path of each entry is `skills/<dir>/SKILL.md`. Use `find skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l` to confirm.

## By category

### `audit` (11)

| Skill | α | Description |
|---|---|---|
| [`ci-doctor`](ci-doctor/SKILL.md) | `enforcement` | Read-only CI readiness audit. Prints one PASS/FAIL summary across files, marker, provider file, secrets, and gh auth. Hand-off answer to "would CI succeed on my next PR?" |
| [`ci-triage`](ci-triage/SKILL.md) | `enforcement` | Triage failing GitHub Actions runs across recent commits, dedupe against a persisted case store, judge new failures against a model/context/harness taxonomy with a required repro + regression test, and record them witho… |
| [`code-viz`](code-viz/SKILL.md) | `state` | 0-arg generic plugin-architecture visualizer. Walks any target repo, emits self-contained HTML with multi-level views (architecture / code / skill / hook / tools-lib / external) + domain pillar map (DB · Cloud · API · M… |
| [`cost-gate`](cost-gate/SKILL.md) | `enforcement` | 0-arg cost-gate status. Prints current session spend, threshold distance, and a two-line git-trailer block to include in commits so the PR-level cost flag can aggregate. |
| [`docs-maintenance`](docs-maintenance/SKILL.md) | `analysis` | Audit repository documentation with the project README as the highest-priority document. The README is always audited AND verified every run, and updated when needed. |
| [`hook-doctor`](hook-doctor/SKILL.md) 🔒 | `enforcement` | Diagnose failed Claude Code or Codex hooks, repair safe cache and registration drift, and report the exact restart step. |
| [`inspect`](inspect/SKILL.md) | `analysis` | 0-arg read-only code health audit. 8-dim fan-out (dead, dup, smell, overeng, overarch, cleancode, tokenbudget, slop) + --secrets/--slop aliases to the audit family (lib/analysis_core/dimensions.py). |
| [`maintenance`](maintenance/SKILL.md) | `enforcement` | Code-sanity gate. Judges the PR diff against the 20-checkbox rubric from eval/prompts/judge-code-sanity.md (CC-1..8 clean code, OE-1..8 over-engineering, VM-1..4 value/meaning). Mirrors .github/workflows/maintenance.yml… |
| [`prune-propose`](prune-propose/SKILL.md) | `state` | 0-arg skill — usage telemetry dump + per-skill delete proposal. User approves each deletion explicitly. |
| [`ralph`](ralph/SKILL.md) | `state` | End-to-end autonomous loop with 4 user gates + unattended build/babysit/ship. |
| [`token-analyzer`](token-analyzer/SKILL.md) | `analysis` | 0-arg token-efficiency dashboard. Runs tools/token_efficiency_analyzer.py over logs/{claude-code,codex}/*.jsonl to produce an HTML report (+ lazy per-worktree transcript sidecars) -- 4-dim session scoring, 6 anti-patter… |

### `bootstrap` (3)

| Skill | α | Description |
|---|---|---|
| [`bootstrap`](bootstrap/SKILL.md) | `state` | 0-arg setup for CLAUDE.md, AGENTS.md, hooks, and optional CI. |
| [`ci-setup`](ci-setup/SKILL.md) | `enforcement` | Install dev-kit's reusable CI workflow templates into a target project. Idempotent via `.dev-kit/ci-config.json` presence, no version gate. Hand-off to /dev-kit:build. |
| [`ci-update`](ci-update/SKILL.md) | `state` | Detect + selectively apply drift between installed CI templates and current dev-kit source. 4-state per-file classification with backup-before-overwrite. |

### `build` (4)

| Skill | α | Description |
|---|---|---|
| [`build`](build/SKILL.md) | `state` | 0-arg. Per-step sub-agent delegation + self-fix loop (MUST-36~38). Uses harness-runner engine. TDD + verify + debug integrated. |
| [`build-debug`](build-debug/SKILL.md) | `enforcement` | 4-phase systematic debugging. No fix proposal before Phase 1 (reproduce) completes (MUST-L2). Root-cause-first Iron Law. Standalone invocation hands the root cause to /dev-kit:plan instead of fixing inline. |
| [`prune`](prune/SKILL.md) | `analysis` | 0-arg slop-removal chain. One slash wraps inspect → 3-pass delete sweep → review. Gated phases for deleting AI slop and dead features (not refactoring). |
| [`refactor`](refactor/SKILL.md) | `analysis` | 0-arg cleanup chain. One slash wraps inspect -> cleanup -> review. 3 gated phases with quoted exit codes between each. |

### `config` (7)

| Skill | α | Description |
|---|---|---|
| [`config`](config/SKILL.md) | `state` | skill + hook + methodology picker (multiSelect). |
| [`gate-select`](gate-select/SKILL.md) | `state` | Unified 3-dimension picker for project / session / AI-judge gates. Reads .dev-kit/gates.json + .dev-kit/ci-config.json + .dev-kit/harness-mode.session.json and dispatches writes through `python -m lib.gates_state ...`. |
| [`guard-mode`](guard-mode/SKILL.md) | `state` | Session-scoped on/off toggle for the tdd-guard, worktree-guard, and git-guard hard-block hooks. |
| [`harness-mode`](harness-mode/SKILL.md) | `state` | Session-scoped local-hook mode picker — fast (all optional local hooks off), full (default, all on), or custom (interactive per-local-hook picker via AskUserQuestion). |
| [`linear`](linear/SKILL.md) | `state` | Optional Linear task tracker. Reconcile the current repository task with a canonical project and non-duplicate issue. Auto-syncs on every Claude Code edit when configured. Owner-gated auto-triggers also fire on worktree… |
| [`sync-version`](sync-version/SKILL.md) | `state` | DEPRECATED. The GitHub Merge Queue now owns version sync at merge time; this skill is a no-op wrapper around bin/sync-version.sh that preserves the CLI surface for callers that haven't migrated yet. See docs/proposals/r… |
| [`team`](team/SKILL.md) | `state` | Read or write the team collaboration toggle (DEV_KIT_TEAM on|off). Default OFF, independent of DEV_KIT_MODE. When ON, team roles/dependency-aware planning is enabled and .dev-kit/ stays tracked in git. |

### `design` (7)

| Skill | α | Description |
|---|---|---|
| [`evidence-plan`](evidence-plan/SKILL.md) | `state` | Idea → cited research → HTML proposal (human confirms) → /dev-kit:plan hand-off. The proposal is rendered and reviewed BEFORE the expensive 5-gate PRD work runs, not after. |
| [`interview`](interview/SKILL.md) | `enforcement` | 5-field safety-contract interview that gates plan emission. Drives `lib.interview_engine` through one Ralph loop, enforces `safety_valve=8`, `narrowed_delta`, `dedup_metric` (identical-ambiguity-cycle=2), and `user_inte… |
| [`proposal`](proposal/SKILL.md) | `state` | 0-arg YAML-to-HTML renderer for reviewable design proposals. |
| [`proposal-orch-issue-pr`](proposal-orch-issue-pr/SKILL.md) | `state` | 0-arg orchestrator-first GitHub backlog triage. Gathers open PRs + issues, scores (bottleneck / risk / change containment), orders by orchestrator critical path, and writes a proposal YAML + HTML via the existing `/dev-… |
| [`research`](research/SKILL.md) | `enforcement` | 0-arg research gate. Run Phase 0-3 escalation (cache / direct / multi / human) + verify() + enforce_citations(). /dev-kit:research <claim> [--max-phase N]. |
| [`sot-harness-writer`](sot-harness-writer/SKILL.md) | `state` | Interview-based Single Source of Truth harness document writer (5 rounds × 2-3 evidence-backed recommendations, full traceability, hands off to /dev-kit:plan). |

### `eval` (2)

| Skill | α | Description |
|---|---|---|
| [`evaluate`](evaluate/SKILL.md) | `enforcement` | 0-arg eval extension. Replays transcripts and consumes workflow evidence against registered rubrics, preserving legacy Agent Behavior D1–D7 and reporting four harness-effectiveness components plus the nested measurement… |
| [`harness-effectiveness`](harness-effectiveness/SKILL.md) | `enforcement` | 0-arg harness-effectiveness report. Wraps `lib.harness_effectiveness.build_report` and prints the four-component (prevention / first-pass / recovery / measurement-integrity) scorecard as JSON + a one-line sta… |

### `mode` (1)

| Skill | α | Description |
|---|---|---|
| [`mode`](mode/SKILL.md) | `state` | Read or write the active DEV_KIT_MODE (full | lite | undev) for the current project. Picker by default; --show to display current mode; --scope=local to write to .claude/settings.local.json instead of .claude/settings.j… |

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
| [`security`](security/SKILL.md) | `enforcement` | Security fan-out — OWASP Top 10 2025 (A01–A10) plus a separate LLM01 Prompt Injection dimension. Eleven parallel subagents, one per category, return evidence-backed findings; a verification pass confirms or rejects each… |
| [`security-metrics`](security-metrics/SKILL.md) | `enforcement` | Calculate a deterministic 0-100 security scorecard for the current repository and render an evidence-backed Markdown table for OWASP Top 10 categories. |

### `ship` (6)

| Skill | α | Description |
|---|---|---|
| [`babysit-pr`](babysit-pr/SKILL.md) | `state` | 0-arg restart-safe PR check, diagnose, fix, and hand-off loop. |
| [`babysit-pr-local`](babysit-pr-local/SKILL.md) | `state` | 0-arg local-mode PR babysitter. Pre-push pytest gate + local LLM judge verdict loop; replaces `gh pr checks --watch` with `bin/review-local.sh`. |
| [`bump`](bump/SKILL.md) | `state` | Explicit version bump of `.claude-plugin/plugin.json` + push of `chore/bump-vX.Y.Z`. Mirrors the auto-bump in `.github/workflows/version-bump.yml` but user-triggered for race recovery and pre-PR explicit bumps. For catc… |
| [`pr-verify`](pr-verify/SKILL.md) | `enforcement` | Deterministic PR verification — fresh `gh pr view` + check + comment fetches on every call. Catches the "stale CI / LLM-judge still in progress" false positive the babysit flow had. |
| [`review-local`](review-local/SKILL.md) | `state` | Local equivalent of the GH-Actions review workflow. Runs /dev-kit:review + /dev-kit:security + /dev-kit:maintenance (via local `claude` CLI) with the same verdict extraction + combined gate + L3-evidence enforcement + o… |
| [`ship`](ship/SKILL.md) | `state` | 0-arg. Release tag emit. Gate check only (hooks auto). Requires Review verdict=Approve + main-block pass. |

### `shortcuts` (5)

| Skill | α | Description |
|---|---|---|
| [`codex-cache-update`](codex-cache-update/SKILL.md) | `analysis` | Refresh the dev-kit Codex marketplace checkout and synchronize the versioned plugin cache. Use when Codex reports the marketplace is current but the installed cache may be stale, or after a dev-kit merge. |
| [`llm-refresh`](llm-refresh/SKILL.md) | `analysis` | Refresh docs/llm-info/<provider>.json from each vendor's official pricing page via WebFetch extraction. Diff-then-commit; manual like set-provider.sh. |
| [`log`](log/SKILL.md) | `state` | Toggle /log setup|on|off|status — install/remove loghooks from ~/dev/loghooks into the current project's Claude/Codex settings. |
| [`skill-usage`](skill-usage/SKILL.md) | `analysis` | Run the skill usage telemetry CLI and inspect turns, invocations, and per-project usage. |
| [`worktree-prune`](worktree-prune/SKILL.md) | `state` | 0-arg interactive prune of stale worktrees. Counts registered worktrees, lists them oldest-first by branch-tip age, asks how many to remove, then dispatches `bin/worktree-remove-safe.sh` per row after a y/N gate. |

### `status` (1)

| Skill | α | Description |
|---|---|---|
| [`status`](status/SKILL.md) | `state` | HOTL visualization. Current loop progress + cumulative cycles + hand-off chain + eval score on one screen. |

## Alphabetical

| # | Skill | Category | α | Invocable |
|---|---|---|---|---|
| 1 | [`babysit-pr`](babysit-pr/SKILL.md) | `ship` | `state` | human |
| 2 | [`babysit-pr-local`](babysit-pr-local/SKILL.md) | `ship` | `state` | human |
| 3 | [`bootstrap`](bootstrap/SKILL.md) | `bootstrap` | `state` | human |
| 4 | [`build`](build/SKILL.md) | `build` | `state` | human |
| 5 | [`build-debug`](build-debug/SKILL.md) | `build` | `enforcement` | human |
| 6 | [`bump`](bump/SKILL.md) | `ship` | `state` | human |
| 7 | [`ci-doctor`](ci-doctor/SKILL.md) | `audit` | `enforcement` | human |
| 8 | [`ci-setup`](ci-setup/SKILL.md) | `bootstrap` | `enforcement` | human |
| 9 | [`ci-triage`](ci-triage/SKILL.md) | `audit` | `enforcement` | human |
| 10 | [`ci-update`](ci-update/SKILL.md) | `bootstrap` | `state` | human |
| 11 | [`code-viz`](code-viz/SKILL.md) | `audit` | `state` | human |
| 12 | [`codex-cache-update`](codex-cache-update/SKILL.md) | `shortcuts` | `analysis` | human |
| 13 | [`config`](config/SKILL.md) | `config` | `state` | human |
| 14 | [`cost-gate`](cost-gate/SKILL.md) | `audit` | `enforcement` | human |
| 15 | [`docs-maintenance`](docs-maintenance/SKILL.md) | `audit` | `analysis` | human |
| 16 | [`evaluate`](evaluate/SKILL.md) | `eval` | `enforcement` | human |
| 17 | [`evidence-plan`](evidence-plan/SKILL.md) | `design` | `state` | human |
| 18 | [`gate-select`](gate-select/SKILL.md) | `config` | `state` | human |
| 19 | [`guard-mode`](guard-mode/SKILL.md) | `config` | `state` | human |
| 20 | [`harness-effectiveness`](harness-effectiveness/SKILL.md) | `eval` | `enforcement` | human |
| 21 | [`harness-mode`](harness-mode/SKILL.md) | `config` | `state` | human |
| 22 | [`hook-doctor`](hook-doctor/SKILL.md) | `audit` | `enforcement` | model |
| 23 | [`inspect`](inspect/SKILL.md) | `audit` | `analysis` | human |
| 24 | [`interview`](interview/SKILL.md) | `design` | `enforcement` | human |
| 25 | [`linear`](linear/SKILL.md) | `config` | `state` | human |
| 26 | [`llm-refresh`](llm-refresh/SKILL.md) | `shortcuts` | `analysis` | human |
| 27 | [`log`](log/SKILL.md) | `shortcuts` | `state` | human |
| 28 | [`maintenance`](maintenance/SKILL.md) | `audit` | `enforcement` | human |
| 29 | [`mode`](mode/SKILL.md) | `mode` | `state` | human |
| 30 | [`plan`](plan/SKILL.md) | `plan` | `state` | human |
| 31 | [`pr-verify`](pr-verify/SKILL.md) | `ship` | `enforcement` | human |
| 32 | [`proposal`](proposal/SKILL.md) | `design` | `state` | human |
| 33 | [`proposal-orch-issue-pr`](proposal-orch-issue-pr/SKILL.md) | `design` | `state` | human |
| 34 | [`prune`](prune/SKILL.md) | `build` | `analysis` | human |
| 35 | [`prune-propose`](prune-propose/SKILL.md) | `audit` | `state` | human |
| 36 | [`ralph`](ralph/SKILL.md) | `audit` | `state` | human |
| 37 | [`refactor`](refactor/SKILL.md) | `build` | `analysis` | human |
| 38 | [`research`](research/SKILL.md) | `design` | `enforcement` | human |
| 39 | [`review`](review/SKILL.md) | `review` | `analysis` | human |
| 40 | [`review-local`](review-local/SKILL.md) | `ship` | `state` | human |
| 41 | [`security`](security/SKILL.md) | `security` | `enforcement` | human |
| 42 | [`security-metrics`](security-metrics/SKILL.md) | `security` | `enforcement` | human |
| 43 | [`ship`](ship/SKILL.md) | `ship` | `state` | human |
| 44 | [`skill-usage`](skill-usage/SKILL.md) | `shortcuts` | `analysis` | human |
| 45 | [`sot-harness-writer`](sot-harness-writer/SKILL.md) | `design` | `state` | human |
| 46 | [`status`](status/SKILL.md) | `status` | `state` | human |
| 47 | [`sync-version`](sync-version/SKILL.md) | `config` | `state` | human |
| 48 | [`team`](team/SKILL.md) | `config` | `state` | human |
| 49 | [`token-analyzer`](token-analyzer/SKILL.md) | `audit` | `analysis` | human |
| 50 | [`worktree-prune`](worktree-prune/SKILL.md) | `shortcuts` | `state` | human |

