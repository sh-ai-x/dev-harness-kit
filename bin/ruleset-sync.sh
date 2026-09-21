#!/usr/bin/env bash
# ruleset-sync.sh — PATCH the GitHub ruleset from the local SSOT.
#
# The GitHub ruleset on this repo (id 20232367, name "protect main
# (admin PAT bypass)") is the single source of truth for branch
# protection, but it lives only on GitHub: the "Allow specified
# actors to bypass required pull requests" checkbox list (the
# bypass_actors block) is invisible from the repo until a ruleset
# recreation wipes it. `.github/rulesets/protect-main.json` is the
# local SSOT for that ruleset; this script PATCHes the live
# GitHub ruleset so the SSOT and the ruleset stay in lock-step.
#
# Why-now: the ruleset JSON shape (bypass_actors, required_status_checks,
# pull_request, non_fast_forward, ...) is GitHub-stable, so the
# entire ruleset can be PATCHed atomically from one local file —
# no per-rule partial updates, no drift window. The same file is
# exercised by the regression test
# `tests/test_ruleset_bypass_actors.py` so a typo in the JSON
# surfaces at PR time, not when the live ruleset silently loses
# the bypass checkbox.
#
# Usage:
#   bin/ruleset-sync.sh                       # apply the SSOT (PATCH)
#   bin/ruleset-sync.sh --check               # exit 0 if GitHub == local
#   bin/ruleset-sync.sh --dry-run             # print what would change
#   bin/ruleset-sync.sh --id <ruleset-id>     # override ruleset id
#   bin/ruleset-sync.sh --file <path>         # override SSOT file
#   bin/ruleset-sync.sh --help
#
# Idempotent: re-running exits 0 with "no change needed" when the
# GitHub ruleset already matches the SSOT (after the first successful
# PATCH). Safe to wire into `/dev-kit:ci-setup` post-PR bootstrap.
#
# Exit codes:
#   0   PATCH applied (or no-op when --check passes)
#   1   runtime error (gh missing/unauth, jq missing, JSON parse error,
#       GitHub API failure, drift detected under --check)
#   2   invalid CLI / unknown flag
# (Previously: exit 3 for degraded mode. Collapsed to 1 with the
# degraded message preserved on stderr; CI treats 1 and 3 identically.)

set -euo pipefail

RULESET_ID="20232367"
SSOT_FILE=".github/rulesets/protect-main.json"
DRY_RUN=0
CHECK_ONLY=0
HELP=0

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

die_runtime() { echo "error: $*" >&2; exit 1; }
die_cli()     { echo "error: $*" >&2; exit 2; }
# `die_degraded` used to exit 3 (gh missing / unauth). Most CI treats
# exit 3 the same as exit 1, so the special code added noise without
# information. Collapsed to exit 1 with the degraded message preserved
# on stderr so the operator still sees the cause.
die_degraded(){ echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)        HELP=1; shift ;;
    -n|--dry-run)     DRY_RUN=1; shift ;;
    --check)          CHECK_ONLY=1; shift ;;
    --id)             RULESET_ID="${2:-}"; shift 2 ;;
    --file)           SSOT_FILE="${2:-}"; shift 2 ;;
    -*)               die_cli "unknown flag: $1 (try --help)" ;;
    *)                die_cli "unexpected positional arg: $1 (try --help)" ;;
  esac
done

if [[ "$HELP" == "1" ]]; then
  usage
  exit 0
fi

# Pre-flight: gh + jq + the SSOT file must all exist.
command -v gh >/dev/null 2>&1 || die_degraded "gh binary not found in PATH"
command -v jq >/dev/null 2>&1 || die_runtime "jq binary not found in PATH; install jq and re-run"
[[ -f "$SSOT_FILE" ]] || die_runtime "SSOT file not found: $SSOT_FILE (was it renamed?)"

# Parse out the four PATCH fields the GitHub REST ruleset API accepts
# (the SSOT uses GitHub's export shape verbatim, so this is a 1:1 lift).
# Refs: https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset
RULESET_PAYLOAD="$(jq -c '{
  name: .name,
  target: .target,
  enforcement: .enforcement,
  conditions: .conditions,
  rules: .rules,
  bypass_actors: .bypass_actors
}' "$SSOT_FILE")" || die_runtime "SSOT file is not valid JSON: $SSOT_FILE"

