#!/usr/bin/env bash
# worktree-guard.sh — PreToolUse hook for Write|Edit|MultiEdit.
#
# Enforces .claude/rules/git-workflow.md "every task = new worktree" rule.
#
# Denies (exit 2):
#   Edit / Write / MultiEdit when the session cwd is the MAIN repo checkout
#   (the checkout that owns the .git directory at its root). Forces the
#   user to cut a worktree off origin/main before making any edits.
#
# Allows (exit 0):
#   Edits from inside ANY git worktree. The discriminator is
#   `git_dir == git_common_dir` which is robust to the worktree living
#   anywhere on disk (not just `.worktrees/`).
#   Edits in non-git directories — this hook is project-scoped.
#   Empty / probe payloads — nothing to gate.
#
# Fails closed (exit 2 with deny JSON) when `jq` is missing.
#
# The discriminator lives in hooks/lib/worktree-detect.sh so the
# three rule-hooks don't drift. See .claude/rules/git-workflow.md.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning) + payload-parse.sh for the
# `deny` helper. The jq-missing warning is informational here — this
# hook fails closed below via its own printf (deny() itself needs jq).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"

# Fail CLOSED if jq is missing. Without jq we cannot parse the
# PreToolUse payload — silent fail-open would disable this rule.
if ! command -v jq >/dev/null 2>&1; then
  # Hand-built printf here (not the deny() helper from payload-parse.sh)
  # because deny() itself depends on jq. Self-contained fail-closed.
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"WORKTREE GUARD: jq is required by worktree-guard.sh but not installed. Install jq (apt/brew/apk) — without it, the worktree rule cannot be enforced."}}\n' >&2
  exit 2
fi

# Extract the target file path. If the payload is empty or has no
# file_path (e.g. a probe call with empty stdin), exit 0 — there is
# nothing to gate. This must run BEFORE the worktree-detect check so
# a probe call from any cwd (main checkout included) is a no-op.
FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // ""' 2>/dev/null)"
[ -z "$FILE_PATH" ] && exit 0

# Session-scoped bypass via /dev-kit:guard-mode. Reset to "on" at every
# SessionStart (hooks/session-start-guard-mode-reset.sh) — never persists
# across sessions. Fails to "on" (enforced) if python3 is unavailable.
if [ "$(python3 -m lib.guard_mode_state get worktree_guard 2>/dev/null)" = "off" ]; then
  exit 0
fi

# Orchestration branches (orch/*) are routing/analysis-only worktrees.
# Edits to protected paths (code, hooks, tests, manifests, plugins,
# and source extensions) are denied here so any code change still
# flows through a non-orchestration worktree
# (fix/|feat/|docs/|chore/|test/|refactor/|perf/|hotfix/). User
# handoff temp notes under .dev-kit/round-*/** remain writable so
# the orchestrator can leave round-N notes for the receiving client.
#
# B — branch detection goes via file_path extraction (NOT the
# parent-session cwd), because sub-agents running inside a nested
# worktree still inherit the parent's `git symbolic-ref --short HEAD`
# output of `main` — see the parent-cwd misfire notes. The previous
# version of this hook therefore always saw `main` and never fired
# the orch branch check; the file_path extraction below closes that
# gap by reading the branch from the worktree the file_path points
# into (the worktree IS a git linkfile, so `git -C <path>` resolves
# the correct branch without cd).
ORCH_BRANCH=""
if [[ "$FILE_PATH" =~ (\.worktrees/)([^/]+) ]]; then
  WT_NAME="${BASH_REMATCH[2]}"
  # Resolve the worktree dir relative to the main checkout, which
  # always owns the `.worktrees/<name>/` sibling directories.
  if [ -d ".worktrees/${WT_NAME}" ]; then
    ORCH_BRANCH="$(git -C ".worktrees/${WT_NAME}" symbolic-ref --short HEAD 2>/dev/null || echo detached)"
  fi
