#!/usr/bin/env bash
# ralph_drive.sh — bash glue for /dev-kit:ralph
#
# Chains 4 gates + 1 attended execution phase. Reads/writes durable
# state at every hop via python3 -m skills.ralph.lib.ralph_state.
# NEVER invokes AskUserQuestion directly — that is the orchestrator's
# job, gated by the state machine's can_ask_question() invariant.
#
# Usage:
#   ./ralph_drive.sh init <idea> [--session NAME]
#   ./ralph_drive.sh status [--session NAME]
#   ./ralph_drive.sh can-ask [--session NAME]
#   ./ralph_drive.sh advance <target> [--action TEXT] [--session NAME]
#   ./ralph_drive.sh rewind <target> --reason TEXT [--session NAME]
#
# Exit codes:
#   0 — OK
#   1 — invalid CLI usage
#   2 — state machine rejected the operation (transition / rewind / can-ask blocked)
#   3 — environment error (missing python3, no project root, etc.)

set -eo pipefail

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_LIB="${SCRIPT_DIR}/../lib"
PROJECT_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SESSION="${RALPH_SESSION:-default}"

usage() {
    cat <<EOF
ralph_drive.sh — bash glue for /dev-kit:ralph state machine

Subcommands:
  init <idea>                     Create a new state for <idea>
  status                          Print current state JSON
  can-ask                         Exit 0 if AskUserQuestion allowed, 1 if locked
  advance <target> [--action T]   Transition current -> target (validated)
  rewind <target> --reason T      Rewind to a prior gate (Edit-then-approve)

Env vars:
  PROJECT_ROOT   Project root (default: git toplevel)
  RALPH_SESSION  Session name (default: default)

Examples:
  PROJECT_ROOT=. ./ralph_drive.sh init "add a hello-world skill"
  PROJECT_ROOT=. ./ralph_drive.sh status
  PROJECT_ROOT=. ./ralph_drive.sh can-ask
  PROJECT_ROOT=. ./ralph_drive.sh advance PROPOSAL_GATE --action "user approved research"
  PROJECT_ROOT=. ./ralph_drive.sh rewind PROPOSAL_GATE --reason "user edits A2"
EOF
}

require_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo "error: python3 not found in PATH" >&2
        exit 3
    fi
}

# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------

cmd_init() {
    local idea="$1"
    if [ -z "$idea" ]; then
        echo "error: init requires an <idea> argument" >&2
        exit 1
    fi
    require_python
    python3 -m skills.ralph.lib.ralph_state \
        --project-root "$PROJECT_ROOT" \
        --session "$SESSION" \
        init "$idea"
}

cmd_status() {
    require_python
    python3 -m skills.ralph.lib.ralph_state \
        --project-root "$PROJECT_ROOT" \
        --session "$SESSION" \
        show
}

cmd_can_ask() {
    require_python
    if python3 -m skills.ralph.lib.ralph_state \
        --project-root "$PROJECT_ROOT" \
        --session "$SESSION" \
        can-ask; then
        exit 0
    fi
    exit 2
}

cmd_advance() {
    local target="$1"
    shift
    local action=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --action)
                action="$2"
                shift 2
                ;;
            --session)
                SESSION="$2"
                shift 2
                ;;
            *)
                echo "error: unknown flag: $1" >&2
                exit 1
                ;;
        esac
    done
    if [ -z "$target" ]; then
        echo "error: advance requires a <target> stage" >&2
        exit 1
    fi
    require_python
    python3 -m skills.ralph.lib.ralph_state \
        --project-root "$PROJECT_ROOT" \
        --session "$SESSION" \
        transition "$target" --action "$action"
}

cmd_rewind() {
    local target=""
    local reason=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --reason)
                reason="$2"
                shift 2
                ;;
            --session)
                SESSION="$2"
                shift 2
                ;;
            *)
                if [ -z "$target" ]; then
                    target="$1"
                    shift
                else
                    echo "error: unexpected argument: $1" >&2
                    exit 1
                fi
                ;;
        esac
    done
    if [ -z "$target" ]; then
        echo "error: rewind requires a <target> gate" >&2
        exit 1
    fi
    require_python
    python3 -m skills.ralph.lib.ralph_state \
        --project-root "$PROJECT_ROOT" \
        --session "$SESSION" \
        rewind "$target" --reason "$reason"
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

sub="$1"
shift

case "$sub" in
    init)      cmd_init "$@" ;;
    status)    cmd_status "$@" ;;
    can-ask)   cmd_can_ask "$@" ;;
    advance)   cmd_advance "$@" ;;
    rewind)    cmd_rewind "$@" ;;
    -h|--help|help) usage; exit 0 ;;
    *)
        echo "error: unknown subcommand: $sub" >&2
        usage
        exit 1
        ;;
esac
