---
name: review-local
description: Local equivalent of the GH-Actions review workflow. Runs /dev-kit:review + /dev-kit:security + /dev-kit:maintenance (via local `claude` CLI) with the same verdict extraction + combined gate + L3-evidence enforcement + optional auto-approve as `.github/workflows/review.yml`. Saves Action minutes when private repos hit the GH-Actions budget cap.
argument-hint: --pr N [--provider minimax|anthropic|deepseek] [--auto-approve] [--review-only|--security-only|--maintenance-only] [--dry-run]
alpha: state
user-invocable: true
---

## Invocation

Arguments: `$ARGUMENTS` — pass `bin/review-local.sh` flags directly. The
script is the canonical implementation; this command is a thin wrapper
that keeps `commands/` inventory consistent with the other slash
commands.

## What it does

Calls `bin/review-local.sh $ARGUMENTS` from the worker's current
worktree. The script:

1. Resolves the LLM provider from `--provider` flag → `CI_REVIEW_PROVIDER`
   env var → `.env` (via `lib/ci_setup.read_provider`).
2. Exports the matching `ANTHROPIC_BASE_URL` + `ANTHROPIC_MODEL*`
   env-var block (mirrors `review.yml:120-131`). The `ANTHROPIC_API_KEY`
   is **scoped to the `claude -p` invocation only** (passed via the
   `env KEY=... claude -p ...` prefix) so it never enters the parent
   shell's persistent environment; subsequent `gh`/shell calls cannot
   leak it via `/proc/<pid>/environ` or core dumps.
3. Runs `claude -p "/dev-kit:review --diff <repo>/pull/<N>"` (and
   `/dev-kit:security`, `/dev-kit:maintenance` per the `--*-only` flags).
4. Captures each `claude -p "$prompt"` invocation's stdout into a
   per-skill variable, then extracts the verdict by piping that stdout
   to `python3 -m lib.maintenance_gate --extract-verdict-from-stdin`
   (the same helper the workflow shells out to). The agent still posts
   inline comments directly via `gh pr comment` for the human reviewer;
   the captured stdout is what feeds the gate.
5. Combines worst-of wins across the enabled judges.
6. Enforces the L3-evidence gate (`<N> passed in <Ns>s` regex on the PR
   body) when the PR touches production code.
7. Optionally casts `gh pr review --approve` when `--auto-approve` is
   passed AND the combined verdict is `Approve` AND the L3-evidence gate
   passes.
8. Posts the `<!-- dev-kit-verdict-audit -->` audit comment.

## Bump-PR skip

Mirrors `review.yml:75`: PRs whose title starts with
`chore(release): bump dev-kit to v` auto-pass without invoking the LLM
judge. The audit comment records the skip with
`source=bin_review_local (bump-PR skip)`.

## When to use

- The repo has hit its GH-Actions minute cap on private plans.
- The operator wants to iterate on the local review verdict loop
  without waiting for CI to spin up.
- The operator is testing a provider switch (`bin/set-provider.sh
  <provider>`) and wants to see the verdict locally before pushing.

## When NOT to use

- The repo relies on a private reviewer bot or org-level MCP that's
  only available via `claude-code-action`. Local `claude -p` does not
  have access to the same MCP servers.
- The PR requires `gh pr merge` — merging is a human action run
  outside this script (and outside `review.yml`'s auto-approve).

## Execution

```bash
# Dry-run (no LLM call, no PR mutation): preview env + planned commands.
bin/review-local.sh --pr 123 --dry-run

# Full review + auto-approve on clean verdict.
bin/review-local.sh --pr 123 --auto-approve

# Force a specific provider (overrides .env:CI_REVIEW_PROVIDER).
bin/review-local.sh --pr 123 --provider anthropic --auto-approve

# Run only /dev-kit:review (skip security + maintenance).
bin/review-local.sh --pr 123 --review-only
```

## Related

- `bin/review-local.sh` — the implementation.
- `skills/review/SKILL.md` — the review skill invoked via `claude -p`.
- `skills/security/SKILL.md` — the security skill.
- `lib/maintenance_gate.py` — verdict-extraction + combined-gate helper (the `/dev-kit:maintenance` slash is the LLM-judge prompt dispatched by `maintenance.yml`, not a `SKILL.md` file).
- `lib/ci_setup.py` — provider resolution + secret name lookup.
- `bin/set-provider.sh` — local provider switch.
- `.github/workflows/review.yml` — the GH-Actions equivalent (unchanged).
- `docs/local-ci.md` — full local-CI playbook (including the pre-push test gate
  on `/dev-kit:babysit-pr-local`).
