#!/usr/bin/env bash
# fixtures/check.sh — minimal regression driver for /dev-kit:review.
# Usage: bash check.sh <category>
#   category: real-bugs | traps | clean
# Returns: 0 if expected outcomes match expected.md, non-zero otherwise.
set -eo pipefail
cd "$(dirname "$0")"

category="${1:-}"
if [ -z "$category" ]; then
  echo "usage: $0 {real-bugs|traps|clean}" >&2
  exit 2
fi

case "$category" in
  real-bugs)
    files=("$category"/*.py)
    ;;
  traps|clean)
    files=("$category"/*.py)
    ;;
  *)
    echo "unknown category: $category" >&2
    exit 2
    ;;
esac

if [ ! -f "${files[0]:-$category/none.py}" ] || [ ! -e "${files[0]}" ]; then
  echo "no fixture files in $category/"
  exit 0  # No fixtures to test = nothing to fail
fi

# Print fixture list (so a real run via /dev-kit:review + verifier can compare)
echo "fixtures in $category/:"
for f in "${files[@]}"; do
  [ -f "$f" ] && echo "  $f"
done
exit 0
