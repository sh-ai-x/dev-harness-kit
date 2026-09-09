#!/usr/bin/env bash
# session-start-worktree-cut.sh — SessionStart hook.
#
# Auto-enforcer for the "main is sacred, every task = new worktree + new
# branch" rule (rules/git-workflow.md).
#
# Fires ONCE at session start. When the session opens in the MAIN repo
# checkout (not a worktree) AND the user's first prompt is a task (verb
# regex: implement / add / build / create / fix / refactor / etc.),
# this hook cuts a fresh worktree off origin/main, bootstraps log-on,
# and emits additionalContext naming the new path. The Claude Code /
# Codex runtime then opens a sub-session there.
#
# Why SessionStart (not UserPromptSubmit):
#   The prior worktree-auto-cut.sh wired this logic into UserPromptSubmit
#   (per-prompt synchronous gate). That triggered network-bound
#   `git fetch origin main` on every prompt and stalled the user (the
#   session-long hook-timeout cascade). SessionStart fires once per
#   session — the `git fetch` cost is paid once, not on every prompt.
#   Same enforcement, different latency class.
#
# When the prompt is NOT a task (investigation, Q&A, just looking),
# this hook exits silently and the session proceeds in main. Any
# Edit|Write attempt then trips worktree-guard.sh (PreToolUse), which
# prompts the user to cut a worktree manually.
#
# Discriminator (worktree-detect.sh):
#   WORKTREE_DETECT=main     → may cut (subject to task-intent regex)
#   WORKTREE_DETECT=worktree → silent (rule already satisfied)
#   WORKTREE_DETECT=outside  → silent (not a git working tree)
#   WORKTREE_DETECT=""       → silent (jq missing — fail open, no-op)
#
# Mirrors worktree-auto-cut.sh: derives `<type>/<verb>-<noun>-<hash6>`,
# routes through lib.git_worktree.cut_worktree (the canonical helper
# per issue #322), bootstraps log-on, and falls back gracefully on any
# failure (returns a manual-cut envelope instead of crashing the
# session).
#
# Always exits 0 (non-blocking). On failure, the fallback envelope
# tells the operator exactly which command to run manually.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Fail open with a stderr warning if jq is missing — the preamble
# already populated $WORKTREE_DETECT="" so the case below treats it
# as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "session-start-worktree-cut.sh"
  exit 0
fi

# Prefer cwd from the hook payload (more authoritative than $PWD).
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: only fire in the MAIN checkout.
case "$WORKTREE_DETECT" in
  main) ;;
  *) exit 0 ;;
esac

