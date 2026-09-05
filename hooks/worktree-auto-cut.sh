#!/usr/bin/env bash
# worktree-auto-cut.sh — UserPromptSubmit hook (advisory, never blocks).
#
# Auto-derives a `<type>/<verb>-<word1>-<hash>` slug from the prompt, cuts
# the worktree (with preconditions), and bootstraps log-on. Falls back
# to a manual-cut nudge on any failure. Slug derivation details are
# in the design doc at docs/designs/worktree-auto-cut.md (PR #320).
#
# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Hooks are advisory, but a silent fallback makes the next edit look like an
# unrelated hard failure from worktree-guard.sh. Always return a handoff
# envelope when a task was detected but auto-cut could not proceed.
fallback_context() {
  local reason="$1"
  local ctx="worktree auto-cut unavailable
  reason:  $reason
  action:  do not edit the main checkout; resolve the reason, then cut the worktree via the canonical helper:
           python3 -c \"from lib.git_worktree import cut_worktree; from pathlib import Path; cut_worktree(repo_root=Path.cwd(), branch='<type>/<slug>', worktree_path=Path('.worktrees/<slug>'), base='origin/main')\"
  Codex:   after the worktree exists, spawn a subagent with that path as cwd and pass the original task prompt"
  jq -nc --arg ctx "$ctx" \
    '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'
}

# Fail open with a stderr warning if jq is missing — this hook is
# advisory; worktree-guard.sh is the hard block.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "worktree-auto-cut.sh"
  exit 0
fi

PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
[ -z "$PROMPT" ] && exit 0

# Prefer cwd from the hook payload; fall back to PWD.
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: already populated by the preamble. Only fire in
# the main checkout. Worktree sessions already follow the rule;
# outside-git is out of scope.
case "$WORKTREE_DETECT" in
  worktree|outside|"") exit 0 ;;
  main) ;;
  *) exit 0 ;;
esac

