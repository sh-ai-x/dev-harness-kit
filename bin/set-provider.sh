#!/usr/bin/env bash
# set-provider.sh — switch the local CI review/security provider explicitly.
#
# Why: provider selection moved off the tracked file
# `.github/ci-review-provider.txt` so the same repo can be used by
# different operators with different providers (no committed default).
# Local selection now lives in `.env:CI_REVIEW_PROVIDER` (gitignored,
# per-user). CI selection lives in the GitHub repo variable
# `vars.CI_REVIEW_PROVIDER` (per-repo). This script manages the local
# half — `bin/set-provider.sh <provider>` upserts the key in `.env`,
# prints a diff, and reminds the operator to set the matching GitHub
# repo variable + API-key secret.
#
# Usage:
#   bin/set-provider.sh                          # show current local provider
#   bin/set-provider.sh minimax                  # switch local provider
#   bin/set-provider.sh anthropic --dry-run      # show what would change
#   bin/set-provider.sh --show                   # alias for no-arg form
#   bin/set-provider.sh --help
#
# Allowlist: minimax, anthropic, deepseek (must match the choice list
# declared in .github/workflows/review.yml -> workflow_dispatch.inputs).
#
# The matching *_API_KEY secret must be set on the GitHub repo before CI
# can actually use a given provider:
#   gh secret set MINIMAX_API_KEY    --body "<value>"
#   gh secret set ANTHROPIC_API_KEY  --body "<value>"
#   gh secret set DEEPSEEK_API_KEY   --body "<value>"
# And the matching CI_REVIEW_PROVIDER repo variable so the workflow
# knows which secret to read:
#   gh variable set CI_REVIEW_PROVIDER --body "<provider>"

set -euo pipefail

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
PROVIDER_KEY="CI_REVIEW_PROVIDER"
ALLOWLIST=(minimax anthropic deepseek)

die() { echo "error: $*" >&2; exit 1; }

show_help() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
}

# Resolve repo root (works in main checkout and worktrees alike).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repo"
cd "$REPO_ROOT"

is_allowed() {
  local p="$1"
  for a in "${ALLOWLIST[@]}"; do
    [ "$p" = "$a" ] && return 0
  done
  return 1
}

# Read CI_REVIEW_PROVIDER from .env (last occurrence wins; comments and
# blanks ignored). Echoes the value, or empty string when unset.
# Delegates to `lib/read_env_key.read_env_key` (issue #711) so the bash
# and Python sides cannot drift on quoting / `export` prefix / CRLF
# edge cases. The helper's full rules are pinned by
# tests/test_read_env_key.py; the previous in-bash parser was deleted.
read_provider_from_env_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  python3 -c "from lib.read_env_key import read_env_key; from pathlib import Path; import sys; print(read_env_key(Path(sys.argv[1]), sys.argv[2]), end='')" "$f" "$PROVIDER_KEY"
}

# Echo the current effective provider: process env → .env → .env.example
# (mirrors `lib/ci_setup.read_provider()` so `bin/set-provider.sh --show`
# and `ci-doctor` never disagree about the active value). Direct
# reference is intentional — `${!PROVIDER_KEY:-}` (indirect expansion)
# would silently typo and echo the key name when the env var is unset,
# which is exactly the bug this avoids.
current_provider() {
  local from_env="${CI_REVIEW_PROVIDER:-}"
  if [ -z "$from_env" ] && [ -f "$ENV_FILE" ]; then
    from_env="$(read_provider_from_env_file "$ENV_FILE")"
  fi
  if [ -z "$from_env" ] && [ -f "$ENV_EXAMPLE" ]; then
    from_env="$(read_provider_from_env_file "$ENV_EXAMPLE")"
  fi
  printf '%s' "$from_env"
}

