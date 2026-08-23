---
name: ci-doctor
category: audit
description: Read-only CI readiness audit. Prints one PASS/FAIL summary across files, marker, provider file, secrets, and gh auth. Hand-off answer to "would CI succeed on my next PR?"
alpha: enforcement
when_to_use:
  - User types /dev-kit:ci-doctor
  - User asks "is my CI set up correctly?" / "would the next PR be green?"
  - After /dev-kit:ci-setup or /dev-kit:bootstrap + ci-setup to verify readiness
  - Pre-PR sanity check before opening the first dev-kit PR
allowed-tools: Read Glob Bash
disallowed-tools: Edit Write Agent WebFetch
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:ci-doctor — CI Readiness Audit

## Iron Law

**0-arg. Read-only. Never mutates target files; never writes secrets; never opens PRs.** Issue #212-D1: a consumer who just ran `/dev-kit:bootstrap + ci-setup` has no way to ask "is my CI ready?" until the next PR turns red. This skill answers that question in one call.

## What it does

Runs `lib/ci_doctor.py:audit()` against the current working directory (or `--target DIR`) and prints a flat PASS/FAIL summary across:

| Check | Why |
|---|---|
| `file present: .github/workflows/{ci,review,auto-fix-pr}.yml` | Three runners must land; missing any = no CI |
| `provider declared` | `CI_REVIEW_PROVIDER` resolved from process env → `.env` → `.env.example`; report shows which source supplied the value |
| `file present: .dev-kit/ci-config.json` | Build pre-flight marker (`/dev-kit:build` refuses to start without it) |
| `marker parseable` / `marker non-empty` / `marker records provider key` | Round-trip JSON, no zero-byte corruption; marker payload records the env key name (`provider_env_key: CI_REVIEW_PROVIDER`) |
| `gh auth` | `gh secret list` requires `gh auth status` to pass |
| `secret set: DEV_KIT_GITHUB_TOKEN` | Consumer-install precondition (issue #212-B1) |
| `secret set: <provider-API-key>` | Provider-matching secret (B2). Default `minimax` ⇒ `MINIMAX_API_KEY` |
| `workflow triggers` / `fork-PR secret gap` / `concurrency:` / `branch policy` | Root-cause diagnostics for *why* `ci github action review` might not run — always WARN or INFO, never FAIL, never flip the verdict (flexible review process) |
| `open PR mergeable` / `open PR draft` / `open PR title` / `open PR state` | Issue #249: surfaces when the open PR's state would silently skip CI (CONFLICTING → FAIL; UNKNOWN → WARN; isDraft / bump-title → INFO; gh absent or no PR open → SKIP) |
| `CI_REVIEW_PROVIDER consistency` | Issue #712: drift probe between local `.env:CI_REVIEW_PROVIDER` (gitignored, per-user) and the GitHub repo variable `vars.CI_REVIEW_PROVIDER` (per-repo, set via `gh variable set`). Same value on both sides → OK; exactly one set OR both set but differ → WARN with the diff and the `gh variable set CI_REVIEW_PROVIDER --body <value>` remediation; `gh` absent or not authenticated → SKIP (honest "can't verify"). Read-only, advisory, never flips the verdict. Engine: `lib/ci_setup.check_provider_consistency()`; pinned by `tests/test_check_provider_consistency.py`. |

Every FAIL row prints the exact remediation (`run: gh secret set NAME --repo OWNER/REPO`, etc.) so the discover path is `audit → paste commands → re-audit` rather than the current `push PR → CI red → read log → grep for the secret name`.

## Workflow diagnostics (WARN/INFO only — verdict-neutral)

For each `.github/workflows/*.yml` the install shipped, the audit emits diagnostic rows that surface the *reason* a PR might not get reviewed. Every diagnostic is hand-parsed from the workflow file (stdlib-only, no PyYAML). Unparseable YAML emits an INFO row, never a FAIL.

| Diagnostic | State | Why |
|---|---|---|
| `workflow triggers: <file>` | WARN if no `pull_request*` / `workflow_run`; PASS otherwise | Missing trigger ⇒ review won't run on PRs |
| `fork-PR secret gap: <file>` | PASS if `pull_request_target`/`workflow_run`, OR `pull_request`-only **with** a same-repo fork guard (`head.repo.full_name == github.repository`); INFO in source-repo mode; WARN only for `pull_request`-only + no guard in a consumer repo | Fork PRs lose repo secrets under bare `pull_request`, but a same-repo guard skips forks before any step so the consumer template stays on `pull_request` (avoids the OIDC-401 that `pull_request_target` causes without org trust) |
| `paths filter: <file>` / `branches filter: <file>` | INFO when present | User-visible so they can verify the filter includes their changes |
| `concurrency: <file>` | WARN if `cancel-in-progress: true`; PASS otherwise | Mid-run cancellation can drop a long review verdict |
| `job if: <file>/<job>` | INFO, verbatim `if:` string | User can audit why a job may be skipped |
| `job name: <file>/<job>` | INFO (`review.yml`) / WARN (`auto-fix-pr.yml`) when missing | Surfaces bare keys vs named jobs in the GitHub UI; matters for branch-protection matching |
| `action ref mutable: <file>` | INFO listing non-SHA third-party `uses:` refs | Supply-chain hardening signal |
| `branch policy` | WARN on required-status mismatch; SKIP if `gh` absent / unauth / no repo context; INFO in source-repo mode | Compares GitHub branch-protection required checks against workflow job `name:`s |

WARN rows appear in the count line (`warnings: N`) and on screen; they never flip the verdict. INFO rows are advisory and never counted — same contract as the existing `repo role: dev-kit source repo` row.

## Source-repo mode (consumer-only checks skipped)

When the target is the dev-kit plugin authoring source itself — detected by a `.claude-plugin/plugin.json` whose `name` is `dev-kit` — the consumer-install-only checks are reported as **SKIP**, not FAIL:

| Check | Why skipped in source repo |
|---|---|
| `file present: .dev-kit/ci-config.json` | The marker gates `/dev-kit:build` in consumers; the source repo gitignores `.dev-kit/` and never builds itself through it |
| `marker parseable` (+ payload rows) | No marker to parse |
| `secret set: DEV_KIT_GITHUB_TOKEN` | The PAT lets consumer CI read the source; the source repo's own CI uses the default `GITHUB_TOKEN` (see `lib/ci_setup.py:DEV_KIT_CONSUMER_SECRET`) |

The provider API-key secret (e.g. `DEEPSEEK_API_KEY`) is **still required** — the source repo's own `review.yml` uses it. An `INFO` row (`repo role: dev-kit source repo`) flags the mode. SKIP rows never flip the verdict, so a correctly-configured source repo audits as PASS instead of the spurious 3-FAIL it produced before.

## Open PR state (issue #249)

A PR opened in `mergeable: CONFLICTING` causes GitHub Actions to silently
refuse ALL workflows on the PR — `gh pr checks <N>` returns `no checks
reported` with no error. The audit calls `gh pr view <branch> --json
mergeable,mergeStateStatus,isDraft,title` and emits:

| Row | State | Meaning |
|---|---|---|
| `open PR mergeable` | FAIL | PR has merge conflicts with main — CI will not run. Run `git fetch origin main && git merge origin/main` |
| `open PR mergeable` | WARN | GitHub still computing (UNKNOWN) — re-run in 30s |
| `open PR mergeable` | PASS | no conflicts |
| `open PR draft` | INFO | draft PR — required checks gated until marked ready-for-review |
| `open PR title` | INFO | title starts with `chore(release): bump dev-kit to v` — ci/review/security skip by design (see `templates/ci/.github/workflows/ci.yml`) |
| `open PR state` | SKIP | `gh` absent, no PR open for the branch, detached HEAD, JSON parse error |

CONFLICTING is the only state that flips the verdict to FAIL. SKIP / WARN
/ INFO rows preserve the existing "verdict-neutral diagnostics" contract.
The check degrades the same way the rest of ci-doctor does: missing tool
→ SKIP, not FAIL. The 8 new tests in `tests/test_ci_doctor.py::TestOpenPrState`
pin every branch.

## Body

1. Parse `--target DIR` (default `$PWD`).
2. Delegate to `lib/ci_doctor.py:audit(target_dir)`.
3. Print `DoctorReport.summary_lines()`. The verdict line is `ci-doctor verdict: PASS` or `ci-doctor verdict: FAIL`.
4. Exit code: 0 on PASS, 1 on FAIL. SKIP rows never flip the verdict (no `gh` locally = honest "can't verify", not "broken").

When a FAIL row is present, also print a one-line "next step" pointing the user at `/dev-kit:ci-setup --force` for re-install or `gh secret set NAME --repo OWNER/REPO` for missing secrets.

## Rules

- **Read-only**: no Edit / Write / Agent tools. Even mutations to the user's `.env` are off-limits; the audit answers questions, it doesn't fix them.
- **Single hand-off**: succeeds → exit 0. Fails → exit 1 + remediation hints. No automated fixing.
- **No secrets in output**: secrets are read via `gh secret list` (returns names, not values). The audit NEVER prints a secret value, even when present.
- **`/dev-kit:bootstrap + ci-setup` should run this skill next**: it is the canonical post-install verification (issue #212-D1 / D2).

## Files installed

This skill ships:

| Path | Purpose |
|---|---|
| `skills/ci-doctor/SKILL.md` | This file |
| `lib/ci_doctor.py` | The audit engine — pure stdlib, no external deps. Re-exported as the `ci-doctor` symlink by `/dev-kit:ci-setup --force` (markers know about it but the templates tree is the source). |

## Iron Law (repeated, for emphasis)

**Read-only. Verdict only. No writes, no PRs, no secrets printed.**

## After this skill ships

This skill is bundled into the dev-kit plugin cache. After a merge:
- **Claude Code**: change is visible on the next session start.
- **Codex**: run `/dev-kit:codex-cache-update` (or reload with `claude --plugin-dir .`).