# Detect task intent (verb regex mirrors the policy described in
# two hooks' classification identical so users see consistent behavior).
LOWER="$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')"
task_intent=0
case "$LOWER" in
  /*) task_intent=1 ;;
esac
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE '^(implement|add|build|create|fix|refactor|develop|introduce|write|design)([[:space:]]|$|:)'; then
  task_intent=1
fi
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE "(let'?s|i want to|please|can you|could you|help me)[[:space:]]+(implement|add|build|create|fix|refactor|develop|introduce|write|design)"; then
  task_intent=1
fi
if [ "$task_intent" = "0" ] && printf '%s' "$LOWER" | grep -qE "(new (feature|task|endpoint|function|module|hook|skill)|feature request|bug report)"; then
  task_intent=1
fi
# Korean task prompts need the same worktree protection. Keep the signal
# narrow: require both an action word and a code/repository noun.
if [ "$task_intent" = "0" ] \
  && printf '%s' "$LOWER" | grep -qE '(수정|해결|구현|추가|변경|만들|작업)' \
  && printf '%s' "$LOWER" | grep -qE '(hook|브랜치|worktree|레포|repo|코드|파일|에러|오류|기능)'; then
  task_intent=1
fi
# Require a code-edit verb to be present anywhere in the prompt
# (Q2: safer than the leading-verb check alone). This filters out
# "investigate this error", "explain X", "what does Y do?".
if [ "$task_intent" = "1" ] \
  && ! printf '%s' "$LOWER" | grep -qE '(implement|add|build|create|fix|refactor|rename|delete|remove|update|change|introduce)[[:space:]]+(file|function|method|class|module|hook|skill|test|feature|column|field|variable|api|endpoint|route|handler|component|import|export|line|lines)' \
  && ! printf '%s' "$LOWER" | grep -qE '((수정|해결|구현|추가|변경|만들|작업).*(hook|브랜치|worktree|레포|repo|코드|파일|에러|오류|기능)|(hook|브랜치|worktree|레포|repo|코드|파일|에러|오류|기능).*(수정|해결|구현|추가|변경|만들|작업))'; then
  task_intent=0
fi
[ "$task_intent" = "1" ] || exit 0

# Precondition 1: main is clean.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  fallback_context "main checkout is dirty; stash or commit its changes before cutting a clean worktree"
  exit 0
fi

# Derive slug from the prompt. Strategy:
#   - Take the first strong verb + the first 1-2 content words
#   - Lowercase, strip non-alphanumeric (keep kebab-case)
#   - Truncate to <= 30 chars (room for type + hash)
#   - Append 6-char hash = first 6 of `git hash-object /dev/null` on a
#     seed derived from the prompt itself (deterministic per prompt).
# If the regex doesn't match (very rare for task prompts), fall back
# to a date-based slug.
derive_slug() {
  local prompt_lc="$1"
  local verb noun slug hash seed type
  # Extract first verb (whichever is at the start of the prompt).
  verb="$(printf '%s' "$prompt_lc" | grep -oE '^(implement|add|build|create|fix|refactor|develop|introduce|write|design)' | head -1)"
  [ -z "$verb" ] && verb="fix"
  # First 1-2 content words AFTER the verb. Strip punctuation, drop
  # common stop words. Cap at 20 chars total.
  noun="$(printf '%s' "$prompt_lc" | sed -E "s/^${verb}//; s/[[:punct:]]//g; s/[[:space:]]+/\n/g" \
        | grep -vE '^(a|an|the|to|for|of|in|on|at|by|with|that|this|it|its|be|is|are|was|were|i|me|my|we|our|you|your)$|^$' \
        | head -2 \
        | tr '\n' '-' \
        | sed 's/-$//')"
  # Compose: verb-noun- (or just verb- if noun empty).
  if [ -n "$noun" ]; then
    slug="${verb}-${noun}"
  else
    slug="${verb}"
  fi
  # Truncate slug body to 24 chars (leaves 6 for hash, plus type prefix).
  slug="${slug:0:24}"
  # Strip trailing dash from the truncation.
  slug="${slug%-}"
  # Non-Latin prompts may not produce an ASCII noun. Keep the branch name
  # valid instead of silently abandoning the automatic handoff.
  if ! printf '%s' "$slug" | grep -qE '^[a-z0-9-]+$'; then
    slug="task"
  fi
  # Type prefix — default to "fix" because the user can rename before
  # commit; the verb mapping is intentionally not used.
  type="fix"
  # Hash from the full prompt — deterministic, so two sessions with the
  # same prompt at different times don't collide.
  seed="auto-cut:${prompt_lc}"
  hash="$(printf '%s' "$seed" | git hash-object --stdin 2>/dev/null | head -c 6 || true)"
  [ -z "$hash" ] && hash="$(date +%s | tail -c 7)"
  printf '%s/%s-%s\n' "$type" "$slug" "$hash"
}

# Check if the branch already exists; if so, append a numeric suffix.
unique_branch_name() {
  local base="$1"
  local candidate="$base"
  local n=1
  while git show-ref --verify --quiet "refs/heads/${candidate}" 2>/dev/null; do
    candidate="${base}-${n}"
    n=$((n + 1))
    [ "$n" -gt 99 ] && return 1
  done
  printf '%s\n' "$candidate"
}

SLUG="$(derive_slug "$LOWER")"
# Slug must match the project branch-naming regex (kebab-case, length
# 2-40, type prefix, no forbidden words). Reject and fall back if not.
if ! printf '%s' "$SLUG" | grep -qE '^(fix|feat|refactor|docs|test|chore|perf|hotfix)/[a-z0-9-]{2,40}$'; then
  fallback_context "the task could not be converted to a valid branch name"
  exit 0
fi
# Reject forbidden slugs (per .claude/rules/git-workflow.md).
case "$SLUG" in
  */wip|*/tmp|*/foo|*/bar|*/asdf|*/test|*/scratch|*/untitled) exit 0 ;;
esac

BRANCH="$(unique_branch_name "$SLUG" 2>/dev/null)" || exit 0
DIRNAME="${BRANCH#*/}"  # strip type prefix for the worktree dir name.

# Resolve which main ref to branch from. Prefer origin/main (just-
# fetched); fall back to local main (no-remote case, e.g. tests).
MAIN_REF=""
if git remote get-url origin >/dev/null 2>&1; then
  if git fetch origin main >/dev/null 2>&1; then
    MAIN_REF="origin/main"
  else
    fallback_context "fetching origin/main failed; the worktree must start from the latest remote main"
    exit 0
  fi
fi
if [ -z "$MAIN_REF" ] && git rev-parse --verify main >/dev/null 2>&1; then
  MAIN_REF="main"
