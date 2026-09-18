#!/usr/bin/env bash
# session-start.sh — one SessionStart registration for the lifecycle bundle.
#
# Child hooks remain independently testable and keep their own contracts.
# This adapter reads the runtime payload once, fans it out, and combines the
# advisory additionalContext values into one valid SessionStart envelope.
# SessionStart children are fail-open, so one unavailable child never hides
# the remaining lifecycle work or blocks a new session.

set -uo pipefail

HOOK_DIR="${BASH_SOURCE[0]%/*}"
INPUT="$(cat 2>/dev/null || true)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dev-kit-session-start.XXXXXX" 2>/dev/null || true)"

emit_empty() {
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart"}}'
}

[ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ] || {
  emit_empty
  exit 0
}
trap 'rm -rf "$TMP_DIR"' EXIT

CHILDREN=(
  session-start-check.sh
  log-on-session-start.sh
  provider-divergence-check.sh
  linear-session-start.sh
  worktree-janitor-session-start.sh
  session-start-harness-mode-reset.sh
  session-start-guard-mode-reset.sh
  plugin-cache-refresh.sh
)

for child in "${CHILDREN[@]}"; do
  child_path="$HOOK_DIR/$child"
  [ -f "$child_path" ] || continue
  output="$TMP_DIR/${child%.sh}.out"
  printf '%s' "$INPUT" | bash "$child_path" >"$output"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '[session-start] %s exited %s; continuing (fail-open)\n' "$child" "$rc" >&2
  fi
done

# jq is optional here: the child hooks already own their jq contracts. If it
# is absent, suppress child envelopes and keep the SessionStart boundary valid.
command -v jq >/dev/null 2>&1 || {
  emit_empty
  exit 0
}

OUTPUTS="$TMP_DIR/outputs.jsonl"
: > "$OUTPUTS"
for child in "${CHILDREN[@]}"; do
  output="$TMP_DIR/${child%.sh}.out"
  [ -s "$output" ] || continue
  if jq -e . "$output" >/dev/null 2>&1; then
    cat "$output" >> "$OUTPUTS"
    printf '\n' >> "$OUTPUTS"
  else
    printf '[session-start] ignored non-JSON child output: %s\n' "$(basename "$output")" >&2
  fi
done

[ -s "$OUTPUTS" ] || {
  emit_empty
  exit 0
}

jq -s -c '
  [
    .[]
    | .hookSpecificOutput? // {}
    | .additionalContext?
    | select(type == "string" and length > 0)
  ] as $contexts
  | {hookSpecificOutput: {hookEventName: "SessionStart"}}
  | if ($contexts | length) > 0
    then .hookSpecificOutput.additionalContext = ($contexts | join("\n"))
    else .
    end
' "$OUTPUTS" 2>/dev/null || emit_empty

exit 0
