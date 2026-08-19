# l4-todo-scan v1 — MARKER bank (SSOT, line-delimited ERE)
#
# Format: one POSIX ERE per non-comment line. `# ` and blank lines are
# skipped at load time via `grep -vE '^[[:space:]]*#|^[[:space:]]*$'`.
#
# Patterns are detection-only; the deliverable is the marker, not the fix.
# Iron Law #4 prohibits these tokens in committed code (see
# iron-laws/index.md line 8).
#
# KO patterns are kept literal (no POSIX class wrappers) so they match
# directly under Python re without locale-dependent collation issues.

# === English hard markers (word-bounded to avoid false positives) ===
\bTODO\b
\bFIXME\b
\bXXX\b
\bHACK\b

# === English soft markers (phrases from the iron-law text) ===
we'll extend later
this is a starting point
to be implemented

# === English standalone soft tokens (word-bounded where ambiguous) ===
\bplaceholder\b
\bstub\b

# === KO soft markers ===
나중에
추후
임시
시작점
플레이스홀더
스텁
