#!/usr/bin/env bash
# install.sh — Bootstrap dev-harness-kit into target project.
# Modes:
#   --team        : include .dev-kit/ in git (100x AX)
#   --strict      : enable hard-block hooks (default advisory)
#   --config      : launch /dev-kit:config picker auto
#   --skip-sanity : skip sanity stage
#   --skip-map    : skip codebase-map stage
#
# Adapted from dev-harness/install.sh.

set -eo pipefail

TARGET="${1:-$PWD}"
WITH_TEAM=false
WITH_STRICT=false
WITH_CONFIG=false
SKIP_SANITY=false
SKIP_MAP=false

for arg in "$@"; do
  case "$arg" in
    --team) WITH_TEAM=true ;;
    --strict) WITH_STRICT=true ;;
    --config) WITH_CONFIG=true ;;
    --skip-sanity) SKIP_SANITY=true ;;
    --skip-map) SKIP_MAP=true ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
echo "→ Installing dev-harness-kit into: $TARGET"
mkdir -p "$TARGET/.claude/hooks" "$TARGET/.claude/skills" "$TARGET/.claude/commands"
mkdir -p "$TARGET/.claude-plugin/plugin/.claude-plugin" "$TARGET/.claude-plugin/plugin/hooks"
mkdir -p "$TARGET/lib" "$TARGET/tests" "$TARGET/eval"

# Plugin manifest
cp "$SRC/../.claude-plugin/marketplace.json" "$TARGET/.claude-plugin/" 2>/dev/null || \
    cp "$SRC/../../.claude-plugin/marketplace.json" "$TARGET/.claude-plugin/" 2>/dev/null || \
    echo "  ! marketplace.json not found (skip)"

# Try to copy from local plugin layout
for p in "$SRC/../.claude-plugin/plugin" "$SRC/../../.claude-plugin/plugin"; do
  if [ -d "$p" ]; then
    SRC_PLUGIN="$p"
    break
fi
done

if [ -n "${SRC_PLUGIN:-}" ]; then
  cp "$SRC_PLUGIN/.claude-plugin/plugin.json" "$TARGET/.claude-plugin/plugin/.claude-plugin/" 2>/dev/null || true
  cp "$SRC_PLUGIN/hooks/hooks.json" "$TARGET/.claude-plugin/plugin/hooks/" 2>/dev/null || true
  cp "$SRC_PLUGIN/hooks/"*.sh "$TARGET/.claude-plugin/plugin/hooks/" 2>/dev/null || true
fi

# lib (3 critical modules)
cp "$SRC/state_codec.py" "$TARGET/lib/" 2>/dev/null || true
cp "$SRC/active_hooks_codec.py" "$TARGET/lib/" 2>/dev/null || true
cp "$SRC/write_claude_md.py" "$TARGET/lib/" 2>/dev/null || true
cp "$SRC/execute.py" "$TARGET/lib/" 2>/dev/null || true

# Skills (flat: skills/<skill-name>/SKILL.md — one level, Claude Code plugin convention)
if [ -d "$SRC/../skills" ]; then
  for skill_dir in "$SRC/../skills"/*/; do
    skill_name=$(basename "$skill_dir")
    mkdir -p "$TARGET/.claude/skills/$skill_name"
    cp "$skill_dir/SKILL.md" "$TARGET/.claude/skills/$skill_name/" 2>/dev/null || true
  done
fi

# Commands
if [ -d "$SRC/../../commands" ]; then
  cp "$SRC/../../commands"/*.md "$TARGET/.claude/commands/" 2>/dev/null || true
fi

# Tests
if [ -d "$SRC/../../tests" ]; then
  cp "$SRC/../../tests"/*.py "$TARGET/tests/" 2>/dev/null || true
fi

# Templates
if [ -d "$SRC/../../templates" ]; then
  cp "$SRC/../../templates"/*.md "$TARGET/templates/" 2>/dev/null || true
fi

# Mode flags
if $WITH_STRICT; then
  echo "" > "$TARGET/.claude-plugin/plugin/hooks/.strict"
  echo "  ✓ strict mode enabled (hooks will hard-block)"
fi

# Team mode — include .dev-kit/ in git (NOT gitignore)
if $WITH_TEAM; then
  if [ -f "$TARGET/.gitignore" ]; then
    grep -v "^\.dev-kit" "$TARGET/.gitignore" > "$TARGET/.gitignore.tmp" || true
    mv "$TARGET/.gitignore.tmp" "$TARGET/.gitignore"
  fi
  echo "  ✓ team mode: .dev-kit/ will be git-included"
fi

# Verification
echo "→ Verifying:"
for f in "$TARGET/.claude-plugin/plugin/.claude-plugin/plugin.json" \
         "$TARGET/.claude-plugin/plugin/hooks/hooks.json" \
         "$TARGET/lib/state_codec.py"; do
  [ -f "$f" ] && echo "  ✓ $f" || { echo "  ✗ MISSING: $f"; exit 1; }
done

if $WITH_CONFIG; then
  echo "  → Auto-launch /dev-kit:config picker (next session)"
fi

echo ""
echo "✅ dev-harness-kit installed."
echo "   Next:  cd $TARGET"
echo "          claude (auto-loads plugin via plugin.json)"
echo "          /dev-kit:bootstrap  (or wait for Stage B auto-load)"
