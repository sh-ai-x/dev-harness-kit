#!/usr/bin/env bash
# tools-sync.sh — SessionStart hook.
#
# Commands/skills shell out to bundled `tools/*.py` scripts with a bare
# relative path (e.g. /dev-kit:skill-usage -> `python3 tools/skill_usage.py`).
# That path resolves against the session's cwd, not the plugin's install
# location. ${CLAUDE_PLUGIN_ROOT} is NOT usable here: it only expands
# inside hooks.json / MCP JSON configs, not inside command markdown
# bodies executed via the Bash tool (anthropics/claude-code#9354, open
# as of this writing). Without this hook, every command that shells out
# to a bundled tool script silently fails with "No such file or
# directory" in any consumer project that isn't dev-harness-kit's own
# checkout.
#
# Fires at session start: for each managed tool script missing from
# ./tools/, copy it from the plugin's tools/ dir (resolved via
# ${CLAUDE_PLUGIN_ROOT}, which DOES expand correctly inside hooks.json --
# this is a hooks.json-invoked script, not a command markdown body).
# No-ops inside dev-harness-kit's own checkout or worktrees -- the files
# are already there via git, so the "missing" check naturally skips them.
#
# Add an entry to MANAGED_FILES whenever a new command/skill body gains
# a `python3 tools/<name>.py` invocation that must survive a consumer
# install.
#
# Fails open (silent, exit 0) when: jq is missing, outside any git
# repo, PLUGIN_ROOT does not resolve to a real tools/ dir, or nothing
# needed copying.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning) + the session-envelope helper.
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"
# shellcheck source=lib/session-envelope.sh
source "${BASH_SOURCE[0]%/*}/lib/session-envelope.sh"

# Warn (not fail) if jq is missing. The preamble's worktree_detect
# leaves $WORKTREE_DETECT="" when jq is absent; the case below already
# treats "" as silent.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "tools-sync.sh"
  exit 0
fi

extract_hook_cwd "tools-sync.sh"

# Discriminator: already populated by the preamble. Only act inside a
# real git working tree (main checkout or a worktree) — dumping files
# into an arbitrary non-project directory would be surprising.
case "$WORKTREE_DETECT" in
  worktree|main) ;;
  *) exit 0 ;;
esac

# Resolve plugin root. Prefer the runtime env var; fall back to a
# path-relative resolution (matches the log-on-session-start.sh /
# slop-detector.sh pattern) for local dev via --plugin-dir.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC_TOOLS="$PLUGIN_ROOT/tools"

[ -d "$SRC_TOOLS" ] || exit 0

# Managed scripts: bundled tools/*.py that user-invocable commands/
# skills shell out to by bare relative path.
MANAGED_FILES=(
  skill_usage.py
  skill_usage_normalize.py
  skill_usage_render.py
)

COPIED=()
for f in "${MANAGED_FILES[@]}"; do
  if [ -f "$SRC_TOOLS/$f" ] && [ ! -f "./tools/$f" ]; then
    mkdir -p tools
    cp "$SRC_TOOLS/$f" "./tools/$f"
    chmod +x "./tools/$f" 2>/dev/null || true
    COPIED+=("$f")
  fi
done

[ "${#COPIED[@]}" -eq 0 ] && exit 0

CTX="tools-sync: installed ${#COPIED[@]} bundled tool script(s) into ./tools/ ($(IFS=,; echo "${COPIED[*]}"))"
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
exit 0