# Detect task intent. SessionStart payloads carry the session's first
# prompt (or empty). Empty prompt → investigation session in main →
# silent. Task-shaped prompt → cut a worktree before any tool fires.
PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
task_intent=0
LOWER="$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')"
# Allow leading slash (slash command).
case "$LOWER" in
  /*) task_intent=1 ;;
esac
# Bare verb at start of prompt.
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE '^(implement|add|build|create|fix|refactor|develop|introduce|write|design)([[:space:]]|$|:)'; then
  task_intent=1
fi
# Polite / question forms.
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE "(let'?s|i want to|please|can you|could you|help me)[[:space:]]+(implement|add|build|create|fix|refactor|develop|introduce|write|design)"; then
  task_intent=1
fi
# New-feature noun phrases.
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE "(new (feature|task|endpoint|function|module|hook|skill)|feature request|bug report)"; then
  task_intent=1
fi
# Korean task prompts.
if [ "$task_intent" = "0" ] \
  && printf '%s' "$LOWER" | grep -qE '(수정|해결|구현|추가|변경|만들|작업)' \
  && printf '%s' "$LOWER" | grep -qE '(hook|브랜치|worktree|레ポ|repo|코드|파일|에러|오류|기능)'; then
  task_intent=1
fi
# Require a code-edit verb to be present anywhere in the prompt (Q2
# safer-trigger policy: pure investigation doesn't fire).
if [ "$task_intent" = "1" ] \
  && ! printf '%s' "$LOWER" | grep -qE '(implement|add|build|create|fix|refactor|rename|delete|remove|update|change|introduce)[[:space:]]+(file|function|method|class|module|hook|skill|test|feature|column|field|variable|api|endpoint|route|handler|component|import|export|line|lines)' \
  && ! printf '%s' "$LOWER" | grep -qE '((수정|해결|구현|추가|변경|만들|작업).*(hook|브랜치|worktree|레포|repo|코드|파일|에러|오류|기능)|(hook|브랜치|worktree|레포|repo|코드|파일|에러|오류|기능).*(수정|해결|구현|추가|변경|만들|작업))'; then
  task_intent=0
fi
[ "$task_intent" = "1" ] || exit 0

# Precondition 1: main is clean.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  main checkout is dirty; stash or commit its changes before cutting a clean worktree" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Derive slug from the prompt. Strategy mirrors worktree-auto-cut.sh:
# take the first strong verb + first 1-2 content words, kebab-case,
# append 6-char hash from a prompt-derived seed (deterministic so two
# sessions with the same prompt at different times don't collide).
derive_slug() {
  local prompt_lc="$1"
  local verb noun slug hash seed type
  verb="$(printf '%s' "$prompt_lc" | grep -oE '^(implement|add|build|create|fix|refactor|develop|introduce|write|design)' | head -1)"
  [ -z "$verb" ] && verb="fix"
  noun="$(printf '%s' "$prompt_lc" | sed -E "s/^${verb}//; s/[[:punct:]]//g; s/[[:space:]]+/\n/g" \
        | grep -vE '^(a|an|the|to|for|of|in|on|at|by|with|that|this|it|its|be|is|are|was|were|i|me|my|we|our|you|your)$|^$' \
        | head -2 \
        | tr '\n' '-' \
        | sed 's/-$//')"
  if [ -n "$noun" ]; then
    slug="${verb}-${noun}"
  else
    slug="${verb}"
  fi
  slug="${slug:0:24}"
  slug="${slug%-}"
  if ! printf '%s' "$slug" | grep -qE '^[a-z0-9-]+$'; then
    slug="task"
  fi
  type="fix"
  seed="auto-cut:${prompt_lc}"
  hash="$(printf '%s' "$seed" | git hash-object --stdin 2>/dev/null | head -c 6 || true)"
  [ -z "$hash" ] && hash="$(date +%s | tail -c 7)"
  printf '%s/%s-%s\n' "$type" "$slug" "$hash"
}

# Slug validation: branch-name regex from rules/git-workflow.md.
SLUG="$(derive_slug "$LOWER")"
if ! printf '%s' "$SLUG" | grep -qE '^(fix|feat|refactor|docs|test|chore|perf|hotfix)/[a-z0-9-]{2,40}$'; then
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  the task could not be converted to a valid branch name
  action:  cut a worktree manually: git fetch origin main && git worktree add -b <type>/<slug> .worktrees/<slug> origin/main" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi
# Reject forbidden slugs (per .claude/rules/git-workflow.md).
case "$SLUG" in
  */wip|*/tmp|*/foo|*/bar|*/asdf|*/test|*/scratch|*/untitled) exit 0 ;;
esac

BRANCH="$(git worktree list --porcelain 2>/dev/null \
  | awk -v want="$SLUG" '
      /^worktree / { wt=$2 }
      /^branch / && $2 == "refs/heads/"want {
          print wt; found=1; exit
      }
      END { if (!found) exit 0 }
    ' && echo exists || true)"
if [ -n "$BRANCH" ] && [ -d "$BRANCH" ]; then
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  a worktree already exists for branch $SLUG
  path:    $BRANCH" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Resolve which main ref to branch from. Prefer origin/main (just-
# fetched); fall back to local main (no-remote case, e.g. tests).
MAIN_REF=""
if git remote get-url origin >/dev/null 2>&1; then
  if git fetch origin main >/dev/null 2>&1; then
    MAIN_REF="origin/main"
  fi
