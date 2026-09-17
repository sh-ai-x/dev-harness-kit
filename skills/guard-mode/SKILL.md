---
name: guard-mode
category: config
description: Session-scoped on/off toggle for the tdd-guard, worktree-guard, and git-guard hard-block hooks.
alpha: state
when_to_use: |
  - User types /dev-kit:guard-mode off|on|show
  - User wants to temporarily suspend TDD RED-evidence enforcement (hooks/tdd-guard.sh) or main-checkout edit blocking (hooks/worktree-guard.sh) for THIS session only
  - A hotfix, config-file edit, or throwaway spike needs to bypass one of these hard blocks without touching project-level config
allowed-tools: Read Bash
model: opus
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:guard-mode — session-scoped hard-block toggle

## What it does

Writes `.dev-kit/guard-mode.session.json` via `lib/guard_mode_state.py`,
which `hooks/tdd-guard.sh`, `hooks/worktree-guard.sh`, and `hooks/git-guard.sh`
check before doing enforcement. Unlike
`/dev-kit:harness-mode` (which controls *optional* local hooks —
`tdd_scope_judge`, `slop_detector`, `maintenance`, ...), this skill controls
the three **hard-block** PreToolUse hooks that enforce Iron Law L1 (no prod
code without a verification artifact) and the `rules/git-workflow.md`
worktree-isolation/branch rules. Those hooks are deliberately out of
harness-mode's scope; this skill exists because turning them off is
sometimes the correct call (a config-only hotfix, a throwaway spike, a
main-checkout edit that genuinely doesn't need a worktree) and the
alternative — hand-editing state files — is the failure mode this skill
replaces.

**Session-scoped, not project-level.** A SessionStart hook
(`hooks/session-start-guard-mode-reset.sh`) applies the project/local
`DEV_KIT_GUARDS` policy at the start of every session. The default is `off`
for main and worktrees; an explicit `on` enables all three repository guards.
SessionStart never prompts.

**This is not `dev-kit-lite:setup-guard`.** That skill (a different plugin)
toggles TDD guard *and* the main-push block persistently via
`.dev-kit/.guard-config.json`, until an operator flips it back. This skill
toggles the three repository guards for the current session only, then
auto-reverts.

## Sub-commands

```bash
/dev-kit:guard-mode off              # turn OFF tdd/worktree/git for this session
/dev-kit:guard-mode off tdd          # turn OFF tdd-guard only
/dev-kit:guard-mode off worktree     # turn OFF worktree-guard only
/dev-kit:guard-mode off git          # turn OFF git-guard only
/dev-kit:guard-mode on               # turn ON tdd/worktree/git for this session
/dev-kit:guard-mode on tdd           # turn ON tdd-guard only
/dev-kit:guard-mode on worktree      # turn ON worktree-guard only
/dev-kit:guard-mode on git           # turn ON git-guard only
/dev-kit:guard-mode show             # print the current session state, no prompts
```

Run directly, no confirmation prompt needed — the state file itself is the
audit trail, and it self-reverts at the next session start:

```bash
python3 -m lib.guard_mode_state set tdd_guard off
python3 -m lib.guard_mode_state set worktree_guard off
python3 -m lib.guard_mode_state set tdd_guard on
python3 -m lib.guard_mode_state set worktree_guard on
python3 -m lib.guard_mode_state reset --policy off  # default SessionStart policy
```

`off` / `on` with no guard argument means all three repository guards; use a
guard name for a single override.

After any `set` or `reset` call, print `python3 -m lib.guard_mode_state show`
so the user sees exactly which guard changed and which one didn't, plus a
one-line reminder of what each guard protects:

```json
{
  "tdd_guard": {"value": "off", "description": "hooks/tdd-guard.sh — blocks prod code edits without RED evidence (Iron Law L1)"},
  "worktree_guard": {"value": "on", "description": "hooks/worktree-guard.sh — blocks Edit/Write/MultiEdit in the main checkout (rules/git-workflow.md worktree isolation)"},
  "git_guard": {"value": "on", "description": "hooks/git-guard.sh — protects main commits and pushes"}
}
```

### `show`

```bash
python3 -m lib.guard_mode_state show
```

No side effects — safe to run any time to check what's currently bypassed.

## What each guard protects

| Guard | Hook | Protects |
|---|---|---|
| `tdd_guard` | `hooks/tdd-guard.sh` | Iron Law L1 — denies a core-code edit unless `.dev-kit/.tdd-cycle.json` shows a logged RED (failing) test run |
| `worktree_guard` | `hooks/worktree-guard.sh` | `rules/git-workflow.md` — denies Edit/Write/MultiEdit while the session cwd is the main checkout |
| `git_guard` | `hooks/git-guard.sh` | Denies direct main commits, protected pushes, and unsafe branch changes |
| `fork_pr_confirm` | `hooks/pr-create-route.sh` | Ask-tier confirmation before `gh pr create` when `lib/actor_classifier` routes a fork PR to `fork_pr_review_environment`. Default **off** — silent breadcrumb only; flip on per-session to surface every consumer_fork classification to the human before the PR is opened. |

Turning a guard `off` does not touch the other guard, and does not touch
anything `/dev-kit:harness-mode` controls (`tdd_scope_judge`,
`slop_detector`, `maintenance`, `security_owasp`, `pre_commit_review`,
`babysit_pr`) or the four hooks harness-mode always keeps on
(`stop_verify`, `secret_scan`, `intent_integrity`, `gh_ci_required`) — those
remain fully enforced regardless of guard-mode state.

## Why this exists

`lib/guard_mode_state.py` is the tested session mechanism:
`tests/test_guard_mode_state.py` covers state, and
`tests/test_guard_mode_hooks.py` covers the hook behavior end-to-end.

## Related

- `lib/guard_mode_state.py` — the state module (`read_state`, `write_state`, `reset_state`, `resolved_guard`, CLI).
- `hooks/lib/guard-policy.sh` — scoped `DEV_KIT_GUARDS` resolver.
- `hooks/session-start-guard-mode-reset.sh` — the SessionStart policy hook.
- `hooks/tdd-guard.sh`, `hooks/worktree-guard.sh`, `hooks/git-guard.sh` — the hooks this skill bypasses.
- `skills/harness-mode/SKILL.md` — the sibling skill for the *optional* local hooks this skill does not touch.
- `iron-laws/index.md` (L1), `rules/git-workflow.md` (worktree isolation) — the rules each guard enforces.

## Next step

Make the edit the guard was blocking. Re-run `/dev-kit:guard-mode show`
before ending the session if you want to confirm what will auto-revert at
the next SessionStart (everything — the reset is unconditional).
