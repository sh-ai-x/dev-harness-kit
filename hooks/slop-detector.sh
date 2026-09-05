#!/usr/bin/env bash
# slop-detector.sh — PostToolUse hook. v2.
#
# Flags LLM-tells in Write/Edit results using the SSOT bank under
#   ${CLAUDE_PLUGIN_ROOT}/hooks/references/slop/{phrases,structures}.md
#
# Default advisory (exit 0). Opt-in strict via SLOP_STRICT=1 (exit 2 on HIGH).
#
# Tiers (env SLOP_LEVEL=1|2, default 2 — SLOP_LEVEL=3 is accepted as alias of 2):
#   T1 PHRASE    phrases.md     — KO + EN n-grams (throat-clearing, jargon, adverbs, meta)
#   T2 STRUCTURE structures.md  — regex shapes incl. KO structure, false agency, three-item lists,
#                                 dramatic fragmentation, em-dash density, lazy extremes, Wh-starters
#   (T3 RHYTHM  was reserved for a future opt-in density-only tier. v2 ships T2 carrying the
#                rhythm patterns because in practice they only matter when a document is already
#                triggering the structural ladder; SLOP_LEVEL=3 is therefore aliased to 2.)
#
# Severity rules (see hooks/references/slop/README.md for the full ladder):
#   - Any KO phrase or KO structure match → HIGH immediately.
#   - ≥3 unique T1 OR (≥1 T2 + ≥1 T1) → HIGH
#   - ≥2 unique T1 → MEDIUM
#   - 1 unique T1 OR 1 T2 → LOW
#
# If references/slop/{phrases,structures}.md is missing, falls back to the v1 inline
# bank and prints a one-shot WARN to stderr. No silent failure.

set -eo pipefail
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# harness-mode opt-out (workflow-fast-mode-lean): /dev-kit:harness-mode fast|custom
# can turn this gate off for the current session. Checked before the jq/stdin
# machinery so the opt-out costs one Python process, not a wasted parse.
if command -v python3 >/dev/null 2>&1; then
  gate=$(cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null && python3 -m lib.harness_mode_state get slop_detector 2>/dev/null || echo on)
  if [ "$gate" = "off" ]; then
    echo "slop-detector: opted out via harness-mode" >&2
    exit 0
  fi
fi
# Use %/* parameter expansion (POSIX, no external `dirname` required) so
# the source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — see TestSlopDetectorRefactor.fails_closed).
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq slop-detector
read_stdin_json slop-detector
[ -z "$INPUT_JSON" ] && exit 0
hook_stage_active slop-detector || exit 0

# ── locale (extracted to hooks/lib/locale-utf8.sh — PR-F)
# shellcheck source=lib/locale-utf8.sh
source "${BASH_SOURCE[0]%/*}/lib/locale-utf8.sh"

FILE=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // ""')
extract_content
[ -z "$CONTENT" ] && exit 0

# File path scope skip — checks/lockfiles produce noise without value.
case "$FILE" in
  *.lock|*.min.js|*.min.css|*-lock.json|pnpm-lock.yaml|package-lock.json|yarn.lock) exit 0;;
esac

# ── config ──────────────────────────────────────────────────────────────────
SLOP_LEVEL="${SLOP_LEVEL:-2}"        # 1=phrase, 2=+structure, 3=+rhythm
SLOP_QUIET="${SLOP_QUIET:-0}"        # 1=suppress stderr (still exit 0)
SLOP_STRICT="${SLOP_STRICT:-0}"      # 1=exit 2 on HIGH

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PHRASES_BANK="${PLUGIN_ROOT}/hooks/references/slop/phrases.md"
STRUCTURES_BANK="${PLUGIN_ROOT}/hooks/references/slop/structures.md"

# ── helpers ─────────────────────────────────────────────────────────────────
# Filter a bank file: drop `#`-prefixed comments and blank lines.
load_bank() {
  local f="$1"
  [ -r "$f" ] || return 1
  grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$f"
}

