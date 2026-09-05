#!/usr/bin/env bash
# git-guard.sh — PreToolUse hook for Bash. Enforces branch strategy.
#
# Blocks (exit 2 with deny JSON):
#   1. `git commit` on main / master (incl. global flags: -C, -c, --git-dir,
#      --work-tree, --no-pager, --bare)
#   2. `git push` to main / origin main / HEAD:main / +main
#   3. `git push --force` (-f / --force). `--force-with-lease` allowed.
#   4. `git checkout main` / `git switch main` (primes a direct commit)
#   5. `git branch -D main|master` (deleting the protection itself)
#   6. `gh pr merge` (any invocation) — merging into main is always a
#      human action, run outside automation.
#
# Allows everything else. See .claude/rules/git-workflow.md for rationale.

set -uo pipefail
# Use %/* parameter expansion (POSIX, no external `dirname` required) so
# the source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — see TestGitGuardRefactor.fails_closed).
# shellcheck source=lib/payload-parse.sh
# shellcheck source=lib/slot-check.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/slot-check.sh"
require_jq git-guard
read_stdin_json git-guard
[ -z "$INPUT_JSON" ] && exit 0

CMD="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# PreToolUse runs in the client session's cwd, which may be the main checkout
# even when the command targets a worktree with `git -C <path>`. Use that
# explicit repository path for branch checks so valid worktree commits are
# not mistaken for commits on the parent's main branch.
GIT_CWD="${PWD}"

