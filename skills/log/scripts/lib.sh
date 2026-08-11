#!/usr/bin/env bash
# lib.sh — shared helpers for skills/log/scripts/*.sh
#
# Sourced by log-setup.sh, log-on.sh, log-off.sh, log-status.sh.
# Never executed directly.

set -euo pipefail

# Sentinel key added to every hook entry this skill installs. log-off.sh
# uses it to strip exactly the entries we added — never touches the
# user's pre-existing hooks.
LOGHOOKS_SENTINEL="_loghooks_managed"

# Resolve the loghooks source repo.
#   1. $LOGHOOKS_DIR if set
#   2. $HOME/dev/loghooks (the user's known location)
# Fails 2 with a stderr message if neither exists or is missing
# the .claude/settings.json marker.
resolve_loghooks_dir() {
    local dir="${LOGHOOKS_DIR:-}"
    if [[ -z "$dir" ]]; then
        dir="${HOME}/dev/loghooks"
    fi
    if [[ ! -d "$dir" ]]; then
        echo "ERROR: loghooks directory not found: $dir" >&2
        echo "Set LOGHOOKS_DIR env var, or create a symlink at $HOME/dev/loghooks" >&2
        return 2
    fi
    if [[ ! -f "$dir/.claude/settings.json" ]]; then
        echo "ERROR: $dir/.claude/settings.json missing — loghooks repo is incomplete" >&2
        return 2
    fi
    printf '%s\n' "$dir"
}

# Resolve the target project = where to install hooks.
#   1. $TARGET_DIR if set
#   2. $PWD (the project where the user ran /log)
resolve_target_dir() {
    printf '%s\n' "${TARGET_DIR:-$PWD}"
}

# Resolve the global target = $HOME/.claude/ for `--global` install.
# Used by `log-setup.sh --global`, `log-on.sh --global`, `log-off.sh --global`
# so capture fires for ANY project (or any worktree of any project) the
# user starts a session in. Overrides $HOME to make tests hermetic.
resolve_global_dir() {
    local home="${HOME:-}"
    if [[ -z "$home" ]]; then
        echo "ERROR: \$HOME is empty; cannot resolve global target." >&2
        return 4
    fi
    printf '%s/.claude\n' "$home"
}


# Build a temp file with the source settings.json rewritten for global
# install. Every `command` field has its `save_log.py` path rewritten to
# ${HOME}/.claude/save_log.py so the merged command is self-contained.
# Returns the temp path on stdout. Caller is responsible for cleanup.
#   $1 = source settings.json path
prepare_global_source() {
    local src="$1"
    local tmp
    # macOS mktemp -t treats the template's suffix literally: it only
    # replaces X's followed by end-of-string, so `XXXXXX.json` produces
    # `XXXXXX.json.<random>` with no X-replacement. Put the X's at the
    # end and append the .json extension after.
    tmp="$(mktemp -t loghooks-global-src-XXXXXX).json"
    # shellcheck disable=SC2064  # we WANT $tmp expanded now, not at signal time
    trap "rm -f '$tmp'" ERR
    # The replacement text is a LITERAL string — the runtime shell expands
    # ${HOME} when the hook actually fires, not at install time. That way
    # a single installed hook works for every user account on the machine
    # (e.g. a per-user install via sudo stays portable across logins).
    # We pass it as --arg so jq's own `${...}` interpolation does not eat
    # our literal `${HOME}`.
    local repl='${HOME}/.claude/save_log.py'
    jq --arg HOME_REPL "$repl" '
        .hooks |= with_entries(
            .value |= map(
                .hooks |= map(
                    select(.command) |
                    (.command |= gsub("[$][{]CLAUDE_PROJECT_DIR[}][/]tools/save_log[.]py"; $HOME_REPL))
                    | (.command |= gsub("tools/save_log[.]py"; $HOME_REPL))
                )
            )
        )
    ' "$src" > "$tmp"
    trap - ERR
    printf '%s\n' "$tmp"
}

# Fail with a clear stderr message and non-zero exit if jq is missing.
require_jq() {
    if ! command -v jq >/dev/null 2>&1; then
        echo "ERROR: jq is required (https://stedolan.github.io/jq/)" >&2
        return 3
    fi
}

# Atomic JSON write: $1 = path, stdin = new contents.
# Cleanup trap on ERR ensures a partial write never leaves a stray
# .tmp.XXXXXX file in the project dir.
write_json_atomic() {
    local path="$1"
    local tmp
    tmp="$(mktemp "${path}.tmp.XXXXXX")"
    # shellcheck disable=SC2064  # we WANT $tmp expanded now, not at signal time
    trap "rm -f '$tmp'" ERR
    cat >"$tmp"
    mv -f "$tmp" "$path"
    trap - ERR
}

# Read a JSON file safely; returns {} when missing.
# Usage: read_json_or_empty <path>
read_json_or_empty() {
    local path="$1"
    if [[ -f "$path" ]]; then
        cat "$path"
    else
        printf '{}\n'
    fi
}

