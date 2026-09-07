#!/usr/bin/env bash
# install.sh — Bootstrap dev-harness-kit into target project.
# Modes:
#   --strict : set DEV_KIT_STRICT=1 in plugin.json env (hard-block hooks)
#
# Source layout (this script lives at lib/install.sh):
#   ../.claude-plugin/{marketplace,plugin}.json
#   ../hooks/hooks.json                         (Claude hook manifest)
#   ../hooks/*.sh                              (actual hook scripts)
#   ./*.py                                     (lib modules)
#   ../agents/*                                (project agents)
#   ../skills/<name>/                          (skills and skill assets)
#   ../../tests/*.py                           (regression tests)

set -eo pipefail

TARGET="${1:-$PWD}"
SRC_LIB="$(cd "$(dirname "$0")" && pwd)"
SRC_REPO="$(cd "$SRC_LIB/.." && pwd)"
WITH_STRICT=false

for arg in "$@"; do
  case "$arg" in
    --strict) WITH_STRICT=true ;;
    *) ;;
  esac
done

echo "→ Installing dev-harness-kit into: $TARGET"
mkdir -p "$TARGET/.claude-plugin/plugin/.claude-plugin" \
         "$TARGET/.claude-plugin/plugin/hooks" \
         "$TARGET/lib" \
         "$TARGET/tests"

copy() {
  # copy <src> <dst_dir>; skip silently if src missing
  [ -e "$1" ] && cp "$1" "$2" || echo "  ! skip (missing): $1"
}

# Plugin manifests
copy "$SRC_REPO/.claude-plugin/marketplace.json" "$TARGET/.claude-plugin/"
copy "$SRC_REPO/.claude-plugin/plugin.json" "$TARGET/.claude-plugin/plugin/.claude-plugin/"
copy "$SRC_REPO/hooks/hooks.json" "$TARGET/.claude-plugin/plugin/hooks/"

# Hook scripts (real location: <repo>/hooks/)
for sh in "$SRC_REPO"/hooks/*.sh; do
  copy "$sh" "$TARGET/.claude-plugin/plugin/hooks/"
done

# Lib modules (all *.py in lib/)
for py in "$SRC_LIB"/*.py; do
  copy "$py" "$TARGET/lib/"
done

# Project agents and skills stay at the target root as the shared SSOT.
if [ -d "$SRC_REPO/agents" ]; then
  mkdir -p "$TARGET/agents"
  for agent_file in "$SRC_REPO"/agents/*; do
    [ -f "$agent_file" ] || continue
    copy "$agent_file" "$TARGET/agents/"
  done
fi

# Skills (flat: skills/<skill-name>/SKILL.md). Copy the full skill directory
# so scripts and fixtures remain available to both provider runtimes.
if [ -d "$SRC_REPO/skills" ]; then
  for skill_dir in "$SRC_REPO/skills"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    mkdir -p "$TARGET/skills/$skill_name"
    cp -R "$skill_dir". "$TARGET/skills/$skill_name/"
  done
fi

# Regression tests
for py in "$SRC_REPO"/tests/*.py; do
  copy "$py" "$TARGET/tests/"
done

# Strict mode — set DEV_KIT_STRICT=1 in plugin.json env block so hooks can read it.
if $WITH_STRICT; then
  plugin_json="$TARGET/.claude-plugin/plugin/.claude-plugin/plugin.json"
  if [ -f "$plugin_json" ] && command -v jq >/dev/null 2>&1; then
    tmp=$(mktemp)
    jq '.env = (.env // {}) + {"DEV_KIT_STRICT": "1"}' "$plugin_json" > "$tmp" && mv "$tmp" "$plugin_json"
    echo "  ✓ strict mode: DEV_KIT_STRICT=1 set in plugin.json env"
  else
    echo "  ! strict mode requested but jq missing or plugin.json absent"
  fi
fi

# Team mode — keep .dev-kit/ tracked (strip any pre-existing ignore).
# Note: this script is invoked by manual `bash lib/install.sh` only; the
# canonical /dev-kit:bootstrap skill reads $DEV_KIT_TEAM via
# hooks/lib/team-resolve.sh and acts there. Callers that want the
# .dev-kit/-tracking effect should set DEV_KIT_TEAM=1 and run the
# bootstrap skill, not pass --team to this script.

echo "→ Verifying:"
for f in "$TARGET/.claude-plugin/marketplace.json" \
         "$TARGET/.claude-plugin/plugin/.claude-plugin/plugin.json" \
         "$TARGET/lib/state_codec.py" \
         "$TARGET/lib/atomic.py"; do
  if [ -f "$f" ]; then echo "  ✓ $f"; else echo "  ✗ MISSING: $f"; exit 1; fi
done

echo ""
echo "✅ dev-harness-kit installed."
echo "   Next:  cd $TARGET && claude"