# A leading `cd <path> &&` / `cd <path>;` prefix changes the directory the
# rest of the command actually runs in — resolve GIT_CWD relative to it
# before the branch check, otherwise `cd <main-checkout> && git commit ...`
# is checked against the session's own cwd instead of where the commit
# really lands (issue #474).
if [[ "$CMD" =~ ^[[:space:]]*cd[[:space:]]+([^\;\&]+)[[:space:]]*(\&\&|\;) ]]; then
  CD_PATH="${BASH_REMATCH[1]}"
  # Trim trailing whitespace, then strip one layer of surrounding quotes.
  CD_PATH="${CD_PATH%"${CD_PATH##*[![:space:]]}"}"
  CD_PATH="${CD_PATH%\"}"; CD_PATH="${CD_PATH#\"}"
  CD_PATH="${CD_PATH%\'}"; CD_PATH="${CD_PATH#\'}"
  case "$CD_PATH" in
    /*) GIT_CWD="$CD_PATH" ;;
    *) GIT_CWD="${GIT_CWD%/}/${CD_PATH}" ;;
  esac
fi

if [[ "$CMD" =~ git[[:space:]]+-C[[:space:]]+([^[:space:]]+) ]]; then
  GIT_CWD="${BASH_REMATCH[1]}"
fi

# Skip commands that are neither git nor gh entirely.
case "$CMD" in
  *"git "*|*"gh "*) ;;
  *) exit 0 ;;
esac

# 0. Block `gh pr merge` (any invocation, any flags) — merging into main
# is always a human action. Checked before the git-only filter below so
# a bare `gh pr merge ...` (no "git " substring) still gets caught. Both
# anchors allow a command separator (;, &&, ||, |) with no space, so
# `cd x && gh pr merge 1 --auto` and `gh pr merge;echo done` are both
# denied, not just the space-separated forms.
if printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+merge([;&|[:space:]]|$)'; then
  deny "GIT GUARD" "gh pr merge is forbidden — merging into main must be done by a human (via the GitHub UI or gh pr merge run outside automation), not by an agent."
fi

# Helper: current branch (empty if detached HEAD or not a git repo).
current_branch() {
  git -C "$GIT_CWD" symbolic-ref --short HEAD 2>/dev/null || true
}

# M1: Strip GLOBAL git options so the verb-extraction patterns below match
# the actual git subcommand. Handles:
#   -C <path>         short flag with separate value
#   -c <key>=<val>    short flag with separate value (or -c<key>=<val> attached)
#   --git-dir <path>  long flag with separate value (or --git-dir=<path> attached)
#   --work-tree <path>
#   --exec-path <path>
#   --no-pager, --bare, --help, --version  (self-contained, no value)
# Unknown -X short flags are skipped (next token is consumed as value
# UNLESS the next token starts with `-` or is a known verb — heuristic to
# avoid eating a real verb as a flag value).
strip_git_globals() {
  local cmd="$1"
  read -ra toks <<< "$cmd"
  [ "${toks[0]:-}" = "git" ] || { echo "$cmd"; return 0; }
  local out="git" in_verb=0 i=1
  while [ $i -lt ${#toks[@]} ]; do
    local t="${toks[$i]}"
    if [ "$in_verb" = "1" ]; then
      out="$out $t"
      i=$((i+1))
      continue
    fi
    case "$t" in
      --no-pager|--bare|--help|--version)
        # Self-contained flag with no value.
        i=$((i+1)) ;;
      -C|-c|--git-dir|--work-tree|--exec-path)
        # Flag with separate value (consume this token + next as the value).
        i=$((i+2)) ;;
      --git-dir=*|--work-tree=*|--exec-path=*|-c*|-C*)
        # Attached value (e.g. -cmain, -C., --git-dir=foo).
        i=$((i+1)) ;;
      -*)
        # Unknown short flag. Heuristic: peek at the next token.
        # If it starts with `-` or is a known verb, this flag has no value.
        local nxt="${toks[$((i+1))]:-}"
        case "$nxt" in
          -*)
            i=$((i+1)) ;;
          commit|push|checkout|switch|branch|log|status|diff|show|fetch|pull|rebase|reset|tag|remote|merge|cherry-pick|revert|clean|stash|init|clone|add|mv|rm|config|shortlog|rerere|repack|gc|prune|fsck|reflog|restore|notes|range-diff|mailinfo|mailsplit|request-pull)
            i=$((i+1)) ;;
          *)
            i=$((i+2)) ;;
        esac
        ;;
      *)
        # First non-flag token → switch to verb mode.
        in_verb=1
        out="$out $t"
        i=$((i+1)) ;;
    esac
  done
  echo "$out"
}

# Normalize the command so the existing verb-extraction patterns below
# work for `git -C . commit`, `git --no-pager push origin main`, etc.
CMD="$(strip_git_globals "$CMD")"

# m1: dropped the dead `branch -d` arm — only `-D` has a denial check below.
write_pattern='(git[[:space:]]+commit|git[[:space:]]+push|git[[:space:]]+checkout|git[[:space:]]+switch|git[[:space:]]+branch[[:space:]]+-D)'
if ! printf '%s' "$CMD" | grep -qE "$write_pattern"; then
  exit 0
fi

# 1. Block git commit on main.
if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+commit'; then
  CUR=$(current_branch)
  if [ "$CUR" = "main" ] || [ "$CUR" = "master" ]; then
    deny "GIT GUARD" "direct commit to '$CUR' is forbidden. Cut a branch off origin/main first (see .claude/rules/git-workflow.md)."
  fi
fi

# 2. Block git push to main.
if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+push'; then
  # Heuristic: any push that names main / master on the remote side.
  # Catches: `git push origin main`, `git push origin HEAD:main`,
  # `git push origin +main`, `git push --force origin main`,
  # `git push origin main:master`, `git -C /repo push origin main`.
  if printf '%s' "$CMD" | grep -qE '(^|[[:space:]:/])(origin[[:space:]]+)?(\+)?(HEAD:)?(main|master)([[:space:]:/.]|$)|:main\b|:master\b'; then
    deny "GIT GUARD" "pushing to main is forbidden. Push to your feature branch: \`git push -u origin <type>/<slug>\`."
  fi
  # Block force-push. m5: bash-guard.sh only blocks force-push in strict mode
  # (DEV_KIT_STRICT=1) and only the `force+main` pattern — git-guard is the
  # only always-on block, so this check is the primary one.
  if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+push.*[[:space:]](-f|--force|--force-with-lease)([[:space:]]|$)'; then
    if printf '%s' "$CMD" | grep -qE -- '--force-with-lease'; then
      # --force-with-lease is allowed on your own unmerged branch; only block
      # if the push target is main (already caught above).
      :
    else
      deny "GIT GUARD" "force-push (-f/--force) is forbidden. Use --force-with-lease only on your own unmerged branch."
    fi
  fi
fi

# 3. Block `git checkout main` (or `git switch main`) — it primes a direct
#    commit to main in the next command. Allow `git checkout -b ...` (new branch).
if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+(checkout|switch)[[:space:]]'; then
  # Allow `git checkout -b`, `git checkout <commit>`, `git checkout <file>`,
  # and the file-restore form `git checkout <ref> -- <path>` (a `--` token
  # appearing anywhere after the ref never changes HEAD, so it must not be
  # treated as a branch switch — issue #471).
  if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+(checkout|switch)[[:space:]]+(-b|-c|-[0-9]+[[:space:]]|[a-f0-9]{7,}[[:space:]]|--)' \
     || printf '%s' "$CMD" | grep -qE 'git[[:space:]]+(checkout|switch)[[:space:]]+[^[:space:]]+[[:space:]]+--([[:space:]]|$)'; then
    :
  elif printf '%s' "$CMD" | grep -qE 'git[[:space:]]+(checkout|switch)[[:space:]]+(main|master)([[:space:]]|$)'; then
    deny "GIT GUARD" "switching to main in this checkout is forbidden. Use a worktree instead: \`git worktree add -b <type>/<slug> .worktrees/<slug> origin/main\`."
  fi
fi

# 4. Block `git branch -D` on main (deleting the protection itself).
if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+branch[[:space:]]+-D'; then
  if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+branch[[:space:]]+-D[[:space:]]+(main|master)([[:space:]]|$)'; then
    deny "GIT GUARD" "deleting main/master with -D is forbidden."
  fi
fi

# 5. Slot freshness check on `git push` to a feature branch.
#    Slot = origin/main's plugin.json version. For parallel PRs, add
#    PR_index; the parallel-PR variant lives in worktree-guard.sh.
_verify_slot() {
  local branch_name="" expected="" actual=""
  branch_name="$(git -C "$GIT_CWD" symbolic-ref --short HEAD 2>/dev/null)" || return 0
  [ -n "$branch_name" ] || return 0
  expected="$(git show origin/main:.claude-plugin/plugin.json 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['version'])" 2>/dev/null)" || return 0
  [ -n "${expected:-}" ] || return 0
  # Check BOTH plugin.json manifests (Claude + Codex). They must
  # both be pinned to the same expected slot — the version-bump
  # workflow on main keeps them in lockstep, and a Codex-only
  # plugin.json drift would let a Codex sub-agent push a stale slot
  # even after the Claude manifest is re-pinned. The deny predicate
  # is in hooks/lib/slot-check.sh so the truth table is unit-tested
  # independently of this PreToolUse hook.
  local actual_claude="" actual_codex=""
  # Resolve manifests from the repository selected above. The hook process
  # can start in the main checkout while the command targets a worktree via
  # `git -C`; relative opens here previously reported the main checkout's
  # release slot and falsely denied valid worktree pushes.
  actual_claude="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$GIT_CWD/.claude-plugin/plugin.json" 2>/dev/null)" || actual_claude=""
  actual_codex="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$GIT_CWD/.codex-plugin/plugin.json" 2>/dev/null)" || actual_codex=""
  if slot_should_deny "$actual_claude" "$actual_codex" "$expected"; then
    deny "GIT GUARD" "plugin.json versions are stale. claude=$actual_claude codex=${actual_codex:-<missing>} expected=$expected (origin/main). Rebase onto origin/main, re-pin BOTH .claude-plugin/plugin.json AND .codex-plugin/plugin.json to $expected, then push again."
  fi
}
# Fire on any `git push` the main-push block above did not deny.
if printf '%s' "$CMD" | grep -qE 'git[[:space:]]+push'; then
  _verify_slot
fi

exit 0