fi
[ -n "$MAIN_REF" ] || {
  fallback_context "no main branch is available as a worktree base"
  exit 0
}

# Hooks run in the session's current directory. Resolve the repository root
# first so a session started from a subdirectory still shares the canonical
# worktree root with Claude Code and Codex.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
WT_PARENT="$REPO_ROOT/.worktrees"
mkdir -p "$WT_PARENT"
WT_PATH="$WT_PARENT/$DIRNAME"

# Precondition 4: worktree doesn't already exist on disk.
if [ -d "$WT_PATH" ]; then
  fallback_context "the target worktree path already exists: $WT_PATH"
  exit 0
fi

# Auto-cut. Routed through the canonical ``lib.git_worktree.cut_worktree``
# helper (issue #322) so the safe-mode contract (fail closed when branch
# or dir exists; preserve pre-existing branches on failure) matches
# ``lib/execute.py`` and ``lib/acp_dispatch.py``. A future contract
# change must therefore touch one place, not three. The hook-level
# timeout (120s in hooks.json) bounds the whole operation; ``cut_worktree``
# itself is normally <2s. Note: no ``timeout(1)`` wrapper because macOS
# does not ship coreutils ``timeout`` by default — the wrapper would
# rc=127 on every Mac.
#
# We pass the four arguments via argv (not shell interpolation) so the
# heredoc body is literal text — no risk of a branch name containing
# shell metacharacters breaking the embedded Python. ``<<'PYEOF'``
# (with quoted delimiter) is the form that disables interpolation.
#
# Import path: the helper lives at ``<plugin_root>/lib/git_worktree.py``.
# We resolve the plugin root from the hook's own path
# (``${BASH_SOURCE[0]%/*}/..``) and prepend ``<plugin_root>/lib`` to
# ``PYTHONPATH`` so the embedded ``from git_worktree import cut_worktree``
# resolves regardless of the consumer project's cwd. The hook also
# passes ``<plugin_root>/lib`` via argv as a belt-and-suspenders second
# path entry — that handles the rare case where the shell strips
# ``PYTHONPATH`` on exec (e.g. some sandboxed invocations).
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LIB_DIR="$PLUGIN_ROOT/lib"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"

if ! python3 - "$REPO_ROOT" "$BRANCH" "$WT_PATH" "$MAIN_REF" "$LIB_DIR" <<'PYEOF'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
branch = sys.argv[2]
wt_path = Path(sys.argv[3])
base = sys.argv[4]
lib_dir = sys.argv[5]

# Belt-and-suspenders: ensure the helper is importable even when
# PYTHONPATH was stripped by the calling shell (rare on macOS/Linux
# but possible inside some sandbox invocations).
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
    # Surface the failure to the shell via a non-zero exit so the
    # enclosing `if !` triggers the fallback envelope. The exact
    # error text is preserved in cut_worktree's exception (the
    # helper passes git's stderr through verbatim).
    print(f"cut_worktree failed: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
then
  fallback_context "git worktree add failed for branch $BRANCH"
  exit 0
fi

# Bootstrap: run /dev-kit:log setup + /dev-kit:log on inside the new
# worktree so the delegated subagent's work is captured. Falls
# through silently if either script is missing (e.g. dev-kit plugin
# not yet installed in the consumer project).
LOG_SETUP="$PLUGIN_ROOT/skills/log/scripts/log-setup.sh"
LOG_ON="$PLUGIN_ROOT/skills/log/scripts/log-on.sh"

if [ -f "$LOG_SETUP" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_SETUP" >/dev/null 2>&1) || true
fi
if [ -f "$LOG_ON" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_ON" >/dev/null 2>&1) || true
fi

# Build additionalContext — the harness consumes this as a client-specific
# handoff envelope. The hook cannot call the host session or Agent API itself.
CTX="worktree auto-cut ready
  branch:  $BRANCH
  path:    $WT_PATH
  Claude Code next: open a new session in $WT_PATH
  Codex next: spawn a subagent with cwd=$WT_PATH and branch=$BRANCH
  handoff: pass the original task prompt and this worktree path to the client-specific worker
  fallback: if any of the above fails, re-run the canonical helper:
            python3 -c \"from lib.git_worktree import cut_worktree; cut_worktree(repo_root=Path('$REPO_ROOT'), branch='$BRANCH', worktree_path=Path('$WT_PATH'), base='$MAIN_REF')\""
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'
exit 0
