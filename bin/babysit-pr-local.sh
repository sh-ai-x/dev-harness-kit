#!/usr/bin/env bash
# bin/babysit-pr-local.sh — single-call wrapper for /dev-kit:babysit-pr-local.
#
# Routes the iteration step that would normally call `gh pr checks --watch`
# (a GH-Actions wait) into `bin/review-local.sh --pr N` instead, so the
# local LLM-judge verdict (`/dev-kit:review` + `/dev-kit:security` +
# `/dev-kit:maintenance`) drives iteration in place of the GH-Actions
# review verdict. Saves GH-Actions minutes when a private repo has hit
# its monthly cap.
#
# Returns the verdict script's exit code:
#   0  = Approve  (loop terminates)
#   1  = Changes Requested or Blocked  (loop iterates)
#   2  = parse failure or operator error (loop exits 1)
#
# MUST-NO-SKIP: refuses any `--auto-approve` flag. The babysit variant
# never auto-merges; the operator runs `gh pr merge` manually after the
# audit comment shows `verdict=Approve`. This scan is enforced at three
# layers (wrapper arg scan + downstream script's own --auto-approve
# branch in `bin/review-local.sh` + audit trail in the comment body).
#
# Usage:
#   bin/babysit-pr-local.sh <PR_NUMBER>
#
# Example:
#   bin/babysit-pr-local.sh 605
#
# Viewer auto-wiring (best-effort, never blocks the verdict pipeline):
# ensures `bin/review-local-server.py` is running on 127.0.0.1:8765,
# opens (once per PR per hour) a browser tab at
# `/pr/<N>?autostart=1`, and tees this run's stdout into
# `.dev-kit/babysit-pr-local-live.log` so the server's `/pr/<N>/tail`
# route can mirror it in real time WITHOUT spawning a second,
# duplicate `bin/review-local.sh` run. Opt out with
# `BABYSIT_NO_VIEWER=1`; auto-skipped when `$CI` is set or `curl` is
# missing.
set -euo pipefail