# Auto-detect owner/repo from the git remote — mirrors
# `lib/gates_state.detect_owner_repo` so the operator never has to
# pass the repo name. Falls back to `gh repo view` when no git
# remote is configured.
detect_repo() {
  local from_remote
  from_remote="$(git remote get-url origin 2>/dev/null || true)"
  if [[ -n "$from_remote" ]]; then
    if [[ "$from_remote" =~ github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$ ]]; then
      printf '%s/%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
      return 0
    fi
  fi
  gh repo view --json nameWithOwner --jq '.nameWithOwner // ""' 2>/dev/null || true
}

REPO="$(detect_repo)"
[[ -n "$REPO" ]] || die_degraded "could not detect owner/repo from git remote or gh auth context (run 'gh auth login')"

echo "SSOT: $SSOT_FILE"
echo "Repo: $REPO"
echo "Ruleset id: $RULESET_ID"

# Fetch the current ruleset so we can compare. Under --check we
# always do this; under --dry-run and the default PATCH we still
# fetch so we can emit a "no-op" early-exit when the live ruleset
# already matches the SSOT (idempotency).
CURRENT="$(gh api "/repos/${REPO}/rulesets/${RULESET_ID}" 2>&1)" \
  || die_runtime "gh api GET /repos/${REPO}/rulesets/${RULESET_ID} failed: ${CURRENT:-<empty>}"

# Strip volatile fields the server echoes back (etag, node_id,
# created_at, updated_at, ...) so the equality check isn't fooled
# by metadata. Only the 6 fields we PATCH matter for drift.
SAME_PROJECTION='{name, target, enforcement, conditions, rules, bypass_actors}'
CURRENT_PROJECTED="$(printf '%s' "$CURRENT" | jq -c "$SAME_PROJECTION")"

# Drift check (under all modes — the operator wants to know whether
# the SSOT is already applied before we PATCH).
if [[ "$CURRENT_PROJECTED" == "$RULESET_PAYLOAD" ]]; then
  echo "  ✓ GitHub ruleset already matches the SSOT (no-op)"
  exit 0
fi

# Under --check we exit 1 here so CI / pre-PR hooks can flag drift.
if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "  ✗ drift detected — GitHub ruleset does not match the SSOT"
  echo "    run: bin/ruleset-sync.sh --dry-run   # preview the diff"
  echo "    run: bin/ruleset-sync.sh             # apply the PATCH"
  exit 1
fi

# --dry-run prints the diff body the PATCH would send and exits 0.
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  → would PATCH the following payload to /repos/${REPO}/rulesets/${RULESET_ID}:"
  printf '%s\n' "$RULESET_PAYLOAD" | jq .
  exit 0
fi

# Apply the PATCH. Per-run response body lives in a `mktemp` file so
# concurrent runs cannot clobber each other on a hardcoded path; the
# EXIT trap cleans up on success, failure, or signal.
TMP_RESPONSE="$(mktemp)"
trap 'rm -f "$TMP_RESPONSE"' EXIT

HTTP="$(gh api --method PATCH "/repos/${REPO}/rulesets/${RULESET_ID}" \
  --input - <<<"$RULESET_PAYLOAD" \
  --include -H "Accept: application/vnd.github+json" \
  -o "$TMP_RESPONSE" -w '%{http_code}' 2>&1)" \
  || die_runtime "gh api PATCH /repos/${REPO}/rulesets/${RULESET_ID} failed: $HTTP"

# 200 (updated) and 422 (validation error from server) are the
# two outcomes that mean the server actually saw the request. Other
# codes (401/403/404/5xx) are fatal here — exit 1 so the operator
# is not silently left with a "ruleset-sync.sh OK" message and an
# unchanged ruleset.
case "$HTTP" in
  200) echo "  ✓ PATCH applied (HTTP 200)" ;;
  422)
    echo "  ✗ GitHub rejected the payload (HTTP 422):" >&2
    jq -r '.message // .errors // .' "$TMP_RESPONSE" 2>/dev/null >&2 || true
    exit 1
    ;;
  *)
    echo "  ✗ unexpected HTTP $HTTP from PATCH:" >&2
    head -c 500 "$TMP_RESPONSE" 2>/dev/null >&2 || true
    exit 1
    ;;
esac
