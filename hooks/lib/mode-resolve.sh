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
#   dev_kit_mode_active             — echo the active mode (no-op if
#                                    not yet resolved).
#   dev_kit_mode_require <required> — GATE helper (NOT a boolean). See
#                                    full comment below.
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

# require_jq_mode — fail-closed contract. Emits the JSON denial on
# STDOUT (Claude Code hook protocol reads JSON from stdout) and a
# one-line reason on STDERR for human visibility.
require_jq_mode() {
  if ! command -v jq >/dev/null 2>&1; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"mode-resolve.sh: jq is required for DEV_KIT_MODE gating. Install jq (apt/brew/apk)."}}\n'
    printf 'mode-resolve.sh: jq missing — fail-closed\n' >&2
    exit 2
  fi
}

# _is_valid_mode <value> — returns 0 iff <value> ∈ {full,lite,undev}.
# Used to reject typos before they silently fail-open every gating hook
# (a missing match in dev_kit_mode_require's loop silently exits 0).
_is_valid_mode() {
  case "${1:-}" in
    full|lite|undev) return 0 ;;
    *) return 1 ;;
  esac
}

# _read_settings_env <path> <key> — read DEV_KIT_MODE from a JSON
# settings file. Only matches the documented 2 locations: top-level OR
# `.env` block. Returns empty if the file is missing, the key is
# absent, or jq fails.
_read_settings_env() {
  local path="$1" key="$2"
  [ -f "$path" ] || return 0
  jq -r --arg k "$key" '(.env[$k] // .[$k] // empty)' "$path" 2>/dev/null \
    | grep -m1 . || true
}

# _warn_invalid <layer> <value> — one-line stderr warning when a layer
# held an unparseable mode value. Honors a rate-limit (one warning per
# layer per resolve call) so a corrupted settings file doesn't spam.
_warn_invalid() {
  local layer="$1" value="$2"
  printf 'mode-resolve.sh: ignoring invalid %s DEV_KIT_MODE=%q\n' "$layer" "$value" >&2
}

dev_kit_mode_resolve() {
  require_jq_mode

  # Layer 1: shell env var wins
  if [ -n "${DEV_KIT_MODE:-}" ] && _is_valid_mode "${DEV_KIT_MODE}"; then
    export DEV_KIT_MODE
    DEV_KIT_MODE_SOURCE="shell"
    export DEV_KIT_MODE_SOURCE
    return 0
  fi
  if [ -n "${DEV_KIT_MODE:-}" ]; then
    _warn_invalid "shell" "$DEV_KIT_MODE"
    unset DEV_KIT_MODE
  fi

  # Find the project root (.claude lives there). If we're outside any
  # project, default to "undev" — no plugin, no hooks.
  local proj_root
  proj_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$proj_root" ] || [ ! -d "$proj_root/.claude" ]; then
    DEV_KIT_MODE="undev"
    export DEV_KIT_MODE
    DEV_KIT_MODE_SOURCE="outside-git"
    export DEV_KIT_MODE_SOURCE
    return 0
  fi

  # Layer 2: project-scope .claude/settings.json
  local proj_mode
  proj_mode="$(_read_settings_env "$proj_root/.claude/settings.json" "DEV_KIT_MODE")"
  if _is_valid_mode "$proj_mode"; then
    DEV_KIT_MODE="$proj_mode"
    export DEV_KIT_MODE
    DEV_KIT_MODE_SOURCE="project"
    export DEV_KIT_MODE_SOURCE
    return 0
  fi
  [ -n "$proj_mode" ] && _warn_invalid "project" "$proj_mode"

  # Layer 3: local-scope .claude/settings.local.json
  local local_mode
  local_mode="$(_read_settings_env "$proj_root/.claude/settings.local.json" "DEV_KIT_MODE")"
  if _is_valid_mode "$local_mode"; then
    DEV_KIT_MODE="$local_mode"
    export DEV_KIT_MODE
    DEV_KIT_MODE_SOURCE="local"
    export DEV_KIT_MODE_SOURCE
    return 0
  fi
  [ -n "$local_mode" ] && _warn_invalid "local" "$local_mode"

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
  DEV_KIT_MODE_SOURCE="default"
  export DEV_KIT_MODE_SOURCE
  return 0
}

dev_kit_mode_active() {
  if [ -z "${DEV_KIT_MODE:-}" ]; then
    dev_kit_mode_resolve
  fi
  printf '%s' "${DEV_KIT_MODE:-undev}"
}

# dev_kit_mode_require <required> — GATE helper for hooks.
#
# IMPORTANT: returns 0 on BOTH MATCH and NO-MATCH.
#   - MATCH (active mode ∈ <required>): caller continues into the hook
#     body.
#   - NO-MATCH (active mode ∉ <required>): caller MUST NOT continue;
#     this function calls `exit 0` to short-circuit the entire hook.
#
# Always call as the LAST statement before the hook body. Example:
#     source .../mode-resolve.sh
#     dev_kit_mode_require full
#     # hook body below — only reached when active mode == "full"
#
# To avoid the footgun, prefer `dev_kit_mode_gate <required>` (alias)
# which reads as a gate rather than a require.
dev_kit_mode_require() {
  local required="$1"
  local active
  active="$(dev_kit_mode_active)"
  local IFS=','
  for r in $required; do
    r="${r// /}"
    [ "$r" = "$active" ] && return 0
  done
  exit 0
}

# dev_kit_mode_gate <required> — alias of dev_kit_mode_require with
# gate-flavored naming. Same call-and-forget semantics.
dev_kit_mode_gate() {
  dev_kit_mode_require "$@"
}

# require_mode — backward-compat alias for older callers. Same as
# dev_kit_mode_require / dev_kit_mode_gate.
require_mode() {
  dev_kit_mode_require "$@"
}
