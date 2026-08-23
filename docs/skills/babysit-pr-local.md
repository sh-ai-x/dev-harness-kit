> [← Skills index](README.md) · [Project README](../../README.md)

# `babysit-pr-local`

**Category:** `ship` · **Alpha:** `state` · **Invocation:** `/dev-kit:babysit-pr-local` (human-invoked)

Local-mode PR babysitter. Same algorithm as `/dev-kit:babysit-pr`, but the LLM-judge verdict loop (`/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance`) runs locally via `bin/review-local.sh` instead of waiting on GH-Actions. Pre-push pytest gate is always on. Saves GH-Actions minutes when private repos hit the budget cap.

## When to use it

- GH-Actions minutes are exhausted on the active PR but the reviewer bot is still needed.
- The operator wants a faster local feedback loop than waiting on `gh pr checks --watch`.
- The operator is iterating on a provider switch (`bin/set-provider.sh <provider>`) before pushing.

## How it works

`bin/babysit-pr-local.sh <PR>` replaces `gh pr checks --watch` with `bin/review-local.sh`:

1. **Pre-push pytest gate** (always on, even when `--local-verify` is off on the parent skill) — run `pytest -q` inside the worktree before any commit.
2. **Local LLM-judge verdict loop** — invoke `bin/review-local.sh` to run `/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance` via local `claude -p`, with the same verdict extraction + combined gate + L3-evidence check as `.github/workflows/review.yml`.
3. **Optional `--auto-approve`** — cast `gh pr review --approve` when the combined verdict is Approve; merging is always a human action.
4. **Step diff vs `/dev-kit:babysit-pr`** — local mode skips step 4 (`WAIT`), step 5 (`FETCH LOGS`), step 12 (`SLEEP`); it replaces the GH-Actions wait with a local `bin/review-local.sh` invocation.

## Usage

```bash
# Local babysit on the current branch's PR
/dev-kit:babysit-pr-local

# Or via the binary
bin/babysit-pr-local.sh <PR>

# Skip the local pytest gate (rare; default is on)
bin/babysit-pr-local.sh <PR> --no-pytest-gate

# Auto-approve when verdict = Approve
bin/babysit-pr-local.sh <PR> --auto-approve
```

## When NOT to use

- The branch's PR requires a reviewer bot or org-level MCP that's only available via `anthropics/claude-code-action`. Local `claude -p` does not have access to the same MCP servers.
- The PR requires `gh pr merge` — merging is always a human action.

## CI infrastructure failures

Local mode runs the LLM-judge verdict loop locally, but the GitHub-side check rollup still depends on the same workflows as `babysit-pr` (`.github/workflows/maintenance.yml`, `review.yml`). When a check fails for an *infrastructure* reason — e.g. `anthropics/claude-code-action` OIDC token exchange returning `401 Unauthorized` on `pull_request_target` — local pytest + local `bin/review-local.sh` both pass, yet the GH-Actions check stays red. In that case:

1. Confirm the failing run's log is a token/auth error, not a code review issue (run `gh run view <run-id> --log-failed`).
2. Re-dispatch via `gh workflow run maintenance.yml --ref <branch> -f pr_number=<N>` — `workflow_dispatch` hits the `bin/ci-claude-p.sh` workaround path that bypasses OIDC, so the rerun typically succeeds where the original PRT run could not.
3. The dispatch run posts the verdict + audit comment; the next push (or another re-run) refreshes the rollup.

This keeps the loop moving without modifying the workflow to silence an OIDC failure (which would violate the no-workaround iron law).

## Related

- [`/dev-kit:babysit-pr`](babysit-pr.md) — the GH-Actions-mode sibling; `--local-verify` flag adds a local pytest gate without leaving the GH-Actions wait loop.
- [`bin/babysit-pr-local.sh`](../../bin/babysit-pr-local.sh) — single-call wrapper script (≈70 lines).
- [`bin/review-local.sh`](../../bin/review-local.sh) — local equivalent of the GH-Actions review workflow.
- [`bin/babysit-pr-local-status.py`](../../bin/babysit-pr-local-status.py) — read-only one-line ANSI summary of the active PR's gate state; consumed by Claude Code's statusLine, Codex's `[tui.status_line]`, and this skill's per-iteration tail.
- [`docs/local-ci.md`](../local-ci.md) — full local-CI playbook (when / when not / how).
- [`skills/babysit-pr-local/SKILL.md`](../../skills/babysit-pr-local/SKILL.md) — full algorithm body.
