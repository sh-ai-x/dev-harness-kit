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
#   bin/set-provider.sh --check-extensibility    # list files to touch to
#                                               #   add a new provider
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
#
# TO ADD A NEW PROVIDER (e.g. `openai`) — five touchpoints:
#   1. bin/set-provider.sh — append `openai` to ALLOWLIST=() near the top.
#   2. bin/set-provider.sh — add a `openai)` arm to the `case` block that
#      prints `gh secret set OPENAI_API_KEY --body '<value>'`.
#   3. .github/workflows/review.yml — extend the
#      `workflow_dispatch.inputs.review_provider.options:` list to include
#      `openai` (this is the choice list the manual dispatch UI shows).
#   4. .env.example — document the new `<NAME>_API_KEY=<value>` line and
#      add `openai` to the inline allowlist comment.
#   5. Run `bin/set-provider.sh --check-extensibility` for a live diff
#      of the files + line numbers an operator must touch today. This
#      is the fastest way to see what drifted since this list was
#      written; do not rely on these bullets alone.

set -euo pipefail

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
PROVIDER_KEY="CI_REVIEW_PROVIDER"
ALLOWLIST=(minimax anthropic deepseek)

die() { echo "error: $*" >&2; exit 1; }

# Print the live list of files + line numbers an operator must touch to
# onboard a new provider. Stable (no timestamps / no random IDs) so the
# output is safe to diff in regression tests. Uses `grep -n` against
# this script + the workflow so the answers do not go stale when the
# comment block above is hand-edited.
check_extensibility() {
  local script_path review_yml env_example
  script_path="bin/set-provider.sh"
  review_yml=".github/workflows/review.yml"
  env_example=".env.example"

  local allowlist_line case_start case_end choices_line env_key_line
  allowlist_line="$(grep -n '^ALLOWLIST=' "$script_path" | head -1 | cut -d: -f1)"
  case_start="$(grep -n '^case "\$NEW" in' "$script_path" | head -1 | cut -d: -f1)"
  case_end="$(grep -n '^esac' "$script_path" | tail -1 | cut -d: -f1)"
  # Anchor on the editable `options:` block (line 59 in review.yml). The
  # previous two-step grep fell back to a prose comment when the literal
  # `'workflow_dispatch.inputs.review_provider'` had no match, leaving an
  # operator stranded on `review.yml:28`. Anchor on the line shape itself.
  choices_line="$(grep -n '^[[:space:]]*options:' "$review_yml" | head -1 | cut -d: -f1)"
  # Anchor on the assignment line (`CI_REVIEW_PROVIDER=minimax`), not the
  # prose comment at `.env.example:25`. Same rationale as choices_line.
  env_key_line="$(grep -n '^CI_REVIEW_PROVIDER=' "$env_example" | head -1 | cut -d: -f1)"

  echo "Extensibility checklist for adding a new provider"
  echo "================================================="
  echo
  echo "Current ALLOWLIST (line ${allowlist_line:-?}) in ${script_path}:"
  echo "    ${ALLOWLIST[*]}"
  echo
  echo "Files an operator must edit (with current line numbers):"
  echo "  1. ${script_path}:${allowlist_line:-?}    # ALLOWLIST=(...)  — append the new name."
  echo "  2. ${script_path}:${case_start:-?}-${case_end:-?}  # case \"\$NEW\" in … esac  — add a <name>) arm printing 'gh secret set <NAME>_API_KEY --body <value>'."
  echo "  3. ${review_yml}:${choices_line:-?}      # workflow_dispatch.inputs.review_provider.options  — extend the choice list."
  echo "  4. ${env_example}:${env_key_line:-?}     # CI_REVIEW_PROVIDER + the matching <NAME>_API_KEY line."
  echo
  echo "After editing, run:"
  echo "  gh secret set <NAME>_API_KEY --body '<value>'   # CI-only secret"
  echo "  gh variable set CI_REVIEW_PROVIDER --body '<name>'  # so the workflow picks the right secret"
  echo
  echo "Recipe reference: bin/set-provider.sh --help  (TO ADD A NEW PROVIDER section)"
}

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
# Strips surrounding single/double quotes from the value to match
# `lib/ci_setup._read_env_key()` so the two sides agree on quoted inputs.
read_provider_from_env_file() {
  local f="$1" line key val last=""
  [ -f "$f" ] || return 0
  while IFS= read -r line; do
    case "$line" in
      "#"*|"") continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    if [ "$key" = "$PROVIDER_KEY" ]; then
      last="${val%\"}"
      last="${last#\"}"
      last="${last%\'}"
      last="${last#\'}"
    fi
  done < "$f"
  printf '%s' "$last"
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
    --check-extensibility) check_extensibility; exit 0 ;;
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
