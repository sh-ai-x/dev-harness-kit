#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# UserPromptSubmit: judge only requests not covered by path rules.
set -euo pipefail
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
require_jq "TDD SCOPE JUDGE"
INPUT=$(cat)
PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // ""')
[ -z "$PROMPT" ] && exit 0
# Root must honor DEV_KIT_TDD_ROOT, matching hooks/tdd-guard.sh:26.
# Without this, a judge decision recorded under DEV_KIT_TDD_ROOT is
# written to the git toplevel instead, so tdd-guard.sh (which reads
# $DEV_KIT_TDD_ROOT/.dev-kit/.tdd-scope.json) never sees
# tdd_required=false and false-denies the edit.
ROOT="${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
python3 -m lib.tdd_scope_judge --root "$ROOT" --prompt "$PROMPT" >/dev/null 2>&1 || true
