#!/usr/bin/env bash
# worktree-classify.sh — extract the two predicates that decide whether
# a `git worktree list --porcelain` record is a prune candidate, plus
# the 8-guard safety chain that gates the auto-prune removal.
#
# Extracted from hooks/worktree-janitor-session-start.sh by
# inspect-pass4 (finding s9, s13, o6 — 10-level nesting + 7-counter
# data clump + 9 overlapping counters for a binary decision) on
# 2026-09-23. The main loop previously inlined 80+ lines of nested
# predicate logic, mutating 7 globals per record; the main loop is
# now a 5-line per-record call to these two helpers, which return
# plain strings instead of mutating caller state.
#
# API:
#
#   classify_worktree_record <record> [stale_days]
#       Parse one porcelain record (multi-line text), run the two
#       predicates, and print three lines to stdout:
#           merged=<0|1>
#           stale=<0|1>
#           age_days=<int>
#           path=<worktree_path>
#           branch=<branch-short-name>
#       (record fields omitted when not present)
#       Returns 0 if the record parsed (even if neither predicate
#       matched), 1 if the record had no worktree path.
#
#   should_auto_prune <merged> <stale> <wt_path> <current_wt>
#                     <main_wt> <safe_remove> <removed_count>
#                     <auto_prune_max> <git_status_porcelain>
#       Run the 8-guard safety chain. Prints "yes" or "no" to stdout.
#       Guards (all must pass):
#           1. AUTO_PRUNE mode is on          (caller passes stale=1)
#           2. stale=1 (not merged-only)       (caller passes stale=1)
#           3. removed_count < auto_prune_max
#           4. wt_path is non-empty and a directory
#           5. safe_remove is non-empty and executable
#           6. wt_path != current_wt (not the session's cwd)
#           7. wt_path != main_wt (not the main checkout)
#           8. wt_path has a clean git status
#       The 9th guard "wt_path is not the session cwd" was previously
#       checked via the captured `RECORDS_SEEN` sentinel that short-
#       circuits the loop; this helper is invoked AFTER the loop has
#       decided to attempt the prune, so it does not duplicate.

classify_worktree_record() {
  local record="$1"
  local stale_days="${2:-7}"

  # `git worktree list --porcelain` uses SPACE-separated fields (the
  # tag is the first token, value is everything after the first space).
  # `awk $2` would truncate paths containing spaces, so we strip the
  # leading tag and keep the rest of the line.
  local wt_path branch merged stale age_days last_ts now
  wt_path="$(printf '%s\n' "$record" | sed -n 's/^worktree //p' | head -1)"
  branch="$(printf '%s\n' "$record" | awk '$1=="branch"{print $2}' | sed 's#^refs/heads/##')"
  if [ -z "$wt_path" ] || [ -z "$branch" ]; then
    return 1
  fi

  merged=0
  stale=0
  age_days=0
  if git merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
    merged=1
  elif [[ "$branch" == fix/classify-request-* ]]; then
    last_ts="$(git log -1 --pretty=%ct "$branch" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    if [ "${last_ts:-0}" -gt 0 ] 2>/dev/null; then
      age_days=$(( (now - last_ts) / 86400 ))
      if [ "${age_days:-0}" -gt "${stale_days}" ]; then
        merged=1
        stale=1
      fi
    fi
  fi

  printf 'merged=%d\nstale=%d\nage_days=%d\npath=%s\nbranch=%s\n' \
    "$merged" "$stale" "$age_days" "$wt_path" "$branch"
  return 0
}

should_auto_prune() {
  local merged="$1" stale="$2" wt_path="$3" current_wt="$4" \
        main_wt="$5" safe_remove="$6" removed_count="$7" \
        auto_prune_max="$8" git_status_porcelain="$9"

  # Predicate 1 (merged-into-main) is REPORTED ONLY; never auto-removed
  # because `--is-ancestor` is reflexive and a fresh branch off main
  # would match. Only predicate 2 (stale=1) flows through auto-apply.
  if [ "$stale" != "1" ]; then
    printf 'no\n'; return 0
  fi
  # The 7 remaining guards. Order is from cheapest (string compare) to
  # most expensive (git status --porcelain). Each guard short-circuits
  # with `no` so the helper returns promptly on the first failure.
  if [ -z "$wt_path" ] || [ ! -d "$wt_path" ]; then
    printf 'no\n'; return 0
  fi
  if [ -z "$safe_remove" ] || [ ! -x "$safe_remove" ]; then
    printf 'no\n'; return 0
  fi
  if [ "$wt_path" = "$current_wt" ]; then
    printf 'no\n'; return 0
  fi
  if [ "$wt_path" = "$main_wt" ]; then
    printf 'no\n'; return 0
  fi
  if [ -n "$auto_prune_max" ] && [ "${removed_count:-0}" -ge "${auto_prune_max}" ]; then
    printf 'no\n'; return 0
  fi
  if [ -n "$git_status_porcelain" ]; then
    # Caller passed non-empty status — worktree has dirty files. Don't
    # auto-remove; let git's dirty-tree refusal be the backstop.
    printf 'no\n'; return 0
  fi
  printf 'yes\n'
  return 0
}