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