# Upsert CI_REVIEW_PROVIDER in .env, preserving all other lines verbatim.
# Creates .env from .env.example when neither exists (so first-time
# operators get a complete template). Idempotent on re-run.
upsert_env_file() {
  local new_value="$1" current_file saw_key line key val tmp
  if [ -f "$ENV_FILE" ]; then
    current_file="$ENV_FILE"
  elif [ -f "$ENV_EXAMPLE" ]; then
    current_file="$ENV_EXAMPLE"
    echo "note: $ENV_FILE missing; bootstrapping from $ENV_EXAMPLE"
  else
    die "neither $ENV_FILE nor $ENV_EXAMPLE exists; cannot manage provider"
  fi

  tmp="$(mktemp)"
  # Copy every line. The first CI_REVIEW_PROVIDER= match is rewritten
  # with the new value; any subsequent matches are dropped so a manual
  # edit that left duplicates collapses to one line on next switch.
  # Track whether we saw one so we can append if missing.
  saw_key=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "#"*|"")
        printf '%s\n' "$line" >> "$tmp"
        continue
        ;;
    esac
    key="${line%%=*}"
    if [ "$key" = "$PROVIDER_KEY" ]; then
      if [ "$saw_key" = "0" ]; then
        printf '%s=%s\n' "$PROVIDER_KEY" "$new_value" >> "$tmp"
        saw_key=1
      fi
      # Subsequent matches: drop the line (do not write).
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$current_file"
  if [ "$saw_key" = "0" ]; then
    printf '%s=%s\n' "$PROVIDER_KEY" "$new_value" >> "$tmp"
  fi

  # If we bootstrapped from .env.example, write to .env (not back to
  # .env.example — that's the tracked template).
  if [ "$current_file" != "$ENV_FILE" ]; then
    cp "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    echo "wrote $ENV_FILE (new file)"
  else
    mv "$tmp" "$ENV_FILE"
    echo "updated $ENV_FILE"
  fi
}

# Parse args. Support provider as first positional, then flags.
PROVIDER_ARG=""
DRY_RUN=0
SHOW_ONLY=0

if [ $# -eq 0 ]; then
  SHOW_ONLY=1
else
  case "$1" in
    -h|--help) show_help; exit 0 ;;
    --show)    SHOW_ONLY=1 ;;
    --dry-run) DRY_RUN=1; PROVIDER_ARG="${2:-}"; [ -n "$PROVIDER_ARG" ] || die "--dry-run requires a provider name" ;;
    -*)        die "unknown flag: $1 (try --help)" ;;
    *)         PROVIDER_ARG="$1"
               # Allow --dry-run as second arg too.
               if [ $# -ge 2 ] && [ "${2:-}" = "--dry-run" ]; then DRY_RUN=1; fi ;;
  esac
fi

if [ "$SHOW_ONLY" = "1" ]; then
  CUR="$(current_provider)"
  if [ -z "$CUR" ]; then
    echo "current: (unset) — no provider declared in $ENV_FILE or process env"
  else
    echo "current: $CUR"
  fi
  echo "source:  $ENV_FILE (local) + vars.CI_REVIEW_PROVIDER (CI)"
  echo "allowlist: ${ALLOWLIST[*]}"
  echo "to switch: bin/set-provider.sh <provider>"
  exit 0
fi

# Switch path: validate first, fail fast.
is_allowed "$PROVIDER_ARG" || die "invalid provider '$PROVIDER_ARG'; allowed: ${ALLOWLIST[*]}"

CURRENT="$(current_provider)"
NEW="$PROVIDER_ARG"

# Noop check is gated on `.env` actually existing — current_provider()
# falls back to .env.example, so a fresh clone with no .env would
# otherwise report "already <whatever-template-says>" and skip the
# bootstrap. Bootstrap must run on a missing .env regardless.
if [ -f "$ENV_FILE" ] && [ "$CURRENT" = "$NEW" ]; then
  echo "already $NEW; nothing to do."
  exit 0
fi

echo "current: ${CURRENT:-(unset)}"
echo "new:     $NEW"
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] would upsert $PROVIDER_KEY=$NEW in $ENV_FILE"
  if [ -f "$ENV_FILE" ]; then
    TMP="$(mktemp)"
    trap 'rm -f "$TMP"' EXIT
    awk -v key="$PROVIDER_KEY" -v val="$NEW" '
      BEGIN { saw = 0 }
      /^#/ || /^$/ { print; next }
      {
        k = $0; sub(/=.*/, "", k)
        if (k == key) { print key"="val; saw = 1; next }
        print
      }
      END { if (!saw) print key"="val }
    ' "$ENV_FILE" > "$TMP"
    diff -u "$ENV_FILE" "$TMP" | sed 's/^/[dry-run] /' || true
    rm -f "$TMP"
  else
    echo "[dry-run] $ENV_FILE does not exist yet; would bootstrap from $ENV_EXAMPLE"
  fi
  exit 0
fi

# Apply. .env is gitignored so there's nothing to commit; just print the
# effective diff for review.
upsert_env_file "$NEW"

echo
echo "next steps:"
echo "  # Local: nothing — .env is read by your tools on next run."
echo "  # CI:    set the matching repo variable + secret:"
echo "  gh variable set CI_REVIEW_PROVIDER --body '$NEW'"
case "$NEW" in
  minimax)   echo "  gh secret   set MINIMAX_API_KEY   --body '<value>'" ;;
  anthropic) echo "  gh secret   set ANTHROPIC_API_KEY --body '<value>'" ;;
  deepseek)  echo "  gh secret   set DEEPSEEK_API_KEY  --body '<value>'" ;;
esac
