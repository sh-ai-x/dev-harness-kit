#!/usr/bin/env bash
# log-retention.sh — gzip old Claude Code session transcripts, archive-delete
# stale .jsonl.gz files. Pure bash; mirrors bin/worktree-prune.sh flag shape.
#
# Session transcripts live at ~/.claude/projects/**/*.jsonl. After 30 days
# (LOG_RETENTION_DAYS), gzip in place. After 180 days
# (LOG_RETENTION_ARCHIVE_DAYS), delete the .jsonl.gz to bound disk use.
#
# Usage: bin/log-retention.sh [--dry-run|-y] [--days N] [--archive-days N] [--help]
#
# Env vars:
#   LOG_RETENTION_DAYS          (default 30)
#   LOG_RETENTION_ARCHIVE_DAYS   (default 180)
#   LOG_RETENTION_ROOT           (default ~/.claude/projects — overridable)
#   APPROVE=1                    (same as -y — execute instead of dry-run)
#
# Exit codes:
#   0 = success / dry-run printed
#   1 = runtime error
#   2 = invalid CLI
#   3 = partial success (some actions failed)

set -euo pipefail

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//' | awk 'NF'
}

DAYS="${LOG_RETENTION_DAYS:-30}"
ARCHIVE_DAYS="${LOG_RETENTION_ARCHIVE_DAYS:-180}"
ROOT="${LOG_RETENTION_ROOT:-$HOME/.claude/projects}"
DRY_RUN=1   # default to dry-run for safety (this script is destructive)
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)             DRY_RUN=0 ;;
    -n|--dry-run)         DRY_RUN=1 ;;
    --days)               [ $# -ge 2 ] || { echo "error: --days requires N" >&2; exit 2; }
                          DAYS="$2"; shift ;;
    --archive-days)       [ $# -ge 2 ] || { echo "error: --archive-days requires N" >&2; exit 2; }
                          ARCHIVE_DAYS="$2"; shift ;;
    -h|--help)            usage; exit 0 ;;
    *)                    echo "error: unknown flag $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
[ "${APPROVE:-0}" = "1" ] && DRY_RUN=0

if [ ! -d "$ROOT" ]; then
  echo "log-retention: $ROOT not found, nothing to do"
  exit 0
fi

now_epoch="$(date +%s)"
day_secs=86400
gzip_secs=$((DAYS * day_secs))
archive_secs=$((ARCHIVE_DAYS * day_secs))

# Race guard: skip files modified within the last 60 s so we don't gzip a
# transcript that's still being written by an active session.
skip_recent_secs=60

reclaim_bytes=0
retain_count=0
gzip_count=0
delete_count=0
fail_count=0

while IFS= read -r -d '' path; do
  mtime="$(stat -f %m "$path" 2>/dev/null || stat -c %Y "$path" 2>/dev/null || echo 0)"
  age=$((now_epoch - mtime))
  if [ "$age" -lt "$skip_recent_secs" ]; then
    retain_count=$((retain_count + 1))
    continue
  fi
  case "$path" in
    *.jsonl.gz)
      if [ "$age" -ge "$archive_secs" ]; then
        size="$(stat -f %z "$path" 2>/dev/null || stat -c %s "$path" 2>/dev/null || echo 0)"
        if [ "$DRY_RUN" = "1" ]; then
          echo "would delete  $path ($(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B"))"
        else
          if rm -f "$path"; then
            delete_count=$((delete_count + 1))
            reclaim_bytes=$((reclaim_bytes + size))
          else
            fail_count=$((fail_count + 1))
          fi
        fi
      else
        retain_count=$((retain_count + 1))
      fi
      ;;
    *.jsonl)
      if [ "$age" -ge "$gzip_secs" ]; then
        size="$(stat -f %z "$path" 2>/dev/null || stat -c %s "$path" 2>/dev/null || echo 0)"
        if [ "$DRY_RUN" = "1" ]; then
          echo "would gzip    $path ($(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B"))"
        else
          if gzip -6 -n "$path"; then
            gzip_count=$((gzip_count + 1))
            # gzipped files are ~30% of original size in this corpus; rough
            # reclaim estimate uses the original size for visibility.
            reclaim_bytes=$((reclaim_bytes + size * 70 / 100))
          else
            fail_count=$((fail_count + 1))
          fi
        fi
      else
        retain_count=$((retain_count + 1))
      fi
      ;;
    *)
      retain_count=$((retain_count + 1))
      ;;
  esac
done < <(find "$ROOT" -mindepth 1 -maxdepth 3 -type f \( -name '*.jsonl' -o -name '*.jsonl.gz' \) -print0 2>/dev/null)

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "log-retention: would_reclaim=$(numfmt --to=iec "$reclaim_bytes" 2>/dev/null || echo "${reclaim_bytes}B") retain_count=${retain_count} (dry-run; pass -y to execute)"
else
  echo
  echo "log-retention: gzipped=${gzip_count} deleted=${delete_count} retained=${retain_count} reclaimed=$(numfmt --to=iec "$reclaim_bytes" 2>/dev/null || echo "${reclaim_bytes}B")"
fi

if [ "$fail_count" -gt 0 ]; then
  echo "log-retention: ${fail_count} action(s) failed" >&2
  exit 3
fi
exit 0
