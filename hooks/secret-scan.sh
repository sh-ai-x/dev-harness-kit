#!/usr/bin/env bash
# secret-scan.sh — PostToolUse hook. Detects credential patterns in Write/Edit/MultiEdit.
# Default advisory (exit 0).

set -eo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full,lite
# Use %/* parameter expansion (POSIX, no external `dirname` required) so
# the source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — see TestSecretScanRefactor.fails_closed).
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
# shellcheck source=lib/secret-patterns.sh
source "${BASH_SOURCE[0]%/*}/lib/secret-patterns.sh"
require_jq secret-scan
read_stdin_json secret-scan
[ -z "$INPUT_JSON" ] && exit 0
hook_stage_active secret-scan || exit 0
FILE=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // ""')
extract_content
[ -z "$CONTENT" ] && exit 0

# Patterns (regex) — sourced from lib/secret-patterns.sh (SSOT mirror of
# lib/analysis_core/runner.py::_SECRET_PATTERNS). NO secret text in
# output — masked only.

HITS=()
for p in "${SECRET_PATTERNS[@]}"; do
  MATCHES=$(echo "$CONTENT" | grep -oE "$p" 2>/dev/null | head -3 || true)
  if [ -n "$MATCHES" ]; then
    HITS+=("$p × $(echo "$MATCHES" | wc -l)")
  fi
done

if [ ${#HITS[@]} -gt 0 ]; then
  echo "[secret-scan] $FILE — credential patterns detected (masked):" >&2
  for h in "${HITS[@]}"; do
    echo "  - $h" >&2
  done
  echo "[secret-scan] Remove or replace with env var reference." >&2
fi
exit 0
