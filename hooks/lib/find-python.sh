#!/usr/bin/env bash
# find-python.sh — resolve the first available Python 3 interpreter
# from {python3, python, py}. Shared by hooks/sub-agent-handoff.sh
# and hooks/worktree-auto-cut.sh (post-extract) — the linear-fast-path
# helper (hooks/lib/linear-fast-path.sh) bakes this lookup into the
# fast-path body directly.
#
# Extracted by inspect-pass4 (finding p5-p7, 2026-09-23) to eliminate
# the byte-identical 5-line `for py in python3 python py; do ... done`
# loop. Two consumers remained after Pass 2 consolidated the 4 linear
# hooks into linear-fast-path.sh.
#
# Why no caching: the lookup is ~30ms (one `command -v` per candidate)
# and runs only on hook fires (not per-keystroke), so caching adds
# complexity for marginal savings.
#
# Usage:
#   source "${BASH_SOURCE[0]%/*}/lib/find-python.sh"
#   PY="$(find_python)" || { echo "no python3 in PATH" >&2; exit 0; }
#   "$PY" /path/to/script.py
#
# Returns 0 on success (prints the resolved path to stdout) or 1
# when no candidate is found. The caller decides the failure mode
# (silent skip vs. log warning).

find_python() {
  local py
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      printf '%s\n' "$py"
      return 0
    fi
  done
  return 1
}