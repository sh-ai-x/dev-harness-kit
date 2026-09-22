#!/usr/bin/env bash
# guard-policy.sh — scoped DEV_KIT_GUARDS resolution.
#
# Resolution order is intentionally different from plugin discovery:
#   1. shell DEV_KIT_GUARDS
#   2. .claude/settings.local.json env.DEV_KIT_GUARDS
#   3. .claude/settings.json env.DEV_KIT_GUARDS
#   4. default off
#
# User-scope settings are never read. Loading the plugin from user scope must
# not enable repository guards in unrelated projects.

set -eo pipefail

_guard_policy_root() {
  printf '%s' "${DEV_KIT_GUARD_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}"
}

_guard_policy_valid() {
  case "${1:-}" in
    on|off) return 0 ;;
    *) return 1 ;;
  esac
}

_guard_policy_read_setting() {
  local path="$1"
  [ -f "$path" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  jq -r --arg key DEV_KIT_GUARDS '
    if ((.env | type) == "object" and (.env | has($key))) then .env[$key]
    elif (type == "object" and has($key)) then .[$key]
    else empty
    end
    | select(type == "string")
  ' "$path" 2>/dev/null | grep -m1 . || true
}

_guard_policy_warn_invalid() {
  printf 'guard-policy.sh: ignoring invalid %s DEV_KIT_GUARDS=%q\n' "$1" "$2" >&2
}

dev_kit_guards_resolve() {
  # A missing jq cannot safely read settings, so the thin default is off.
  if [ -n "${DEV_KIT_GUARDS:-}" ]; then
    if _guard_policy_valid "$DEV_KIT_GUARDS"; then
      export DEV_KIT_GUARDS
      DEV_KIT_GUARDS_SOURCE="shell"
      export DEV_KIT_GUARDS_SOURCE
      return 0
    fi
    _guard_policy_warn_invalid shell "$DEV_KIT_GUARDS"
    unset DEV_KIT_GUARDS
  fi

  local root project local_value project_value
  root="$(_guard_policy_root)"
  project="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -z "$project" ] || [ ! -d "$project/.claude" ]; then
    DEV_KIT_GUARDS=off
    DEV_KIT_GUARDS_SOURCE="outside-git"
    export DEV_KIT_GUARDS DEV_KIT_GUARDS_SOURCE
    return 0
  fi

  # Local scope wins over project scope so a single checkout can opt in
  # without changing the committed repository default.
  local_value="$(_guard_policy_read_setting "$project/.claude/settings.local.json")"
  if _guard_policy_valid "$local_value"; then
    DEV_KIT_GUARDS="$local_value"
    DEV_KIT_GUARDS_SOURCE="local"
    export DEV_KIT_GUARDS DEV_KIT_GUARDS_SOURCE
    return 0
  fi
  [ -n "$local_value" ] && _guard_policy_warn_invalid local "$local_value"

  project_value="$(_guard_policy_read_setting "$project/.claude/settings.json")"
  if _guard_policy_valid "$project_value"; then
    DEV_KIT_GUARDS="$project_value"
    DEV_KIT_GUARDS_SOURCE="project"
    export DEV_KIT_GUARDS DEV_KIT_GUARDS_SOURCE
    return 0
  fi
  [ -n "$project_value" ] && _guard_policy_warn_invalid project "$project_value"

  DEV_KIT_GUARDS=off
  DEV_KIT_GUARDS_SOURCE="default"
  export DEV_KIT_GUARDS DEV_KIT_GUARDS_SOURCE
}

dev_kit_guards_active() {
  if ! _guard_policy_valid "${DEV_KIT_GUARDS:-}"; then
    unset DEV_KIT_GUARDS
    dev_kit_guards_resolve
  fi
  printf '%s' "${DEV_KIT_GUARDS:-off}"
}

dev_kit_guard_state() {
  # Read the session override when it exists; otherwise resolve the scoped
  # policy directly. This keeps hooks default-off even when Python cannot
  # import the plugin package from a consumer project's cwd.
  local guard="${1:-}" root="${2:-$(_guard_policy_root)}" state value
  case "$guard" in
    tdd_guard|worktree_guard|git_guard|push_confirm|fork_pr_confirm) ;;
    *) printf '%s' on; return 0 ;;
  esac
  state="$root/.dev-kit/guard-mode.session.json"
  if [ -f "$state" ] && command -v jq >/dev/null 2>&1; then
    value="$(jq -r --arg guard "$guard" '.[$guard] // empty' "$state" 2>/dev/null || true)"
    if _guard_policy_valid "$value"; then
      printf '%s' "$value"
      return 0
    fi
  fi
  DEV_KIT_GUARD_ROOT="$root" dev_kit_guards_resolve >/dev/null
  case "$guard" in
    tdd_guard|worktree_guard|git_guard) printf '%s' "${DEV_KIT_GUARDS:-off}" ;;
    push_confirm) printf '%s' on ;;
    fork_pr_confirm) printf '%s' off ;;
  esac
}

dev_kit_guards_branch_class() {
  local root git_dir common git_dir_abs common_abs
  root="$(_guard_policy_root)"
  root="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$root" ] || { printf '%s' outside; return 0; }
  git_dir="$(cd "$root" && git rev-parse --git-dir 2>/dev/null || true)"
  common="$(cd "$root" && git rev-parse --git-common-dir 2>/dev/null || true)"
  if [ -z "$git_dir" ] || [ -z "$common" ]; then
    printf '%s' outside
  elif command -v realpath >/dev/null 2>&1 \
      && git_dir_abs="$(cd "$root" && realpath "$git_dir" 2>/dev/null)" \
      && common_abs="$(cd "$root" && realpath "$common" 2>/dev/null)" \
      && [ "$git_dir_abs" = "$common_abs" ]; then
    printf '%s' main
  elif [ "$git_dir" = "$common" ]; then
    printf '%s' main
  else
    printf '%s' worktree
  fi
}