# Run a tier through Python `re` to dodge grep's locale-dependent collation
# ("Invalid collation character" with KO patterns under POSIX locale).
# Args: <bank_path> <content_path> -> unique matches (one per line) on stdout.
scan_tier() {
  local bank="$1"
  local content_file="$2"
  local pats
  pats="$(load_bank "$bank" 2>/dev/null)" || return 0
  [ -z "$pats" ] && return 0
  # No head cap on the scan — `count_lines` needs every unique match to decide severity.
  # The print-time cap in `emit` below limits how many markers show up in stderr.
  PYTHONIOENCODING=utf-8 python3 - "$bank" "$content_file" <<'PY' 2>/dev/null | sort -u || true
import re, sys, pathlib
bank_path, content_path = sys.argv[1], sys.argv[2]
# Drop `#` comments and blank lines; one ERE per remaining line.
pats = [
    line for line in pathlib.Path(bank_path).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
text = pathlib.Path(content_path).read_text(encoding="utf-8")
seen = set()
for p in pats:
    # Normalize POSIX classes for Python `re`: `[[:space:]]*` → `\s*`, etc.
    # Otherwise Python treats `[[:space:]]` as a char class containing literal
    # `[`, `:`, `s`, ..., `]`, which doesn't match whitespace.
    p_norm = re.sub(r"\[\[:space:\]\]", r"\\s", p)
    p_norm = re.sub(r"\[\[:alnum:\]_]\]", r"\\w", p_norm)
    p_norm = re.sub(r"\[\[:digit:\]\]", r"\\d", p_norm)
    try:
        for m in re.finditer(p_norm, text):
            seen.add(m.group(0))
    except re.error:
        # Skip malformed pattern silently (validation lives in tests/test_slop_detector.py).
        continue
# Stable order: matches appear in the order they are first seen.
for m in seen:
    print(m)
PY
}

# KO flag — anything matching the CJK Hangul range within matches.
ko_present() {
  printf '%s' "$1" | python3 -c '
import sys
print("yes" if any(0xAC00 <= ord(c) <= 0xD7A3 for c in sys.stdin.read()) else "no")
'
}

# Count non-empty match lines.
count_lines() {
  printf '%s\n' "$1" | awk 'NF' | wc -l | tr -d ' '
}

CONTENT_FILE="$(mktemp -t slop.XXXXXX)"
printf '%s' "$CONTENT" > "$CONTENT_FILE"
trap 'rm -f "$CONTENT_FILE"' EXIT

# ── inline fallback (v1 SSOT, used only if banks missing) ──────────────────
INLINE_BANK='(Certainly[!.]|I'\''d be happy to|Great question|Let'\''s dive in|delve into|leverage|robust|comprehensive|tapestry|In conclusion|Hope this helps|It'\''s worth noting|Importantly|seamlessly|unleash|empower|game-changer|cutting-edge|state-of-the-art|강력한|종합적인|다양한|꼼꼼하게|꾹꾹|핵심적으로|중요한 점은|주시하겠습니다|살펴보겠습니다)'

t1_matches=""
t2_matches=""

if [ ! -r "$PHRASES_BANK" ]; then
  echo "[slop-detector] WARN: $PHRASES_BANK not readable; using inline v1 fallback (T1 only)." >&2
  t1_matches="$(grep -oE "$INLINE_BANK" "$CONTENT_FILE" 2>/dev/null | sort -u)"
else
  t1_matches="$(scan_tier "$PHRASES_BANK" "$CONTENT_FILE")"
  if [ "$SLOP_LEVEL" -ge 2 ] && [ -r "$STRUCTURES_BANK" ]; then
    t2_matches="$(scan_tier "$STRUCTURES_BANK" "$CONTENT_FILE")"
  fi
fi

# ── severity ladder ─────────────────────────────────────────────────────────
# SSOT: these thresholds are mirrored in references/slop/scoring.md.
#   any KO phrase or KO structure                  → HIGH
#   ≥3 unique T1 OR (≥1 T1 AND ≥1 T2)              → HIGH
#   ≥2 unique T1                                    → MEDIUM
#   1 unique T1 OR 1 T2                             → LOW
#   0                                               → OK (clean)
t1_n=$(count_lines "$t1_matches")
t2_n=$(count_lines "$t2_matches")
ko_t1=$(ko_present "$t1_matches")
ko_t2=$(ko_present "$t2_matches")

severity="OK"
if [ "$ko_t1" = "yes" ] || [ "$ko_t2" = "yes" ]; then
  severity="HIGH"
elif [ "$t1_n" -ge 3 ] || { [ "$t1_n" -ge 1 ] && [ "$t2_n" -ge 1 ]; }; then
  severity="HIGH"
elif [ "$t1_n" -ge 2 ]; then
  severity="MEDIUM"
elif [ "$t1_n" -ge 1 ] || [ "$t2_n" -ge 1 ]; then
  severity="LOW"
fi

# ── emit ────────────────────────────────────────────────────────────────────
# Args: <severity> <body> -> stdout-via-stderr advisory block.
# Print-time cap of 50 markers per report section; the severity counts above
# still see every unique match (cap is presentation-only).
emit() {
  [ "$SLOP_QUIET" = "1" ] && return 0
  local sev="$1"; shift
  echo "[slop-detector] ${sev} — ${FILE}" >&2
  head -50 <<< "$*" | while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] && echo "  ${line}" >&2
  done || true
  # Show a "more" cue when the input was truncated.
  local total
  total=$(printf '%s\n' "$*" | awk 'NF' | wc -l | tr -d ' ')
  if [ "$total" -gt 50 ]; then
    echo "[slop-detector]   (+$((total-50)) more unique matches hidden; severity count already reflects every match)" >&2
  fi
  echo "[slop-detector] If intentional, ignore. Otherwise delete the phrases." >&2
}

body=""
if [ -n "$t1_matches" ]; then
  body="${body}T1 phrase:
$t1_matches
"
fi
if [ -n "$t2_matches" ]; then
  body="${body}T2 structure:
$t2_matches
"
fi

case "$severity" in
  HIGH|MEDIUM|LOW) emit "$severity" "$body" ;;
esac

if [ "$severity" = "HIGH" ] && [ "$SLOP_STRICT" = "1" ]; then
  exit 2
fi

exit 0
