#!/usr/bin/env bash
# mode-resolve.sh — shared DEV_KIT_MODE resolution for hooks.
#
# Single source of truth for "which mode is the current session in?".
# Sourced (not executed) by every hook that wants to short-circuit when
# its required mode doesn't match the active mode.
#
# Resolution order (highest wins), matching docs/scopes/modes.md:
#   1. $DEV_KIT_MODE shell env var  — per-session override
#   2. <proj>/.claude/settings.json env.DEV_KIT_MODE — committed project choice
#   3. <proj>/.claude/settings.local.json env.DEV_KIT_MODE — personal override
#   4. Default = "full" ONLY when enabledPlugins.dev-kit@dev-kit: true;
#      otherwise "undev" (silent — plugin not loaded)
#
# Public API:
#   dev_kit_mode_resolve            — sets $DEV_KIT_MODE to one of
#                                    "full", "lite", "undev".
#   dev_kit_mode_require <required> — exits 0 if the active mode matches
#                                    <required> (which may be a comma-
#                                    separated list); exits 0 silently
#                                    otherwise. Fail-closed on jq missing.
#   dev_kit_mode_active             — echo the active mode (no-op if
#                                    not yet resolved).
#
# Hooks that ONLY run in full mode add at the top:
#     source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
#     dev_kit_mode_require full
# exit 0
#
# Hooks that run in BOTH full and lite (the "lite subset") add:
#     dev_kit_mode_require full,lite
# exit 0
#
# Hooks that are always-on (no mode gate) do not source this file.

set -eo pipefail

# require_jq — like lib/payload-parse.sh's require_jq. Fail closed.
require_jq_mode() {
  if ! command -v jq >/dev/null 2>&1; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"mode-resolve.sh: jq is required for DEV_KIT_MODE gating. Install jq (apt/brew/apk)."}}\n' >&2
    exit 2
  fi
}

# _read_settings_env <path> <key> — read `.<key>` from a JSON file's top
# level OR its `env` block, returning empty if the file is missing or
# malformed. Best-effort — never raises.
_read_settings_env() {
  local path="$1" key="$2"
  [ -f "$path" ] || return 0
  jq -r --arg k "$key" '.. | objects | select(has($k)) | .[$k] // empty' "$path" 2>/dev/null \
    | grep -m1 . || true
}

# dev_kit_mode_resolve — compute the active mode.
#
# Reads from the three layers in priority order. Sets $DEV_KIT_MODE
# globally for the rest of the shell session. Idempotent (safe to call
# multiple times; later calls re-resolve from current state).
dev_kit_mode_resolve() {
  require_jq_mode

  # Layer 1: shell env var wins
  if [ -n "${DEV_KIT_MODE:-}" ]; then
    DEV_KIT_MODE="${DEV_KIT_MODE}"
    export DEV_KIT_MODE
    return 0
  fi

  # Find the project root (.claude lives there). If we're outside any
  # project, default to "undev" — no plugin, no hooks.
  local proj_root
  proj_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$proj_root" ] || [ ! -d "$proj_root/.claude" ]; then
    DEV_KIT_MODE="undev"
    export DEV_KIT_MODE
    return 0
  fi

  # Layer 2: project-scope .claude/settings.json
  local proj_mode
  proj_mode="$(_read_settings_env "$proj_root/.claude/settings.json" "DEV_KIT_MODE")"
  if [ -n "$proj_mode" ]; then
    DEV_KIT_MODE="$proj_mode"
    export DEV_KIT_MODE
    return 0
  fi

  # Layer 3: local-scope .claude/settings.local.json
  local local_mode
  local_mode="$(_read_settings_env "$proj_root/.claude/settings.local.json" "DEV_KIT_MODE")"
  if [ -n "$local_mode" ]; then
    DEV_KIT_MODE="$local_mode"
    export DEV_KIT_MODE
    return 0
  fi

  # Layer 4: conditional default — `full` only when the plugin is
  # actually enabled at project scope. Otherwise `undev`.
  local enabled
  enabled="$(jq -r '.enabledPlugins // {} | keys | map(select(test("dev-kit@"))) | length' \
    "$proj_root/.claude/settings.json" 2>/dev/null || echo 0)"
  if [ "${enabled:-0}" -gt 0 ]; then
    DEV_KIT_MODE="full"
  else
    DEV_KIT_MODE="undev"
  fi
  export DEV_KIT_MODE
  return 0
}

# dev_kit_mode_active — echo the active mode. Resolves first if needed.
dev_kit_mode_active() {
  if [ -z "${DEV_KIT_MODE:-}" ]; then
    dev_kit_mode_resolve
  fi
  printf '%s' "${DEV_KIT_MODE:-undev}"
}

# dev_kit_mode_require <required> — short-circuit hook body when the
# active mode does NOT match any of the comma-separated modes in
# <required>. Exits 0 silently on no-match (hook does nothing).
#
# Examples:
#   dev_kit_mode_require full           # full only
#   dev_kit_mode_require full,lite      # shared subset
#   dev_kit_mode_require lite,undev     # never (sanity check)
dev_kit_mode_require() {
  local required="$1"
  local active
  active="$(dev_kit_mode_active)"
  local IFS=','
  for r in $required; do
    # Strip whitespace
    r="${r// /}"
    [ "$r" = "$active" ] && return 0
  done
  exit 0
}

# Backward-compat alias (older code may use require_mode)
require_mode() {
  dev_kit_mode_require "$@"
}
