#!/usr/bin/env bash
# log_state.sh — per-repo log-state detection for hook authors.
#
# Usage:
#   is_source_log_on <repo_path>
#     Returns 0 (true) iff the source repo's log capturing is currently
#     ENABLED, i.e. the repo has tools/save_log.py AND its
#     .claude/settings.json carries at least one entry tagged with
#     _loghooks_managed=true. Returns 1 otherwise.
#
# Why managed-entries AND script presence (and not just one):
#   - scripts/lib.sh#log-off leaves tools/save_log.py in place by
#     design (skills/log/SKILL.md Iron Law: "Hooks merge, not replace.
#     Off is sentinel-scoped, not 'rm -rf'"). So script presence is
#     necessary but not sufficient.
#   - Settings.json managed entries are the ground truth that hooks
#     are currently active — that's what `log on` writes and `log off`
#     removes. Together they reflect the user's explicit ON/OFF choice.
#
# Why per-repo (not user-scope):
#   The /dev-kit:log on/off contract is per-repository. Auto-install
#   paths (worktree creation, session start) must respect each repo's
#   own state — propagating ON from one repo to another via a shared
#   user-scoped default would silently capture data the operator
#   turned off.
#
# Mirror this gate in every hook that auto-enables log. The
# /dev-kit:session-start detection at hooks/log-on-session-start.sh
# still uses a slightly looser "script presence" gate and is a known
# follow-up (see hooks/log-on-session-start.sh:73-77). Kept looser
# there temporarily so an incomplete bootstrap doesn't regress to NO
# capture; in this file we can be stricter because the caller already
# knows we are inside a worktree creation flow that has been
# preconditions-gated.

is_source_log_on() {
    local src="$1"
    [[ -x "$src/tools/save_log.py" ]] || return 1
    [[ -f "$src/.claude/settings.json" ]] || return 1
    local n
    n="$(jq --arg sentinel "_loghooks_managed" \
            '[ (.hooks // {}) | to_entries[] | .value[] | select(.[$sentinel] == true) ] | length' \
            "$src/.claude/settings.json" 2>/dev/null)" || return 1
    [[ "$n" -gt 0 ]]
}
