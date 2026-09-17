---
name: babysit-pr
category: ship
description: 0-arg restart-safe PR check, diagnose, fix, and hand-off loop.
alpha: state
when_to_use: |
  - User types `/dev-kit:babysit-pr`
  - An open PR on the current branch needs evidence-driven CI/review iteration
allowed-tools: Read Write Bash Agent
disallowed-tools: WebFetch
model: opus
user-invocable: true
---
> [← Skills index](../../README.md) · Algorithm SSOT: [docs/skills/babysit-pr.md](../../docs/skills/babysit-pr.md)

# `/dev-kit:babysit-pr`

Before acting, read `docs/skills/babysit-pr.md`; it contains the complete
algorithm, state schema, tracker behavior, and operator bypass contract. This
file keeps the invocation and non-negotiable safety gates visible to the
orchestrator.

## Target resolution: evidence only

Default target is the open PR for the current branch. `--pr N` is explicit.
`CONVERSATION_PR` is established only by a literal PR number in the user
message or the immediately preceding assistant tool result that created/listed
that PR. The phrase `"babysit the latest PR"` is not evidence; never infer a
target from recency or PR number.

### Validate a conversation handoff

From main, validate a handoff with `gh pr view "$CONVERSATION_PR"` before
any candidate enumeration. If `CONVERSATION_STATE" != "OPEN"`, exit 1 rather
than reporting success. A validated handoff goes directly to worktree
resolution. Otherwise list candidate PRs off main. Exactly one candidate, or a
validated conversation handoff, goes directly to worktree resolution;
The rule is: Exactly one candidate, or a validated conversation handoff goes
directly to worktree resolution.
**Multiple candidates without a conversation handoff** are printed and stop.
**Never auto-pick.** Zero candidates exits 0 with “nothing to babysit”.

Run inside the PR-owning worktree. Main may resolve or create that worktree
only after explicit target validation; outside a repository exits 0. Source
`hooks/lib/worktree-detect.sh` rather than reimplementing checkout detection.

## Durable loop

The complete loop is: snapshot PR/review/checks → load and persist phase →
terminate only on approved + green → classify blockers → wait on pending →
fetch changed failure logs → diagnose one root cause → apply one logical fix →
verify locally and quote `local: <command> → <result> (exit <code>)` → persist
outcome → commit specific paths → push → log → wait → save check-state →
increment with a bounded cap. Unchanged failing checks are not re-fetched;
this saves polling, not CI execution.

Persist `WAIT_FOR_CHECKS`, `WAIT_FOR_APPROVAL`, and `RECOVERY_REQUIRED` so a
restart cannot become a false success. Three consecutive no-information
outcomes enter recovery. A live `.dev-kit/babysit.lock` refuses a second run;
stale locks use `lib/babysit_pr_reliability.py:is_stale_lock()`. Keep the
PR-owning worktree, branch, logs, and `.dev-kit/babysit-retention.json` after
terminal PR state for post-PR analysis; only the operator cleans them up.

## Safety gates

- No auto-merge, `gh pr merge`, force-push to main/master, `reset --hard`,
  `clean -fd`, branch deletion, or secret auto-removal.
- Never skip a failing test, weaken a required check, disable review/security,
  mask a root cause with `|| true`, or push before local verification passes.
- Secret detection aborts with file:line. Review approval is human-only;
  only approved + green is successful completion.
- `MAX_ITERS` limits the worker, not approval time. Never silently retry past
  the cap and never claim “fixed” without the evidence line and exit code.

## Single-operator bypass

`--operator-is-only-human` requires `--rationale`, checks CODEOWNERS and the
GitHub collaborators API, and posts an audit comment only when no alternate
owner is confirmed. API uncertainty is fail-closed to the human gate. It
never merges. See the docs for helper exit codes and named I/O shims.

## Usage and hand-off

```bash
/dev-kit:babysit-pr [--pr N] [--operator-is-only-human] [--rationale "<text>"]
```

Every iteration reports branch, check, verdict, log, fix, local verification,
push, review, and remaining blockers. The loop uses `stop-verify`,
`secret-scan`, `slop-detector`, `bash-guard`, and `git-guard`; `tdd-guard` is
off because this skill repairs an existing PR rather than authoring a feature.
After approved + green, hand off to `/dev-kit:ship`; this skill never merges.
