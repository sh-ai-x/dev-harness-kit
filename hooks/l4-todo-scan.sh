#!/usr/bin/env bash
# l4-todo-scan.sh — PostToolUse hook. v1.
#
# Flags deferred-work markers in Write/Edit/MultiEdit payloads using
# the SSOT bank under
#   ${CLAUDE_PLUGIN_ROOT}/hooks/references/l4/markers.md
#
# Default fail-closed (exit 2) on any marker found outside allowed
# paths (*.md docs, tests/fixtures/**, docs/adoption/**). Allowed
# paths are documentation and test fixtures where markers are
# legitimately discussed.
#
# Opt-in escalation via L4_STRICT=1: also scans allowed paths and
# fail-closes on any marker. Use this for PR-time enforcement where
# the allowed-path exemption should be removed.
#
# If references/l4/markers.md is missing, falls back to an inline
# marker list and prints a one-shot WARN to stderr. No silent failure.
#
# jq-missing -> exit 2 (fail-closed; no silent pass-through).

set -eo pipefail
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq l4-todo-scan
# python3 is required by scan_markers below (POSIX classes via Python
# `re` for KO locale safety; mirrors lib/stage-gate.sh:26 which already
# hard-depends on python3). Fail-closed on missing rather than letting
# the `python3 … 2>/dev/null || true` further down silently produce an
# empty scan and exit 0 with no scan performed.
command -v python3 >/dev/null 2>&1 || {
  echo "[l4-todo-scan] FAIL — python3 is required but not installed." >&2
  exit 2
}
read_stdin_json l4-todo-scan
[ -z "$INPUT_JSON" ] && exit 0
hook_stage_active l4-todo-scan || exit 0

FILE=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // ""')
extract_content
[ -z "$CONTENT" ] && exit 0

# ── config ──────────────────────────────────────────────────────────────────
L4_STRICT="${L4_STRICT:-0}"

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MARKERS_BANK="${PLUGIN_ROOT}/hooks/references/l4/markers.md"

# ── allowed paths ───────────────────────────────────────────────────────────
# Iron Law #4 applies to committed code/docs; *.md docs, test fixtures,
# and adoption guides may legitimately discuss these markers.
is_allowed_path() {
  case "$FILE" in
    *.md) return 0 ;;
    tests/fixtures/*) return 0 ;;
    tests/fixtures) return 0 ;;
    docs/adoption/*) return 0 ;;
    docs/adoption) return 0 ;;
    # Bank file path is exempt so the bank's own content does not self-trigger
    # when the L4_STRICT=1 mode scans everything.
    hooks/references/l4/markers.md) return 0 ;;
    # Hook script itself legitimately carries the marker patterns as data;
    # without this exemption, editing the hook would trigger the hook on itself.
    hooks/l4-todo-scan.sh) return 0 ;;
  esac
  return 1
}

# Default mode: skip allowed paths entirely. L4_STRICT=1 scans them too.
if [ "$L4_STRICT" != "1" ] && is_allowed_path; then
  exit 0
fi

# ── inline fallback (used only when bank file is missing) ─────────────────
# 1:1 mirror of hooks/references/l4/markers.md so the fallback scans
# the same markers as the SSOT bank. Drift here means the fallback
# misses (or invents) markers the bank would catch — both directions
# are bugs. Kept as a single combined ERE for grep -E; the bank file
# remains the source of truth and this block must be edited in lockstep
# with it (test_l4_todo_scan.py::BankFallback::test_inline_parity_with_bank
# pins this).
INLINE_BANK='(\bTODO\b|\bFIXME\b|\bXXX\b|\bHACK\b|we'"'"'ll extend later|this is a starting point|\bplaceholder\b|\bstub\b|to be implemented|나중에|추후|임시|시작점|플레이스홀더|스텁)'

# ── scan via Python re (locale-safe for KO) ────────────────────────────────
scan_markers() {
  local bank="$1"
  local content_file="$2"
  local pats
  pats="$(grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$bank" 2>/dev/null)" || return 0
  [ -z "$pats" ] && return 0
  PYTHONIOENCODING=utf-8 python3 - "$bank" "$content_file" <<'PY' 2>/dev/null | sort -u || true
import re, sys, pathlib
bank_path, content_path = sys.argv[1], sys.argv[2]
pats = [
    line for line in pathlib.Path(bank_path).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
text = pathlib.Path(content_path).read_text(encoding="utf-8")
seen = set()
for p in pats:
    # Normalize POSIX classes for Python re: [[:space:]] -> \s, etc.
    p_norm = re.sub(r"\[\[:space:\]\]", r"\\s", p)
    p_norm = re.sub(r"\[\[:alnum:\]_]\]", r"\\w", p_norm)
    p_norm = re.sub(r"\[\[:digit:\]\]", r"\\d", p_norm)
    try:
        for m in re.finditer(p_norm, text):
            seen.add(m.group(0))
    except re.error:
        continue
for m in seen:
    print(m)
PY
}

CONTENT_FILE="$(mktemp -t l4todo.XXXXXX)"
printf '%s' "$CONTENT" > "$CONTENT_FILE"
trap 'rm -f "$CONTENT_FILE"' EXIT

matches=""
if [ -r "$MARKERS_BANK" ]; then
  matches="$(scan_markers "$MARKERS_BANK" "$CONTENT_FILE")"
else
  echo "[l4-todo-scan] WARN: $MARKERS_BANK not readable; using inline fallback." >&2
  matches="$(grep -oE "$INLINE_BANK" "$CONTENT_FILE" 2>/dev/null | sort -u || true)"
fi

if [ -z "$matches" ]; then
  exit 0
fi

# ── emit + fail closed ─────────────────────────────────────────────────────
echo "[l4-todo-scan] FAIL — ${FILE}" >&2
echo "  Markers found:" >&2
while IFS= read -r line; do
  [ -n "$line" ] && echo "    ${line}" >&2
done <<< "$matches"
echo "[l4-todo-scan] Iron Law #4 prohibits deferred-work markers in committed code." >&2
echo "[l4-todo-scan] Resolve the marker or remove it before re-running." >&2

exit 2