# --- arg validation ----------------------------------------------------
if [[ $# -lt 1 ]]; then
  echo "usage: $0 <PR_NUMBER>" >&2
  echo "       calls bin/review-local.sh --pr \$PR_NUMBER" >&2
  exit 2
fi

# MUST-NO-SKIP enforcement: refuse any --auto-appearing flag in argv
# BEFORE the numeric check. The --auto-approving refusal is the
# primary defense; running the numeric check first would surface
# `--auto-approve 123` as "PR_NUMBER must be numeric" instead of the
# clear "auto-approve forbidden" message operators need.
# bin/review-local.sh also refuses the flag as a belt-and-suspenders
# backstop.
for arg in "$@"; do
  case "$arg" in
    --auto-approve|--auto|--approve)
      echo "error: babysit-pr-local must NOT pass $arg to review-local.sh" >&2
      echo "       (operator-driven merging is the contract;" >&2
      echo "        use bin/review-local.sh --auto-approve directly)" >&2
      exit 2
      ;;
  esac
done

PR_NUMBER="$1"

if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "error: PR_NUMBER must be numeric (got '$PR_NUMBER')" >&2
  exit 2
fi

# SCRIPT_DIR resolves to the directory holding THIS script at runtime,
# so the lookup stays valid when the wrapper is invoked from any cwd.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

# --- per-PR concurrency lock (machine-wide, cross-worktree) -----------
# A second babysit-pr-local invocation on the same PR would run its
# own review-local.sh, write to the same live log, and possibly push
# mid-fixup of the first. Refuse early with a one-line diagnostic that
# surfaces the holder's PID + branch + ISO timestamp; the operator can
# then kill the previous run or wait for it. Stale locks (PID gone OR
# TTL exceeded) are removed via lib/babysit_pr_reliability.is_stale_lock.
#
# Lock path encodes the PR number, so two parallel wrappers on
# DIFFERENT PRs do NOT collide -- only same-PR duplicates are
# blocked. The lock dir defaults to `<git-common-dir>/dev-kit` so
# every worktree in this repo shares the same per-PR namespace (a
# second terminal in a different worktree hitting the same PR still
# triggers "already running"). `BABYSIT_LOCK_PARENT` overrides the
# parent directory for hermetic tests so they don't race with a
# developer's own babysit session running against the real project
# repo.
LOCK_PARENT="${BABYSIT_LOCK_PARENT:-}"
if [[ -z "$LOCK_PARENT" ]]; then
  GIT_COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
  if [[ -n "$GIT_COMMON_DIR" ]]; then
    # `--git-common-dir` returns a relative path when run inside the
    # main checkout; resolve to absolute so subsequent `mkdir -p`
    # and lock writes do not depend on cwd.
    case "$GIT_COMMON_DIR" in
      /*) ABS_COMMON="$GIT_COMMON_DIR" ;;
      *)  ABS_COMMON="$(cd "$REPO_ROOT" && cd "$GIT_COMMON_DIR" 2>/dev/null && pwd || echo "$REPO_ROOT/.git")" ;;
    esac
    LOCK_PARENT="$ABS_COMMON"
  else
    # Not a git checkout (defensive default) -- fall back to the
    # in-repo .git so the lock still gates duplicate runs on the
    # same machine (this script is meant to be invoked from inside
    # the project repo, so the fallback is rarely exercised).
    LOCK_PARENT="$REPO_ROOT/.git"
  fi
fi
LOCK_DIR="$LOCK_PARENT/dev-kit"
PR_LOCK_PATH="$LOCK_DIR/babysit-pr-local-${PR_NUMBER}.lock"
mkdir -p "$LOCK_DIR" 2>/dev/null || true

if [[ -f "$PR_LOCK_PATH" ]]; then
  if python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
import babysit_pr_reliability as bpr
sys.exit(0 if bpr.is_stale_lock('$PR_LOCK_PATH') else 1)
" 2>/dev/null; then
    echo "stale pr lock removed: $PR_LOCK_PATH" >&2
    rm -f "$PR_LOCK_PATH"
    # Also clear any stale lockdir from a prior crashed run.
    rm -rf "${PR_LOCK_PATH}.d"
  else
    # `set -e` propagates into `$(...)`; `read_pr_lock_body` already
    # collapses read failures to "" but the python3 call could still
    # exit non-zero on an unhandled exception. `|| true` makes the
    # assignment bulletproof so the diagnostic always prints.
    HOLDER="$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
import babysit_pr_reliability as bpr
sys.stdout.write(bpr.read_pr_lock_body('$PR_LOCK_PATH'))
" 2>/dev/null || true)"
    HOLDER="${HOLDER:-<unreadable>}"
    echo "already running babysit-pr-local for PR #${PR_NUMBER}: ${HOLDER}" >&2
    exit 1
  fi
fi
# Atomic acquire (closes the TOCTOU race the local security judge
# flagged in PR #766 — the previous `[[ -f ]]` + `>` pattern let two
# concurrent invocations both pass the check before either wrote the
# lock, producing interleaved log writes and possibly conflicting
# pushes). `try_acquire_pr_lock` uses `mkdir` of a sibling
# `${PR_LOCK_PATH}.d` directory as the atomic primitive (POSIX
# guarantees the directory either exists or doesn't after mkdir
# returns — two concurrent mkdir calls cannot both succeed).
LOCK_BODY="$(date -Iseconds) pid=$$ branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown') source=babysit-pr-local pr=${PR_NUMBER}"
if ! python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
import babysit_pr_reliability as bpr
sys.exit(0 if bpr.try_acquire_pr_lock('$PR_LOCK_PATH', '''$LOCK_BODY''') else 1)
" 2>/dev/null; then
  HOLDER="$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/../lib')
import babysit_pr_reliability as bpr
sys.stdout.write(bpr.read_pr_lock_body('$PR_LOCK_PATH'))
" 2>/dev/null || true)"
  HOLDER="${HOLDER:-<unreadable>}"
  echo "already running babysit-pr-local for PR #${PR_NUMBER}: ${HOLDER}" >&2
  exit 1
fi
trap 'rm -f "$PR_LOCK_PATH" && rm -rf "${PR_LOCK_PATH}.d"' EXIT

# --- auto-launch the localhost HTML viewer (best-effort) ---------------
# `bin/review-local-server.py` (PR #731) exposes a live-streaming HTML
# page for `bin/review-local.sh`, but nothing wired it to
# babysit-pr-local automatically -- operators had to hand-start the
# server and open the tab themselves, so the SKILL.md's documented
# "external trigger" flow never actually fired. This block does both,
# and tees this run's stdout into
# `.dev-kit/babysit-pr-local-live.log` so the server's read-only
# `/pr/<N>/tail` route (see bin/review-local-server.py's
# _tail_babysit_log -- it NEVER spawns review-local.sh) can mirror
# this exact run in real time instead of triggering a second,
# duplicate verdict pipeline.
#
# Best-effort throughout: a missing `review-local-server.py` (e.g. the
# hermetic wrapper tests copy only this script + a fake
# review-local.sh into a tmpdir), a missing `curl`/`open`, or a
# CI/headless environment must never block the verdict pipeline below.
LIVE_LOG="$REPO_ROOT/.dev-kit/babysit-pr-local-live.log"
VIEWER_PORT="${BABYSIT_VIEWER_PORT:-8765}"
mkdir -p "$(dirname "$LIVE_LOG")"
: > "$LIVE_LOG"

if [[ -z "${BABYSIT_NO_VIEWER:-}" && -z "${CI:-}" ]] && command -v curl >/dev/null 2>&1; then
  if ! curl -fsS --max-time 1 "http://127.0.0.1:$VIEWER_PORT/healthz" >/dev/null 2>&1; then
    SERVER_SCRIPT="$SCRIPT_DIR/review-local-server.py"
    if [[ -x "$SERVER_SCRIPT" ]]; then
      nohup "$SERVER_SCRIPT" --port "$VIEWER_PORT" >/dev/null 2>&1 &
      disown
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -fsS --max-time 1 "http://127.0.0.1:$VIEWER_PORT/healthz" >/dev/null 2>&1 && break
        sleep 0.3
      done
    fi
  fi

  if curl -fsS --max-time 1 "http://127.0.0.1:$VIEWER_PORT/healthz" >/dev/null 2>&1; then
    # Only pop a new tab once per PR per hour -- babysit calls this
    # wrapper once per LOOP iteration (SKILL.md step 4L), and popping
    # a fresh tab on every iteration would spam the operator's browser.
    VIEWER_MARKER="$REPO_ROOT/.dev-kit/babysit-pr-local-viewer-opened.$PR_NUMBER"
    MARKER_AGE=3601
    if [[ -f "$VIEWER_MARKER" ]]; then
      MARKER_MTIME="$(stat -f %m "$VIEWER_MARKER" 2>/dev/null || stat -c %Y "$VIEWER_MARKER" 2>/dev/null || echo 0)"
      MARKER_AGE=$(( $(date +%s) - MARKER_MTIME ))
    fi
    if [[ "$MARKER_AGE" -gt 3600 ]]; then
      VIEWER_URL="http://127.0.0.1:$VIEWER_PORT/pr/$PR_NUMBER?autostart=1"
      if command -v open >/dev/null 2>&1; then
        open "$VIEWER_URL" >/dev/null 2>&1 && touch "$VIEWER_MARKER" 2>/dev/null || true
      elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$VIEWER_URL" >/dev/null 2>&1 && touch "$VIEWER_MARKER" 2>/dev/null || true
      fi
    fi
  fi
fi

# --- run bin/review-local.sh, mirrored into the live log --------------
# No longer `exec`: the sentinel append below must run AFTER
# review-local.sh exits, so the wrapper stays alive and propagates the
# exit code explicitly (via PIPESTATUS) instead of via process
# replacement. `set +e` / `set -e` bracket the pipeline so a non-zero
# pipeline status (Changes Requested / Blocked -> exit 1) doesn't trip
# `set -e` before RC is captured and the sentinel is written.
set +e
"$SCRIPT_DIR/review-local.sh" --pr "$PR_NUMBER" 2>&1 | tee -a "$LIVE_LOG"
RC=${PIPESTATUS[0]}
set -e
echo "##BABYSIT-DONE exit_code=$RC##" >> "$LIVE_LOG"
exit "$RC"
