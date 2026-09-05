#!/usr/bin/env bash
# UserPromptSubmit: judge only requests not covered by path rules.
set -euo pipefail
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
require_jq "TDD SCOPE JUDGE"
INPUT=$(cat)
PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // ""')
[ -z "$PROMPT" ] && exit 0
ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
python3 -m lib.tdd_scope_judge --root "$ROOT" --prompt "$PROMPT" >/dev/null 2>&1 || true
