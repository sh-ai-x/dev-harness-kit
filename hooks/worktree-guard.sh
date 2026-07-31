#!/usr/bin/env bash
# worktree-guard.sh — PreToolUse hook for Write|Edit|MultiEdit.
#
# Enforces .claude/rules/git-workflow.md "every task = new worktree" rule,
# with a confirmation-prompt layer for the main checkout (chore/wg-ask-mode).
#
# Asks (exit 0 + permissionDecision:"ask" JSON) — main checkout, non-safelist:
#   Edit / Write / MultiEdit on any path that is NOT in the safelist below.
#   Surfaces the Iron Law L1 reminder + worktree list in the prompt body so
#   the user can confirm with a justification that lands in the transcript.
#
# Allows (exit 0):
#   Edits from inside ANY git worktree. The discriminator is
#   `git_dir == git_common_dir` which is robust to the worktree living
#   anywhere on disk (not just `.worktrees/`).
#   Edits in non-git directories — this hook is project-scoped.
#   Empty / probe payloads — nothing to gate.
#   Main-checkout edits whose FILE_PATH is in the safelist:
#     .dev-kit/**                        (hand-off notes, round-* tmp, scratch)
#     .claude/settings.local.json        (per-user Claude overrides)
#     .codex/settings.local.json         (per-user Codex overrides)
#     .worktrees/.gitignore              (worktree bookkeeping)
#
# Fails closed (exit 2 with deny JSON) when `jq` is missing. Even ask mode
# must refuse to soften without a parseable payload — silent fail-open
# would disable the rule.
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
  worktree|outside|"") exit 0 ;;
  main) ;;
  *) exit 0 ;;
esac

# In main checkout → safelist short-circuit, otherwise ASK (not deny).
# The previous hard-deny was softened to a confirmation prompt so the
# user can override with an explicit reason captured in the transcript.
# Real code / docs / hooks / tests / plugin / template edits still
# gate on approval; routine bootstrap and per-user config edits pass.
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo detached)"

# _safelist_match PATH — return 0 if PATH is a bootstrap / hand-off /
# per-user override path that may be edited on main without prompting.
# Iron Law L1 protects code on main, not session notes or local
# overrides; these paths are NOT "real code changes":
#   .dev-kit/**                        # hand-off notes, round-* tmp, scratch
#   .claude/settings.local.json        # per-user Claude overrides
#   .codex/settings.local.json         # per-user Codex overrides
#   .worktrees/.gitignore              # worktree bookkeeping
# The case-globbing below is a defensive match: each pattern accepts
# either an absolute path ending in the literal (cd into main checkout
# yields /abs/.dev-kit/...) or a repo-relative path (.dev-kit/...).
_safelist_match() {
  case "$1" in
    */.dev-kit/*|*/.dev-kit|.dev-kit/*|.dev-kit) return 0 ;;
    */.claude/settings.local.json|.claude/settings.local.json) return 0 ;;
    */.codex/settings.local.json|.codex/settings.local.json) return 0 ;;
    */.worktrees/.gitignore|.worktrees/.gitignore) return 0 ;;
    *) return 1 ;;
  esac
}

if _safelist_match "$FILE_PATH"; then
  exit 0
fi

# _worktree_list_rich — Phase 2.1 (issue #358).
# Enumerate the project's worktrees for the ask message. Uses
# `git worktree list --porcelain` directly. The previous LCS-first /
# shell-fallback shape (with timeout-primitive selection for the LCS
# read) was removed in PR #462 because the per-call Python startup
# cost made it net-negative; the ask-path latency budget is ~10 ms
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

MSG="Editing on main checkout (branch='$BRANCH') requires explicit approval.

Canonical path: open a Claude session in a worktree (chore/<slug>, fix/<slug>, etc.).

If you must edit here, approve and state the reason in the dialog — the
override lands in the transcript and is reviewable later. Routine
bootstrap paths (.dev-kit/**, settings.local.json, .worktrees/.gitignore)
are auto-allowed and do NOT trigger this prompt.

Routing (when you decide to cut a worktree instead):
  git worktree add -b <type>/<slug> .worktrees/<slug> origin/main
  cd .worktrees/<slug>
  open a Claude session there

Hard rules (Iron Laws: see iron-laws/index.md, L1/L3/L4/L5):
  M push / commit / PR to main: forbidden
  M edit of code files in any worktree: forbidden (Tier 1 = orchestrator)
  Other worktrees are private to their T; entry is allowed ONLY for hand-off docs
   in .dev-kit/round-*/**."

  ask "WORKTREE GUARD" "$MSG

$WT_BLOCK"
