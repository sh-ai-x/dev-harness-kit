#!/usr/bin/env bash
# mode-gate.sh — enforce DEV_KIT_MODE at the manifest boundary.
#
# The hook manifests are loaded by the host before any hook shell runs.  Keep
# mode selection in one executable boundary so every registered hook observes
# the same full/lite/undev policy without duplicating resolver boilerplate in
# each hook implementation.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  printf 'mode-gate.sh: hook path is required\n' >&2
  exit 2
fi

HOOK_PATH="$1"
shift
HOOK_DIR="${BASH_SOURCE[0]%/*}"
case "$HOOK_PATH" in
  "$HOOK_DIR"/*.sh) ;;
  *)
    printf 'mode-gate.sh: refusing hook outside %s: %s\n' "$HOOK_DIR" "$HOOK_PATH" >&2
    exit 2
    ;;
esac

# shellcheck source=lib/mode-resolve.sh
source "$HOOK_DIR/lib/mode-resolve.sh"
ACTIVE_MODE="$(dev_kit_mode_active)"
HOOK_NAME="${HOOK_PATH##*/}"
HOOK_NAME="${HOOK_NAME%.sh}"

case "$ACTIVE_MODE" in
  full)
    ;;
  lite)
    # Seven low-friction safety/verification hooks form the documented lite
    # subset. All other hooks are silent no-ops in lite mode.
    case "$HOOK_NAME" in
      acp-tier-assert|tdd-guard|worktree-guard|destructive-confirm|bash-guard|git-guard|stop-verify)
        ;;
      *) exit 0 ;;
    esac
    ;;
  undev)
    exit 0
    ;;
  *)
    printf 'mode-gate.sh: invalid resolved mode: %s\n' "$ACTIVE_MODE" >&2
    exit 2
    ;;
esac

exec bash "$HOOK_PATH" "$@"
