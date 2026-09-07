#!/usr/bin/env bash
# team-resolve.sh — shared DEV_KIT_TEAM resolution for hooks + skills.
#
# Single source of truth for "is the current session in team mode?".
# Sourced (not executed) by every consumer that wants to short-circuit
# when team-mode is OFF, or to flip its default behavior when team-mode
# is ON. Independent of DEV_KIT_MODE (the three `mode` values
# `full|lite|undev` are orthogonal to this toggle).
#
# Resolution order (highest wins), matching docs/scopes/modes.md:
#   1. $DEV_KIT_TEAM shell env var    — per-session override
#   2. <proj>/.claude/settings.json env.DEV_KIT_TEAM — committed team choice
#   3. <proj>/.claude/settings.local.json env.DEV_KIT_TEAM — personal override
#   4. Default = "off" — silent; team mode is opt-in
#
# Public API:
#   dev_kit_team_resolve            — sets $DEV_KIT_TEAM to one of
#                                     "on", "off". Source label is
#                                     exported as $DEV_KIT_TEAM_SOURCE.
#   dev_kit_team_active             — echo the active value (no-op if
#                                     not yet resolved).
#   dev_kit_team_require <required> — GATE helper (NOT a boolean). See
#                                     full comment in mode-resolve.sh —
#                                     this function uses identical
#                                     call-and-forget semantics.
#
# Hooks/skills that want to act on team-mode (e.g., track .dev-kit/ in
# git) add at the top:
#     source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/team-resolve.sh"
#     [ "$(dev_kit_team_active)" = "on" ] && ...track...
#
# Hooks that run ONLY in team-mode add:
#     source .../team-resolve.sh
#     dev_kit_team_require on
# exit 0
#
# Always-on consumers do not source this file.

set -eo pipefail

# require_jq_team — fail-closed contract. Emits the JSON denial on
# STDOUT (Claude Code hook protocol reads JSON from stdout) and a
# one-line reason on STDERR for human visibility.
require_jq_team() {
  if ! command -v jq >/dev/null 2>&1; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"team-resolve.sh: jq is required for DEV_KIT_TEAM gating. Install jq (apt/brew/apk)."}}\n'
    printf 'team-resolve.sh: jq missing — fail-closed\n' >&2
    exit 2
  fi
}

# _is_valid_team <value> — returns 0 iff <value> ∈ {on,off,1,0,true,false}.
# Used to reject typos before they silently fail-open every gating hook.
_is_valid_team() {
  case "${1:-}" in
    on|off|1|0|true|false) return 0 ;;
    *) return 1 ;;
  esac
}

# _read_settings_env <path> <key> — read DEV_KIT_TEAM from a JSON
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
# held an unparseable value. Honors a rate-limit (one warning per
# layer per resolve call) so a corrupted settings file doesn't spam.
_warn_invalid() {
  local layer="$1" value="$2"
  printf 'team-resolve.sh: ignoring invalid %s DEV_KIT_TEAM=%q\n' "$layer" "$value" >&2
}

# _normalize_team <value> — map any truthy/falsy value to "on"/"off".
#   on   ← "on", "1", "true"
#   off  ← "off", "0", "false"
# Anything else is invalid (caller must gate with _is_valid_team first).
_normalize_team() {
  case "${1:-}" in
    on|1|true) printf '%s' "on" ;;
    off|0|false) printf '%s' "off" ;;
    *) printf '%s' "${1:-}" ;;
  esac
}

dev_kit_team_resolve() {
  require_jq_team

  # Layer 1: shell env var wins
  if [ -n "${DEV_KIT_TEAM:-}" ] && _is_valid_team "${DEV_KIT_TEAM}"; then
    DEV_KIT_TEAM="$(_normalize_team "$DEV_KIT_TEAM")"
    export DEV_KIT_TEAM
    DEV_KIT_TEAM_SOURCE="shell"
    export DEV_KIT_TEAM_SOURCE
    return 0
  fi
  if [ -n "${DEV_KIT_TEAM:-}" ]; then
    _warn_invalid "shell" "$DEV_KIT_TEAM"
    unset DEV_KIT_TEAM
  fi

  # Find the project root (.claude lives there). If we're outside any
  # project, default to "off" — no plugin, no team toggle semantics.
  local proj_root
  proj_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$proj_root" ] || [ ! -d "$proj_root/.claude" ]; then
    DEV_KIT_TEAM="off"
    export DEV_KIT_TEAM
    DEV_KIT_TEAM_SOURCE="outside-git"
    export DEV_KIT_TEAM_SOURCE
    return 0
  fi

  # Layer 2: project-scope .claude/settings.json
  local proj_team
  proj_team="$(_read_settings_env "$proj_root/.claude/settings.json" "DEV_KIT_TEAM")"
  if _is_valid_team "$proj_team"; then
    DEV_KIT_TEAM="$(_normalize_team "$proj_team")"
    export DEV_KIT_TEAM
    DEV_KIT_TEAM_SOURCE="project"
    export DEV_KIT_TEAM_SOURCE
    return 0
  fi
  [ -n "$proj_team" ] && _warn_invalid "project" "$proj_team"

  # Layer 3: local-scope .claude/settings.local.json
  local local_team
  local_team="$(_read_settings_env "$proj_root/.claude/settings.local.json" "DEV_KIT_TEAM")"
  if _is_valid_team "$local_team"; then
    DEV_KIT_TEAM="$(_normalize_team "$local_team")"
    export DEV_KIT_TEAM
    DEV_KIT_TEAM_SOURCE="local"
    export DEV_KIT_TEAM_SOURCE
    return 0
  fi
  [ -n "$local_team" ] && _warn_invalid "local" "$local_team"

  # Layer 4: silent default = "off"
  DEV_KIT_TEAM="off"
  export DEV_KIT_TEAM
  DEV_KIT_TEAM_SOURCE="default"
  export DEV_KIT_TEAM_SOURCE
  return 0
}

dev_kit_team_active() {
  if [ -z "${DEV_KIT_TEAM:-}" ]; then
    dev_kit_team_resolve
  fi
  printf '%s' "${DEV_KIT_TEAM:-off}"
}

# dev_kit_team_require <required> — GATE helper for hooks.
#
# IMPORTANT: returns 0 on BOTH MATCH and NO-MATCH.
#   - MATCH (active value ∈ <required>): caller continues into the hook
#     body.
#   - NO-MATCH (active value ∉ <required>): caller MUST NOT continue;
#     this function calls `exit 0` to short-circuit the entire hook.
#
# Always call as the LAST statement before the hook body. Example:
#     source .../team-resolve.sh
#     dev_kit_team_require on
#     # hook body below — only reached when active team == "on"
#
# To avoid the footgun, prefer `dev_kit_team_gate <required>` (alias)
# which reads as a gate rather than a require.
dev_kit_team_require() {
  local required="$1"
  local active
  active="$(dev_kit_team_active)"
  local IFS=','
  for r in $required; do
    r="${r// /}"
    [ "$r" = "$active" ] && return 0
  done
  exit 0
}

# dev_kit_team_gate <required> — alias of dev_kit_team_require with
# gate-flavored naming. Same call-and-forget semantics.
dev_kit_team_gate() {
  dev_kit_team_require "$@"
}

# require_team — backward-compat alias for older callers. Same as
# dev_kit_team_require / dev_kit_team_gate.
require_team() {
  dev_kit_team_require "$@"
}
