#!/usr/bin/env bash
# bash-guard.sh — PreToolUse hook for Bash. Blocks destructive commands.
# Default advisory (exit 0); hard-block (exit 2) with --strict.

set -eo pipefail
# Use %/* parameter expansion (POSIX, no external `dirname` required) so
# the source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — see TestBashGuardRefactor.fails_closed).
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq bash-guard
read_stdin_json bash-guard
[ -z "$INPUT_JSON" ] && exit 0
hook_stage_active bash-guard || exit 0
CMD=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""')
[ -z "$CMD" ] && exit 0

# Destructive patterns
BLOCKED_PATTERNS=(
  "rm -rf /([[:space:]]|$)"
  "rm -rf ~"
  "rm -rf [\"']?\\\$HOME[\"']?"
  "git push --force.* main"
  "git push -f .* main"
  "git reset --hard"
  "git clean -f"
  "DROP TABLE"
  "DROP DATABASE"
  "chmod 777"
  "chown -R /"
  ">/etc/passwd"
  "curl.*[|].*sh"
  "wget.*[|].*sh"
  "npm publish"
  "docker system prune"
  "eval \$"
  "DEV_KIT_HOOK_OFF=.bash-guard"
)

for pattern in "${BLOCKED_PATTERNS[@]}"; do
  if echo "$CMD" | grep -qE "$pattern"; then
    if [ "${DEV_KIT_STRICT:-0}" = "1" ]; then
      deny "BASH GUARD (strict)" "pattern '$pattern' blocked."
    fi
    echo "[bash-guard] Pattern '$pattern' in command: ${CMD:0:60}... (advisory). strict mode required to block." >&2
    exit 0
  fi
done
exit 0
