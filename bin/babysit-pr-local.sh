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

# --- live tail file + archive ------------------------------------------
# Persistence contract (matches the user's "viewer is read-only, refresh
# shows the latest persisted run" requirement):
#
#   .review-local-archive/<PR>/<timestamp>-$/log   -- permanent per-run log
#   .review-local-current/<PR>.log                 -- symlink to the
#                                                    latest archived log
#
# The viewer at http://127.0.0.1:8766/pr/<N> serves this single log
# file. Opening the URL never triggers execution -- it's pure
# read-only. On page refresh, the operator sees the SAME log (the
# most recent completed run's full output, including final verdict).
# The babysit skill is what starts new runs and rotates the symlink;
# the viewer just renders whatever file the symlink points at.
#
# Why a symlink (not a truncate-and-rewrite): truncating the live
# file from one browser tab while another is mid-render breaks the
# viewer's SSE stream and can leave the page on a partial view.
# Atomically rotating the symlink (`ln -sf` + `rm` of the old link)
# means readers on the old symlink keep seeing the old log until
# their next reload, and the new symlink targets the new log
# immediately. No torn writes, no partial renders.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && git rev-parse --show-toplevel 2>/dev/null || true)"
fi
RUN_LOG=""
LIVE_DIR=""
LIVE_LINK=""
if [ -n "$REPO_ROOT" ]; then
  LIVE_DIR="${REPO_ROOT}/.review-local-current"
  mkdir -p "$LIVE_DIR"
  LIVE_LINK="${LIVE_DIR}/${PR_NUMBER}.log"
  RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  RUN_LOG="${REPO_ROOT}/.review-local-archive/${PR_NUMBER}/${RUN_TS}/log"
  mkdir -p "$(dirname "$RUN_LOG")"
  : > "$RUN_LOG"
  # Point the viewer at the new run's log BEFORE we tee a single line,
  # so a concurrent viewer reload during the run sees the new file.
  # Remove any leftover regular file from a prior babysit that didn't
  # use the symlink pattern -- `ln -sfn` would otherwise fail silently
  # when the target already exists as a regular file.
  [ -L "$LIVE_LINK" ] || [ ! -e "$LIVE_LINK" ] || rm -f "$LIVE_LINK"
  ln -sfn "$RUN_LOG" "$LIVE_LINK"
  printf 'babysit-pr-local: started PR=%s pid=%s run=%s url=http://127.0.0.1:8766/pr/%s\n' \
    "$PR_NUMBER" "$$" "$RUN_TS" "$PR_NUMBER" >> "$RUN_LOG"

  # Auto-open the HTML viewer in the operator's default browser. The
  # viewer is at http://127.0.0.1:8766/pr/<N> (served by
  # bin/review-local-server.py). It is a read-only snapshot renderer
  # -- opening it does NOT trigger execution; it just shows the
  # latest persisted log via the same symlink rotation the live
  # tail file uses.
  #
  # - `open <url>` on macOS dispatches to the default browser. On
  #   non-macOS, fall back to `xdg-open` (Linux) or `wslview` (WSL).
  # - In CI / non-TTY contexts (`$CI=1`, `$(tty)` empty, or
  #   `DISPLAY` unset on a Linux server with no X), the commands
  #   silently fail; we don't care -- the operator can still open
  #   the URL manually from the stdout banner above.
  # - Server may not be running yet on a fresh worktree; warn but
  #   don't fail (the next iteration's `curl` will surface this).
  if [ -z "${CI:-}" ] && [ -n "${DISPLAY:-${WAYLAND_DISPLAY:-}}" ] || command -v open >/dev/null 2>&1; then
    VIEWER_URL="http://127.0.0.1:8766/pr/$PR_NUMBER"
    if command -v curl >/dev/null 2>&1; then
      if curl -sS -o /dev/null -w "%{http_code}" --max-time 2 "$VIEWER_URL" 2>/dev/null | grep -q '^200$'; then
        if command -v open >/dev/null 2>&1; then
          open "$VIEWER_URL" 2>/dev/null || true
        elif command -v xdg-open >/dev/null 2>&1; then
          xdg-open "$VIEWER_URL" 2>/dev/null || true
        fi
      else
        printf '  viewer not yet serving on %s (start bin/review-local-server.py to enable auto-open)\n' "$VIEWER_URL" >> "$RUN_LOG"
      fi
    fi
  fi
fi

# --- delegate to bin/review-local.sh -----------------------------------
# `exec` replaces the wrapper process with the downstream script; the
# downstream's exit code becomes the wrapper's exit code 1:1, so the
# babysit iteration loop's TERMINATE / iterate branches fire
# deterministically (exit 0 = Approve / exit 1 = Changes|Blocked).
# The tee wrapper mirrors stdout+stderr to the live tail file so the
# SSE viewer sees the same line-by-line output the operator sees in
# their terminal. Without `tee` the server's tail-f would never see
# new content from this skill — that was the coupling gap before.
#
# Depends on `set -o pipefail` (line 27): without it, `tee` always
# exits 0 and the wrapper would silently always exit 0, breaking the
# babysit iteration loop's TERMINATE/iterate branches (the operator
# would never see Changes|Blocked escape to the iterate path).
if [ -n "$RUN_LOG" ]; then
  exec "$SCRIPT_DIR/review-local.sh" --pr "$PR_NUMBER" 2>&1 \
    | tee -a "$RUN_LOG"
else
  exec "$SCRIPT_DIR/review-local.sh" --pr "$PR_NUMBER"
fi
