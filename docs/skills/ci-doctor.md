> [← Skills index](README.md) · [Project README](../../README.md)

# `ci-doctor`

**Category:** `audit` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:ci-doctor` (human-invoked)

`ci-doctor` is a read-only CI readiness audit that answers, in a single call, "would CI succeed on my next PR?" It was added for issue #212-D1: a consumer who just ran `/dev-kit:bootstrap (with ci-setup prompt)` had no way to check CI readiness until the next PR turned red. The skill prints one flat PASS/FAIL summary across workflow files, the build marker, the provider file, required secrets, and `gh` auth state.

## When to use it

- The user types `/dev-kit:ci-doctor`.
- The user asks "is my CI set up correctly?" or "would the next PR be green?"
- The user just ran `/dev-kit:ci-setup` or `/dev-kit:bootstrap (with ci-setup prompt)` and wants to verify readiness.
- The user wants a pre-PR sanity check before opening the first dev-kit PR.

## How it works

The skill is 0-arg (optionally `--target DIR`, default `$PWD`) and delegates entirely to `lib/ci_doctor.py:audit(target_dir)`, then prints `DoctorReport.summary_lines()`. It deterministically checks and reports PASS/FAIL on:

| Check | Why |
|---|---|
| `file present: .github/workflows/{ci,review,auto-fix-pr}.yml` | All three runners must land; any missing means no CI. |
| `provider declared` | `CI_REVIEW_PROVIDER` is resolved from process env → `.env` → `.env.example`; the report names the source. |
| `file present: .dev-kit/ci-config.json` | This is the build pre-flight marker; `/dev-kit:build` refuses to start without it. |
| `marker parseable` / `marker non-empty` / `marker records provider key` | Round-trip JSON check; the marker payload must record `provider_env_key: CI_REVIEW_PROVIDER`. |
| `gh auth` | `gh secret list` needs `gh auth status` to pass. |
| `secret set: DEV_KIT_GITHUB_TOKEN` | Consumer-install precondition (issue #212-B1). |
| `secret set: <provider-API-key>` | Provider-matching secret (issue #212-B2); default `minimax` ⇒ `MINIMAX_API_KEY`. |
| `workflow triggers` / `fork-PR secret gap` / `concurrency:` / `branch policy` | Root-cause diagnostics — always WARN or INFO, never FAIL; they never flip the verdict. |
| `open PR mergeable` / `open PR draft` / `open PR title` / `open PR state` | Issue #249: surfaces when the open PR's state would silently skip CI. |

### Gates.json consistency check (issue #834)

When `.dev-kit/gates.json` is present, the audit runs `_check_gates_consistency` (`lib/ci_doctor.py:check_gates_consistency`) to surface drift between the committed SSOT and the live `gh variable get GATES_<NAME>_ENABLED` state. One row per gate emits PASS / WARN / SKIP — drift is WARN, never FAIL, because the operator may intentionally have a pending `gate-select sync` between local commit and `gh variable set`. SKIP fires when `gh` is absent / unauthenticated or the target repo can't be resolved. This check is the post-PR #834 follow-up to the maintenance gate's `if:`-uniqueness verification; a consumer who edits `gates.json` but forgets to `sync` now sees a WARN instead of a silently-stale CI run.

Every FAIL row prints the exact remediation command (e.g. `gh secret set NAME --repo OWNER/REPO`), so the workflow is audit → paste commands → re-audit, instead of push PR → CI red → read log → grep for the secret name.

### Workflow diagnostics (WARN/INFO only, verdict-neutral)

For each shipped `.github/workflows/*.yml`, the audit hand-parses the YAML (stdlib only, no PyYAML) and emits diagnostic rows explaining *why* a PR might not get reviewed. Unparseable YAML always emits INFO, never FAIL.

| Diagnostic | State | Why |
|---|---|---|
| `workflow triggers: <file>` | WARN if no `pull_request*` / `workflow_run`; PASS otherwise | Missing trigger means review won't run on PRs. |
| `fork-PR secret gap: <file>` | PASS if `pull_request_target`/`workflow_run`, or `pull_request`-only with a same-repo fork guard (`head.repo.full_name == github.repository`); INFO in source-repo mode; WARN only for `pull_request`-only with no guard in a consumer repo | Fork PRs lose repo secrets under bare `pull_request`; a same-repo guard avoids the OIDC-401 that `pull_request_target` causes without org trust. |
| `paths filter: <file>` / `branches filter: <file>` | INFO when present | Lets the user verify the filter includes their changes. |
| `concurrency: <file>` | WARN if `cancel-in-progress: true`; PASS otherwise | Mid-run cancellation can drop a long review verdict. |
| `job if: <file>/<job>` | INFO, verbatim `if:` string | Lets the user audit why a job may be skipped. |
| `job name: <file>/<job>` | INFO (`review.yml`) / WARN (`auto-fix-pr.yml`) when missing | Surfaces bare keys vs named jobs in the GitHub UI; matters for branch-protection matching. |
| `action ref mutable: <file>` | INFO, lists non-SHA third-party `uses:` refs | Supply-chain hardening signal. |
| `branch policy` | WARN on required-status mismatch; SKIP if `gh` absent/unauth/no repo context; INFO in source-repo mode | Compares GitHub branch-protection required checks against workflow job `name:`s. |

WARN rows are counted in `warnings: N` and shown on screen but never flip the verdict; INFO rows are advisory and never counted.

### Source-repo mode

When the target is the dev-kit plugin's own authoring source — detected by a `.claude-plugin/plugin.json` whose `name` is `dev-kit` — consumer-install-only checks report as SKIP instead of FAIL: `file present: .dev-kit/ci-config.json`, `marker parseable` (+ payload rows), and `secret set: DEV_KIT_GITHUB_TOKEN` (the source repo's CI uses the default `GITHUB_TOKEN`, per `lib/ci_setup.py:DEV_KIT_CONSUMER_SECRET`). The provider API-key secret is still required. An `INFO` row (`repo role: dev-kit source repo`) flags the mode, so a correctly-configured source repo audits PASS instead of a spurious 3-FAIL.

### Open PR state (issue #249)

A PR opened `mergeable: CONFLICTING` causes GitHub Actions to silently refuse all workflows (`gh pr checks <N>` returns "no checks reported" with no error). The audit calls `gh pr view <branch> --json mergeable,mergeStateStatus,isDraft,title` and reports:

| Row | State | Meaning |
|---|---|---|
| `open PR mergeable` | FAIL | Merge conflicts with main — CI will not run; fetch and merge `origin/main`. |
| `open PR mergeable` | WARN | GitHub still computing (UNKNOWN) — re-run in 30s. |
| `open PR mergeable` | PASS | No conflicts. |
| `open PR draft` | INFO | Draft PR — required checks gated until marked ready-for-review. |
| `open PR title` | INFO | Title starts with `chore(release): bump dev-kit to v` — ci/review/security skip by design. |
| `open PR state` | SKIP | `gh` absent, no PR open for the branch, detached HEAD, or JSON parse error. |

`CONFLICTING` is the only state that flips the verdict to FAIL. The 8 tests in `tests/test_ci_doctor.py::TestOpenPrState` pin every branch.

### Body / execution order

1. Parse `--target DIR` (default `$PWD`).
2. Delegate to `lib/ci_doctor.py:audit(target_dir)`.
3. Print `DoctorReport.summary_lines()`; the verdict line is `ci-doctor verdict: PASS` or `ci-doctor verdict: FAIL`.
4. Exit code 0 on PASS, 1 on FAIL. SKIP rows never flip the verdict — no local `gh` means an honest "can't verify," not "broken."

When a FAIL row is present, the skill also prints a one-line next step pointing at `/dev-kit:ci-setup --force` for re-install, or the specific `gh secret set NAME --repo OWNER/REPO` command for a missing secret.

## Usage

```bash
/dev-kit:ci-doctor [--target DIR]
```

## Output

A flat list of PASS/FAIL/WARN/INFO/SKIP rows followed by a `warnings: N` count and a final verdict line (`ci-doctor verdict: PASS` or `ci-doctor verdict: FAIL`). Exit code mirrors the verdict (0 = PASS, 1 = FAIL).

## Related

- [status](status.md) — for broader HOTL loop/stage visualization, as distinct from this CI-specific check.
- `lib/ci_doctor.py` — the audit engine, pure stdlib, no external deps; re-exported as the `ci-doctor` symlink by `/dev-kit:ci-setup --force`.
- `tests/test_ci_doctor.py::TestOpenPrState` — pins the 8 open-PR-state branches.
- `lib/ci_setup.py:DEV_KIT_CONSUMER_SECRET` — the consumer-vs-source-repo secret name distinction.
- `templates/ci/.github/workflows/ci.yml` — source of the release-bump title skip rule.

---
*Source: [`skills/ci-doctor/SKILL.md`](../../skills/ci-doctor/SKILL.md)*