fi
if [[ "$ORCH_BRANCH" == orch/* ]]; then
  # .dev-kit/round-*/** hand-off tmp notes are the ONLY writable paths
  # on an orchestration branch — short-circuit before main-deny so the
  # orchestrator can leave round-N notes even if cwd is main checkout.
  # Matches .dev-kit/round-* at the start OR after any slash segment.
  if [[ "$FILE_PATH" =~ (^|/)\.dev-kit/round- ]]; then
    exit 0
  fi
  case "$FILE_PATH" in
    *lib/*|*lib|*skills/*|*skills|*hooks/*|*hooks|*tests/*|*tests|*templates/*|*templates|*bin/*|*bin|*.codex-plugin*|*.claude-plugin*|*.py|*.sh|*.ts|*.js)
      deny "ORCH ISOLATION" "code edits are forbidden in orch/* worktree. Allowed paths only are .dev-kit/round-*/**. Move the change to a feature worktree."
      ;;
  esac
fi

# Detect whether we are in the main checkout or a worktree. The lib
# function never returns 1 here because we just verified jq exists;
# $WORKTREE_DETECT was already populated by the preamble.
case "$WORKTREE_DETECT" in
  worktree|outside|"") emit_guard_event "WORKTREE GUARD" "allowed" allowed; exit 0 ;;
  main) ;;
  *) exit 0 ;;
esac

# In main checkout → deny with actionable reason. The case statement
# and the deny() call below are byte-identical to the pre-PR-270
# version — only the MSG string content is updated to the
# deterministic env-var checklist + Iron Laws recap.
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo detached)"

# _worktree_list_rich — Phase 2.1 (issue #358).
# Enumerate the project's worktrees for the deny message. Uses
# `git worktree list --porcelain` directly. The previous LCS-first /
# shell-fallback shape (with timeout-primitive selection for the LCS
# read) was removed in PR #462 because the per-call Python startup
# cost made it net-negative; the deny-path latency budget is ~10 ms
# with direct shell on this repo (~220 worktrees today).
#
# The LCS substrate was dropped entirely in #463; the CLI no longer
# ships. Direct `git worktree list --porcelain` is the only path.
_worktree_list_rich() {
  : "no-op placeholder kept for structural symmetry with the prior shape"
  # Fallback: porcelain worktree list, branch stripped of refs/heads/.
  # Handle both `^branch refs/heads/X` and `^detached` so a CI
  # checkout in detached HEAD (the common case for `actions/checkout`
  # on a PR ref) still surfaces a worktree entry instead of an empty
  # list.
  git worktree list --porcelain 2>/dev/null | awk '
    /^worktree / { path = substr($0, 10); pending = 1; next }
    /^branch /   { sub(/^refs\/heads\//, "", $2); print "  " path "\t" $2; pending = 0 }
    /^detached/  { if (pending) { print "  " path "\t(detached)"; pending = 0 } }
  '
}
WT_LIST="$(_worktree_list_rich)"
if [ -n "$WT_LIST" ]; then
  WT_BLOCK="Existing worktrees (cd into one, or open a Claude session there):
$WT_LIST"
else
  WT_BLOCK="(no worktrees listed — run \`git worktree list\` to enumerate them)"
fi

MSG="editing in main checkout (branch='$BRANCH') is forbidden.

REQUIRED environment setup before retrying:
  git config --global dev-kit.orch.client=claude   # or codex
  git config --global dev-kit.orch.concurrency=single   # or parallel

Without these, abort this edit. Re-running without setting them will be denied.

Routing (after config is set):
  claude  + single   -> git worktree add -b <type>/<slug> .worktrees/<slug> origin/main
                        cd .worktrees/<slug>
                        open a Claude session there
  claude  + parallel -> same worktree, then fan out sub-agents via the Agent tool
  codex   + single   -> git worktree add ..., then spawn one sub-agent with cwd=<worktree>
  codex   + parallel -> spawn N sub-agents each with cwd=<worktree> and explicit task prompt

Hard rules (Iron Laws: see iron-laws/index.md, L1/L3/L4/L5):
  M push / commit / PR to main: forbidden
  M edit of code files in any worktree: forbidden (Tier 1 = orchestrator)
  Other worktrees are private to their T; entry is allowed ONLY for hand-off docs
   in .dev-kit/round-*/**."

  deny "WORKTREE GUARD" "$MSG

$WT_BLOCK"
