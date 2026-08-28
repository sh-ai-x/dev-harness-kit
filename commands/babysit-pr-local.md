---
name: babysit-pr-local
description: Local-mode PR babysitter. Same algorithm as `/dev-kit:babysit-pr`, but the LLM-judge verdict loop (`/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance`) runs locally via `bin/review-local.sh` instead of waiting on GH-Actions. Pre-push pytest gate is always on. Saves GH-Actions minutes when private repos hit the budget cap.
argument-hint: ""
alpha: state
user-invocable: true
---

## Invocation

Operators run `/dev-kit:babysit-pr-local` with **no arguments**. The
slash description is the operator-facing UX; nothing else.

## What it does

Calls `bin/babysit-pr-local.sh $ARGUMENTS` from the worker's current
worktree. The script:

1. Validates the PR-number argument (refuses any `--auto-appearing`
   flag with exit 2 + stderr message; local mode never auto-merges).
2. Resolves the LLM provider from `.env:CI_REVIEW_PROVIDER` via
   `lib/ci_setup.read_provider`.
3. Exports the matching `ANTHROPIC_BASE_URL` + `ANTHROPIC_MODEL*`
   env-var block (mirrors `review.yml`); the `ANTHROPIC_API_KEY` is
   **scoped to the `claude -p` invocation only** (via the
   `env KEY=... claude -p ...` prefix) so it never enters the parent
   shell's persistent environment.
4. Runs `claude -p "/dev-kit:review --diff <repo>/pull/<N>"` +
   `/dev-kit:security` + `/dev-kit:maintenance` via the underlying
   `bin/review-local.sh --pr $PR_NUMBER` (NOT `--auto-approve`).
5. Returns the combined verdict as an exit code (0 = Approve,
   1 = Changes Requested / Blocked / unparseable).
6. Posts the `<!-- dev-kit-verdict-audit -->` audit comment
   (`source=bin_review_local`).

The skill body's §Algorithm loop calls this wrapper as step 4L.
The wrapper's exit code drives the loop's TERMINATE check (exit 0
→ done) vs the iterate branch (exit 1 → read audit, fix, re-push).

## When to use

- The repo has hit its GH-Actions minute cap on private plans.
- The operator wants to iterate on the local review verdict loop
  without waiting for GH-Actions to spin up.
- The operator is testing a provider switch (`bin/set-provider.sh`)
  and wants to see the verdict locally before pushing.

## When NOT to use

- The repo relies on a private reviewer bot or org-level MCP that's
  only available via `claude-code-action`. Local `claude -p` does
  not have access to the same MCP servers.
- The PR requires `gh pr merge` immediately — local mode is an
  iterative repair loop, not a one-shot review. Use
  `/dev-kit:review-local --pr N --auto-approve` for that.

## Differences vs `/dev-kit:babysit-pr` (sibling skill, unchanged)

| | `/dev-kit:babysit-pr` | `/dev-kit:babysit-pr-local` |
|---|---|---|
| Review verdict source | GH-Actions review workflow | Local `bin/review-local.sh` |
| Pre-push pytest gate | none (step 8 re-runs the failing check only) | always on |
| `gh pr checks --watch` | yes | no (replaced by `bin/review-local.sh --pr N`) |
| `--auto-approve` | n/a | forbidden (refused with exit 2) |
| Operator-visible flags | `--pr`, `--rationale`, `--operator-is-only-human` | none (hidden flags only) |
| `--local-mode` | n/a | always implied by the skill |

The two skills share `lib/babysit_pr_cli` helpers, the worktree-detect
plumbing, and the lock-file protocol (the lock path is shared; either
skill refuses to run while the other holds the lock).

## Execution

There is no manual execution — the parent skill body is the
orchestrator. The wrapper exists so tests + the recipe can invoke
the verdict pipeline deterministically without spawning a full
Claude session.

## Related

- `bin/babysit-pr-local.sh` — the implementation (≈30 lines).
- `bin/review-local.sh` — the verdict pipeline the wrapper delegates to
  (verbatim reuse).
- `commands/review-local.md` — the one-shot local review slash command.
- `skills/babysit-pr-local/SKILL.md` — the skill body (the algorithm loop
  that invokes the wrapper in §Algorithm step 4L).
- `skills/babysit-pr-local/recipes/canonical-wiring.md` — parent preflight
  + sub-agent prompt body.
- `lib/babysit_pr_cli.py` — `run_local_verify` (pre-push pytest gate),
  `is_local_mode` (parser routing), `parse_babysit_args` (hidden flags).
- `docs/local-ci.md` §5 — `/dev-kit:babysit-pr-local — local-mode babysit`.
