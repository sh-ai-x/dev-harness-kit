#!/usr/bin/env bash
# pre-push.sh — local pre-push hook (Phase 1: issue-sync only).
#
# Issue #833 / PR #832 design: install a local pre-push hook that runs the
# `issue-sync` gate on every push and fails the push if the gate would
# fail — so a stale-ref push is caught locally at zero provider-token cost
# instead of after the LLM judges fire in GH Actions.
#
# Phase 1 scope (this file):
#   * Always-on stage: `issue-sync` (strict mode). Catches the PR #829
#     failure mode — stale `Issue #N` / `Closes #N` / etc. refs in commit
#     messages or PR body — for ~9s wall-clock + <10MB RSS.
#   * Opt-in stages (validate / lint / pytest / cache-decay-audit) ship
#     in Phase 2 and stay off by default (cost ~30-120s + 200-500MB RAM).
#     They are wired here against `.dev-kit/local-gates.yaml` so flipping
#     `test: true` activates pytest without editing this script.
#     See `docs/gates/local-pre-push.md` for the per-gate contract.
#
# Phase 0 contract (single source of truth between local + remote):
#   The remote `.github/workflows/issue-sync.yml` calls the SAME
#   `tools/issue_sync.py pre-push` subcommand this script calls. The
#   bash loop in the workflow is removed in favor of this subcommand so
#   local and remote can never drift.
#
# Why not branch-policy / cost-flag in this hook:
#   * `branch-policy` is already enforced by `hooks/git-guard.sh`
#     (PreToolUse Bash, blocks direct push to main).
#   * `cost-flag` is a PR label; it doesn't save provider tokens and
#     the remote gate handles labeling.
#
# Install:
#   `bin/install-pre-push.sh` — idempotent, worktree-aware (uses
#   `git rev-parse --git-common-dir` to find the shared hook root).
#
# Opt-out:
#   `--no-verify` stays available for hotfix-only escape (per
#   `feedback-pre-commit-auto-fix-loop.md`); documented but discouraged.

set -uo pipefail

# Git invokes the pre-push hook with cwd = worktree toplevel and
# GIT_DIR set to the gitdir (which for a regular repo is .git/,
# for a worktree is the shared .git/worktrees/<name>/). PWD is the
# toplevel of the working tree being pushed. Resolve to that path
# (do NOT trust `git rev-parse --show-toplevel` with GIT_DIR set —
# it returns GIT_DIR itself).
REPO_ROOT="${GIT_WORK_TREE:-${PWD}}"
if [ ! -d "$REPO_ROOT" ]; then
  printf '[pre-push] could not resolve repo root (cwd=%s).\n' "$PWD" >&2
  exit 1
fi
cd "$REPO_ROOT" || exit 1

ISSUE_SYNC="$REPO_ROOT/tools/issue_sync.py"

# Bail early if the parser is missing — the remote GH Actions workflow
# is the source of truth, and a missing parser here means the install
# script ran against a broken checkout. Exit 0 (allow push) so the
# developer's workflow isn't blocked by a missing local file.
if [ ! -f "$ISSUE_SYNC" ]; then
  printf '[pre-push] tools/issue_sync.py not found at %s; skipping local issue-sync.\n' "$ISSUE_SYNC" >&2
  exit 0
fi

# Resolve the same-repo identity (`owner/repo`) so the parser can
# build `repos/<owner>/<repo>/issues/<N>` paths for refs like `#N`
# without a cross-repo keyword. Mirrors `$GITHUB_REPOSITORY` in the
# remote workflow. Falls back to empty string if no origin remote
# is configured (the parser then degrades to error-on-unknown-ref,
# matching the strict contract). Implemented in Python (rather than
# sed) so the pattern stays portable across BSD sed (macOS default)
# and GNU sed.
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"
if [ -z "$GITHUB_REPOSITORY" ]; then
  ORIGIN_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
  if [ -n "$ORIGIN_URL" ]; then
    GITHUB_REPOSITORY="$(python3 -c "
import re,sys
m=re.match(r'^(?:git@|ssh://git@|https?://)?[^:/]+[:/](.+?)(?:\.git)?$', sys.argv[1])
print(m.group(1) if m else '')
" "$ORIGIN_URL" 2>/dev/null || true)"
  fi
