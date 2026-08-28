#!/usr/bin/env bash
# setup-git-defaults.sh — write the operator-global git defaults that
# dev-kit assumes on every consumer repo.
#
# Why: the dev-kit workflow runs `git pull --rebase origin main` and
# refuses to start on a dirty tree without `rebase.autoStash=true`.
# Git's default is `rebase.autoStash=false`, so every operator hits
# "cannot pull with rebase: You have unstaged changes" the first time
# they edit a file before pulling. The fix is one line — this script
# applies it once during bootstrap and stays idempotent on re-run.
#
# Why-now: bootstrap is the natural single touchpoint because it is
# the one place every new dev-kit consumer runs exactly once. Adding
# a hook in `hooks/hooks.json` would fire every SessionStart and
# re-mutate state on every reload, which is the wrong shape — this
# is a one-shot preference write, not session telemetry.
#
# Usage:
#   bin/setup-git-defaults.sh                  # apply (idempotent)
#   bin/setup-git-defaults.sh --dry-run        # print what would change
#   bin/setup-git-defaults.sh --check          # exit 0 if all set, 1 if any missing
#   bin/setup-git-defaults.sh --help
#
# Allowlist of settings (single source of truth — extend SETTINGS=()
# below to add more):
#   rebase.autoStash=true   transparent stash/pop during rebase
#                           (fixes "cannot pull with rebase" on dirty tree)
#   pull.rebase=true        make `git pull` default to rebase (matches
#                           the dev-kit workflow + hooks/git-guard.sh)
#
# Idempotent: re-running exits 0 with "already set; nothing to do"
# when every key is already at the expected value. Safe to wire into
# bootstrap + invoke from setup scripts + run by hand.
#
# Exit codes:
#   0   all keys at expected value (or successfully set)
#   1   runtime error (git missing, --check found missing keys, etc.)
#   2   invalid CLI / unknown flag

set -euo pipefail

# Single source of truth for which keys belong in the operator's
# `~/.gitconfig`. Order is preserved in --dry-run and --check output.
SETTINGS=(
  "rebase.autoStash=true"
  "pull.rebase=true"
)

die() { echo "error: $*" >&2; exit 1; }

show_help() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# Read the current value of <key> from the operator's global git config.
# `git config --global --get` exits 1 when the key is unset — suppress
# that with `|| true` so `set -e` does not abort the caller. Echoes
# the value, or empty string when unset.
current_value() {
  git config --global --get "$1" 2>/dev/null || true
}

# Pre-flight: git must be installed.
command -v git >/dev/null 2>&1 || die "git binary not found in PATH; install git and re-run"

# Arg parsing.
DRY_RUN=0
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)        show_help; exit 0 ;;
    -n|--dry-run)     DRY_RUN=1; shift ;;
    --check)          CHECK_ONLY=1; shift ;;
    -*)               die "unknown flag: $1 (try --help)" ;;
    *)                die "unexpected positional arg: $1 (try --help)" ;;
  esac
done

echo "Git global config: $HOME/.gitconfig"
echo

changed=0
missing=0
for kv in "${SETTINGS[@]}"; do
  key="${kv%%=*}"
  expected="${kv#*=}"
  before="$(current_value "$key")"

  if [[ "$CHECK_ONLY" == "1" ]]; then
    if [[ "$before" == "$expected" ]]; then
      echo "  ✓ $key=$expected"
    else
      echo "  ✗ $key=$expected (currently: '${before:-<unset>}')"
      missing=$((missing + 1))
    fi
    continue
  fi

  if [[ "$before" == "$expected" ]]; then
    echo "  ✓ $key=$expected (already set; nothing to do)"
    continue
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  → would set $key=$expected (currently: '${before:-<unset>}')"
    continue
  fi

  git config --global "$key" "$expected"
  echo "  ✓ set $key=$expected"
  changed=$((changed + 1))
done

echo
if [[ "$CHECK_ONLY" == "1" ]]; then
  if [[ "$missing" -gt 0 ]]; then
    echo "$missing setting(s) missing — run bin/setup-git-defaults.sh to apply."
    exit 1
  fi
  echo "All ${#SETTINGS[@]} setting(s) present."
  exit 0
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "(dry-run: no changes made)"
  exit 0
fi

if [[ "$changed" -eq 0 ]]; then
  echo "All ${#SETTINGS[@]} setting(s) already present; nothing to do."
else
  echo "Updated $changed setting(s)."
fi