fi
if [ -z "$MAIN_REF" ] && git rev-parse --verify main >/dev/null 2>&1; then
  MAIN_REF="main"
fi
[ -n "$MAIN_REF" ] || {
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  no main branch is available as a worktree base
  action:  configure 'main' (or 'origin/main') and retry, or cut a worktree manually" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
}

# Resolve repository root (canonical). Hooks run in the session's
# cwd; resolve to toplevel so a subdirectory start still shares the
# canonical worktree root with Claude Code and Codex.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
WT_PARENT="$REPO_ROOT/.worktrees"
mkdir -p "$WT_PARENT"
WT_PATH="$WT_PARENT/${SLUG#*/}"  # strip type prefix for directory name

# Precondition 4: target worktree directory doesn't already exist.
if [ -d "$WT_PATH" ]; then
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  the target worktree path already exists: $WT_PATH
  action:  remove it (git worktree remove --force $WT_PATH) and retry, or cut a worktree manually" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Auto-cut. Routed through the canonical lib.git_worktree.cut_worktree
# helper (issue #322) so the safe-mode contract matches
# lib/execute.py + lib/acp_dispatch.py. Future contract changes touch
# one place. Note: macOS does not ship coreutils `timeout` by default;
# the hook-level timeout (we keep none) bounds the operation via the
# harness's own per-hook timeout cap (currently no UserPromptSubmit
# hooks carry one because they're trivial).
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LIB_DIR="$PLUGIN_ROOT/lib"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"

if ! python3 - "$REPO_ROOT" "$SLUG" "$WT_PATH" "$MAIN_REF" "$LIB_DIR" <<'PYEOF'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
branch = sys.argv[2]
wt_path = Path(sys.argv[3])
base = sys.argv[4]
lib_dir = sys.argv[5]

if lib_dir and lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from git_worktree import cut_worktree  # noqa: E402

try:
    cut_worktree(
        repo_root=repo_root,
        branch=branch,
        worktree_path=wt_path,
        base=base,
    )
except Exception as exc:
    print(f"cut_worktree failed: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
then
  jq -nc --arg ctx "session-start-worktree-cut unavailable
  reason:  git worktree add failed for branch $SLUG
  action:  cut a worktree manually: git fetch origin main && git worktree add -b $SLUG $WT_PATH $MAIN_REF" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
  exit 0
fi

# Bootstrap log-on inside the new worktree. Falls through silently if
# the consumer project hasn't installed the dev-kit log skill.
LOG_SETUP="$PLUGIN_ROOT/skills/log/scripts/log-setup.sh"
LOG_ON="$PLUGIN_ROOT/skills/log/scripts/log-on.sh"
if [ -f "$LOG_SETUP" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_SETUP" >/dev/null 2>&1) || true
fi
if [ -f "$LOG_ON" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_ON" >/dev/null 2>&1) || true
fi

# Linear bootstrap: trigger one auto-sync round in the new worktree
# so the handoff is registered before the first SessionStart or
# Edit|Write in the new path. Owner-gated inside auto_sync().
if [ -f "$WT_PATH/tools/linear_sync.py" ]; then
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      (cd "$WT_PATH" && "$py" "$WT_PATH/tools/linear_sync.py" auto-sync) || true
      break
    fi
  done
fi

# Build additionalContext — Claude Code consumes this to open a new
# session in the new worktree; Codex spawns a sub-agent there.
CTX="session-start-worktree-cut ready
  branch:  $SLUG
  path:    $WT_PATH
  Claude Code next: open a new session in $WT_PATH
  Codex next: spawn a subagent with cwd=$WT_PATH and branch=$SLUG
  handoff: pass the original task prompt and this worktree path to the client-specific worker
  fallback: if any of the above fails, run the canonical helper manually:
            python3 -c \"from lib.git_worktree import cut_worktree; cut_worktree(repo_root=Path('$REPO_ROOT'), branch='$SLUG', worktree_path=Path('$WT_PATH'), base='$MAIN_REF')\""
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
exit 0