fi
export GITHUB_REPOSITORY

# ── Stage 1: issue-sync (always-on, strict) ──────────────────────────────
# Reads commits via `git log origin/main..HEAD --format=%B` and runs the
# same `gh api` check the remote workflow does. Strict mode (default for
# local): any closed ref → HARD FAIL. The audit-trail lenient behavior
# stays a remote-only opt-in (PR `audit-trail` label).
printf '[pre-push] issue-sync (strict)...\n'
if ! python3 "$ISSUE_SYNC" pre-push \
    --from-log \
    --remote origin \
    --base origin/main \
    --strict \
    --json > /tmp/pre-push-issue-sync.json 2>/tmp/pre-push-issue-sync.err; then
  # Surface the structured errors to the developer. The .err file
  # carries stderr (e.g. `gh` failures); the .json carries the gate
  # verdict with `errors[]` and `warnings[]`.
  if [ -s /tmp/pre-push-issue-sync.json ]; then
    printf '\n::error title=pre-push::issue-sync found stale refs:\n'
    python3 -c "
import json,sys
d=json.load(open('/tmp/pre-push-issue-sync.json'))
for e in d.get('errors',[]):
    print(f\"  - {e['ref']}: {e['message']}\")
for w in d.get('warnings',[]):
    print(f\"  - {w['ref']}: {w['message']}\")
" >&2 || true
  fi
  if [ -s /tmp/pre-push-issue-sync.err ]; then
    printf '\n%s\n' "$(cat /tmp/pre-push-issue-sync.err)" >&2
  fi
  printf '\nFix: re-open the issue(s), drop the reference, or run `gh auth login` and re-push.\n' >&2
  rm -f /tmp/pre-push-issue-sync.json /tmp/pre-push-issue-sync.err
  exit 1
fi
rm -f /tmp/pre-push-issue-sync.json /tmp/pre-push-issue-sync.err

# ── Opt-in stages (off by default) ───────────────────────────────────────
# Read `.dev-kit/local-gates.yaml` if present. Absence = all off. Phase
# 2 expands the four supported opt-in keys (test, validate, lint,
# cache_decay_audit); this file ships with the wiring but no behavior
# until Phase 2 lands.
OPT_IN_CONFIG="$REPO_ROOT/.dev-kit/local-gates.yaml"
if [ ! -f "$OPT_IN_CONFIG" ]; then
  printf '[pre-push] opt-in stages skipped (.dev-kit/local-gates.yaml not present).\n'
  printf '\n✓ pre-push gates passed: issue-sync (opt-in stages off by default).\n'
  exit 0
fi

# Minimal opt-in config reader. Avoids pulling yq (not always
# installed). Phase 2 may swap this for a real yaml parser.
_read_opt_in() {
  local key="$1"
  python3 -c "
import re,sys
text=open('$OPT_IN_CONFIG').read()
m=re.search(r'^${key}:\s*(true|false)\s*$', text, flags=re.MULTILINE)
print(m.group(1) if m else 'false')
" 2>/dev/null || echo "false"
}

# Phase 2 wires the other three opt-in stages (validate, lint,
# cache-decay-audit) — see `docs/gates/local-pre-push.md` for the
# per-gate contract. For Phase 1, only `test: true` is honored as a
# smoke path so the documented `.dev-kit/local-gates.yaml` contract
# is exercised end-to-end without expanding the local resource budget
# (pytest -x is bounded; the others ship in Phase 2 with their full
# per-gate opt-in).
if [ "$(_read_opt_in test)" = "true" ]; then
  printf '[pre-push] pytest (opt-in)...\n'
  if ! python3 -m pytest tests/ -q --tb=line -x \
      --ignore=tests/test_review_gate.py \
      --ignore=tests/test_security_gate.py; then
    printf '\n::error title=pre-push::pytest failed\n' >&2
    exit 1
  fi
fi

ENABLED="issue-sync"
[ "$(_read_opt_in test)" = "true" ] && ENABLED="$ENABLED + pytest"
printf '\n✓ pre-push gates passed: %s.\n' "$ENABLED"
exit 0