# Merge source loghooks into a target settings.json (Claude or Codex).
# Idempotent: replaces existing entries with the same .hooks[0].command,
# adds new ones, marks every inserted entry with _loghooks_managed=true.
# Preserves all other top-level keys (e.g. permissions, $schema).
#
# A08 mitigation: every merged entry's command MUST match the documented
# shape — a `for`-style python3 lookup that ends by exec'ing
# "${CLAUDE_PROJECT_DIR}/tools/save_log.py --tool <name>". Anything else
# (including bare shell, `curl | sh`, etc.) is rejected and the merge
# fails. This is a command allow-list, not a heuristic.
#   $1 = source settings.json path (the loghooks repo's settings)
#   $2 = target settings.json path (the project to log)
merge_loghooks_into() {
    local src="$1"
    local target="$2"

    local current
    current="$(read_json_or_empty "$target")"

    # First pass: validate every source command matches the documented
    # structural shape via a single regex. Both Claude and Codex hooks
    # are shell `for` loops that iterate python3/python/py, call
    # save_log.py --tool <name>, and end with `fi; done`. Anything else
    # (curl|sh, arbitrary rm -rf, etc.) is rejected here so a poisoned
    # $LOGHOOKS_DIR cannot exfiltrate through the merged command field.
    local bad
    bad="$(printf '%s' "$current" | jq \
        --slurpfile src "$src" '
        [ $src[0].hooks // {} | to_entries[] | .value[] | .hooks[]? | select(.type == "command") | (.command // "")]
        | map(select(test("^for i in python3 python py;.*save_log\\.py.*--tool [a-z0-9-]+.*fi; done$") | not))
    ')"
    if [[ "$bad" != "[]" ]]; then
        echo "ERROR: $src contains commands that do not match the documented save_log.py shape." >&2
        echo "Refusing to merge — review $src manually." >&2
        echo "Required shape: shell 'for' loop iterating python3/python/py, calling save_log.py --tool <name>, ending 'fi; done'." >&2
        echo "Offending entries:" >&2
        printf '%s\n' "$bad" | jq -r '.[]' | sed 's/^/  /' >&2
        return 6
    fi

    printf '%s' "$current" | jq \
        --slurpfile src "$src" \
        --arg sentinel "$LOGHOOKS_SENTINEL" '
        .hooks = (.hooks // {})
        | reduce ([$src[0].hooks // {} | keys[]] | unique[]) as $event (
            .;
            .hooks[$event] = (
                (
                    (.hooks[$event] // [])
                    | map(
                        select(
                            (.hooks[0].command // "") as $cmd
                            | ($src[0].hooks[$event] // [])
                              | map(.hooks[0].command // "")
                              | index($cmd)
                            | not
                        )
                    )
                )
                + (
                    ($src[0].hooks[$event] // [])
                    | map(. + {($sentinel): true})
                )
            )
          )
        ' | write_json_atomic "$target"
}

# Remove all hook entries marked _loghooks_managed from a settings.json.
# Leaves user-authored entries untouched. Preserves all other top-level keys.
#   $1 = target settings.json path
remove_managed_from() {
    local target="$1"

    [[ -f "$target" ]] || return 0

    jq \
        --arg sentinel "$LOGHOOKS_SENTINEL" '
        if .hooks == null then . else
          reduce (.hooks | keys[]) as $event (
            .;
            .hooks[$event] = (
                (.hooks[$event] // [])
                | map(select(.[$sentinel] != true))
            )
          )
          | if .hooks == {} then del(.hooks) else . end
        end
        ' "$target" | write_json_atomic "$target"
}

# Count managed entries across all events in a settings.json.
# Returns 0 on success (writes count to stdout).
#   $1 = target settings.json path
count_managed() {
    local target="$1"

    [[ -f "$target" ]] || { echo 0; return 0; }

    jq --arg sentinel "$LOGHOOKS_SENTINEL" '
        [ (.hooks // {}) | to_entries[] | .value[] | select(.[$sentinel] == true) ]
        | length
    ' "$target"
}


# parse_log_args — shared CLI parser for log-on/off/setup/status.
# Parses: --target DIR | --global | --codex-only | --claude-only | -h | --help
# Validates mutual exclusivity of --codex-only/--claude-only and --global/--target.
# Sets globals CLAUDE_ONLY, CODEX_ONLY, GLOBAL, TARGET_DIR.
# On -h/--help, prints $LOG_USAGE (each script sets its own) and exits 0.
# On unknown arg, prints usage to stderr and exits 1.
parse_log_args() {
    CLAUDE_ONLY=0
    CODEX_ONLY=0
    GLOBAL=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target)       TARGET_DIR="$2"; shift 2 ;;
            --global)       GLOBAL=1; shift ;;
            --claude-only)  CLAUDE_ONLY=1; shift ;;
            --codex-only)   CODEX_ONLY=1; shift ;;
            -h|--help)
                if [[ -n "${LOG_USAGE:-}" ]]; then
                    eval "$LOG_USAGE"
                fi
                exit 0
                ;;
            *) echo "ERROR: unknown arg: $1" >&2
                if [[ -n "${LOG_USAGE:-}" ]]; then
                    eval "$LOG_USAGE" >&2
                fi
                exit 1
                ;;
        esac
    done

    if [[ "$CLAUDE_ONLY" -eq 1 && "$CODEX_ONLY" -eq 1 ]]; then
        echo "ERROR: --claude-only and --codex-only are mutually exclusive" >&2
        exit 1
    fi
    if [[ "$GLOBAL" -eq 1 && -n "${TARGET_DIR:-}" ]]; then
        echo "ERROR: --global is mutually exclusive with --target" >&2
        exit 1
    fi
}
