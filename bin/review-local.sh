#!/usr/bin/env bash
# review-local.sh — Local equivalent of the GH-Actions review + maintenance
# workflow orchestration. Saves Action minutes when private repos hit
# the GH-Actions budget cap; same verdict extraction + combined gate +
# L3-evidence check + auto-approve as `review.yml` + `maintenance.yml`.
#
# This script is ADDITIVE: the GH-Actions workflows are unchanged. Use
# this when you want to run the same review pipeline locally without
# consuming a GitHub Actions run.
#
# Usage:
#   bin/review-local.sh --pr N
#   bin/review-local.sh --pr N --provider anthropic
#   bin/review-local.sh --pr N --auto-approve
#   bin/review-local.sh --pr N --review-only
#   bin/review-local.sh --pr N --maintenance-only --dry-run
#   bin/review-local.sh --help
#
# Flags:
#   --pr N                PR number to review (required).
#   --provider NAME       minimax | anthropic | deepseek (default: from
#                         .env:CI_REVIEW_PROVIDER via lib/ci_setup.read_provider).
#                         Applied BEFORE the API key is resolved so the
#                         flag always wins, even on a process env that
#                         has the .env provider's key already loaded.
#   --auto-approve        Cast `gh pr review --approve` when combined
#                         verdict = Approve AND L3-evidence gate passes
#                         AND PR touches production code AND every
#                         enabled judge produced a parseable verdict.
#                         A missing/empty verdict REFUSES auto-approve
#                         (a gate that approves when its input is missing
#                         is worse than no gate). Default: OFF.
#   --review-only         Run only /dev-kit:review (skip security + maintenance).
#   --security-only       Run only /dev-kit:security.
#   --maintenance-only    Run only /dev-kit:maintenance.
#   --all                 Run all three (default).
#   --no-touch-probe      Treat every PR as production-touching (skip
#                         the auto-detect file-path probe) but STILL
#                         run the L3-evidence pytest-tail regex. The
#                         flag does not disable the gate; it disables
#                         only the upstream detection. Default: auto-detect.
#   --dry-run             Print the planned env + commands + verdict post
#                         WITHOUT invoking `claude` or `gh pr review`.
#                         Useful for CI-budget planning + smoke tests.
#   -h, --help            Show this help.
#
# Verdict extraction model:
#   The script captures each `claude -p "$prompt"` invocation's stdout
#   into a per-skill variable, then pipes that variable directly into
#   `python3 -m lib.maintenance_gate --extract-verdict-from-stdin`.
#   This is the same helper the workflow shells out to (so the
#   extractor stays single-sourced). It is more robust than reading
#   PR comments because local `claude -p` has no `claude[bot]` login
#   to filter on, and the workflow's per-job extraction relied on
#   temporal locality (each job's judge was its own "last comment")
#   which a sequential local run cannot replicate.
#
#   The agent still posts inline comments directly via `gh pr comment`
#   for the human reviewer; the captured stdout is for the gate only.
#
# Provider switch (matches bin/set-provider.sh + the workflow's choice
# list). The corresponding API key must be in `.env` or the process env
# under the key name `lib/ci_setup.required_secrets_for_provider()` returns,
# e.g. `MINIMAX_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY`.

set -euo pipefail

# ---------------------------------------------------------------------------
# Repo root + helpers.
# ---------------------------------------------------------------------------
# Resolve REPO_ROOT (issue #619). Two-pass strategy:
#   1. Prefer cwd's git toplevel -- this is the cwd-independent path
#      (works whether the script lives at `<repo>/bin/review-local.sh`
#      or in a plugin cache, as long as the user is cd'd into a repo).
#   2. Fall back to BASH_SOURCE's git toplevel -- covers the case
#      where the user runs the script from OUTSIDE the repo (e.g. smoke
#      test from /tmp). The script then finds the repo by walking up
#      from its own location.
# The previous BASH_SOURCE-only derivation hardcoded `<repo>/bin/` and
# failed when the script was symlinked or copied into a non-git directory
# (e.g. plugin cache without a .git marker).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# `|| true` is required: `set -e` would kill the script on the non-zero
# exit from `git rev-parse` when cwd is not a git repo. We deliberately
# probe and fall back, so the failure is the expected branch, not a
# script-killing error.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && git rev-parse --show-toplevel 2>/dev/null || true)"
fi
[ -n "$REPO_ROOT" ] || { echo "error: not in a git repo" >&2; exit 1; }
cd "$REPO_ROOT"

# shellcheck source=lib/review_local_lib.sh
. "$REPO_ROOT/lib/review_local_lib.sh"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "  $*"; }

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
}

# `--help` MUST short-circuit before the manifest check below. The
# cwd-independence smoke test (TestCwdIndependence) installs a minimal
# consumer (bin/ + lib/ only, no .claude-plugin/) and runs the script
# with --help from a directory outside that consumer, to prove
# REPO_ROOT resolves from cwd's git toplevel rather than BASH_SOURCE.
# That path has no manifest by construction, so --help must exit 0
# before the hard-fail check fires.
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

# PLUGIN_SRC: dev-kit plugin root. The local mirror of the GH-Actions
# `claude -p` invocation MUST pass `--plugin-dir` so the spawned claude
# process loads the slash commands (/dev-kit:review, /dev-kit:security,
# /dev-kit:maintenance). Without it the slash commands resolve to
# "Unknown command" and the gate silently defaults to Approve. The
# GH-Actions sibling `bin/ci-claude-p.sh` already does this -- see
# `bin/ci-claude-p.sh:200` for the canonical reference implementation.
#
# HARD FAIL (not warn) when the manifest is missing. A prior version
# of this check only warned and let execution continue into
# `claude -p --plugin-dir <incomplete source>`, which reproduces the
# exact #727 regression this script exists to fix: the slash commands
# resolve to "Unknown command", no `**Verdict:**` line appears, and
# the lenient-default logic (see extract_verdict below) silently maps
# the empty verdict to Approve. A fix that only works when the
# operator's plugin source happens to be intact is not a fix -- it
# narrows the trigger condition. Real consumer installs DO ship the
# plugin directory; a missing manifest here means a broken/partial
# install, and the operator needs a loud failure, not a silent
# Approve. (Local judge finding F1, PR #741.)
PLUGIN_SRC="$REPO_ROOT"
# Security finding F5 (PR #741): the raw absolute $PLUGIN_SRC leaks the
# operator's OS username (e.g. /Users/alice/...) into the dry-run argv
# log and any die() message below -- both land in
# .review-local-current/<PR>.log, an operator-scoped but on-disk file.
# Redact the $HOME prefix for every operator-facing rendering of the
# path; PLUGIN_SRC itself stays unredacted for the actual filesystem
# calls (open(), -f test) that need the real path.
PLUGIN_SRC_DISPLAY="${PLUGIN_SRC/#$HOME/\~}"

# Security finding F6 (PR #741): the two manifest-guard die() calls
# below previously used a bare `echo >&2`, bypassing the script's own
# `log` helper and carrying no timestamp -- unlike every other
# operator-facing line in this script (`log "verdicts: ..."` etc).
# manifest_guard_log emits a UTC-timestamped line through the same
# `log` helper before the eventual `die`, so a postmortem grep of
# .review-local-current/<PR>.log can correlate a guard trip against
# the surrounding gate timeline.
manifest_guard_log() {
  log "$(date -u +%Y-%m-%dT%H:%M:%SZ) manifest-guard: $*"
}

if [ ! -f "$PLUGIN_SRC/.claude-plugin/plugin.json" ]; then
  manifest_guard_log "missing manifest at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json"
  die "dev-kit plugin manifest not found at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json -- refusing to run the LLM judge against an incomplete plugin source (this would silently reproduce issue #727)"
fi
# Local judge finding A08 (security review, PR #741): the existence
# check above only proves *a* manifest is present, not that it's
# dev-kit's. A repo with a substituted `.claude-plugin/plugin.json`
# (typosquat, downgrade, attacker-authored) would pass the check above
# and get loaded into the judge subprocess via `--plugin-dir`.
# Validate the `name` field matches "dev-kit" -- cheap, deterministic,
# and closes the gap the finding describes without adding a new
# dependency (python3 is already a script-wide dependency).
#
# Security finding F3 (PR #741): the original version of this parse
# collapsed every failure mode (missing name key, JSON syntax error,
# non-UTF-8 bytes) to the same empty string, so the die() message
# below couldn't distinguish "manifest has no name field" from
# "manifest is corrupted" -- both looked like `(got "")`. The
# `OK:<name>` / `ERR:<ExceptionType>: <message>` prefix lets the bash
# side branch on parse-success vs parse-failure and surface the real
# exception type + message on failure, without a second python3 call.
PLUGIN_MANIFEST_PARSE="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        name = json.load(f).get("name", "")
    print("OK:" + str(name))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
    print("ERR:" + type(e).__name__ + ": " + str(e))
' "$PLUGIN_SRC/.claude-plugin/plugin.json")"

case "$PLUGIN_MANIFEST_PARSE" in
  OK:dev-kit)
    : # valid manifest; proceed
    ;;
  OK:*)
    manifest_guard_log "name mismatch at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json (got \"${PLUGIN_MANIFEST_PARSE#OK:}\")"
    die "dev-kit plugin manifest at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json does not declare name=\"dev-kit\" (got \"${PLUGIN_MANIFEST_PARSE#OK:}\") -- refusing to load a substituted or malformed plugin source into the judge subprocess"
    ;;
  *)
    manifest_guard_log "parse failure at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json (${PLUGIN_MANIFEST_PARSE#ERR:})"
    die "dev-kit plugin manifest at $PLUGIN_SRC_DISPLAY/.claude-plugin/plugin.json failed to parse (${PLUGIN_MANIFEST_PARSE#ERR:}) -- refusing to load a malformed plugin source into the judge subprocess"
    ;;
esac

# format_audit <verdict> [<extra_key=val> ...]
# Build the human-friendly + machine-parseable audit comment body via
# lib.maintenance_gate --format-audit. Mirrors the emitter in
# .github/workflows/review.yml:289 / maintenance.yml:209 but for the
# local mirror: synthesizes run=local-<pid>, job=review-local, and
# carries per-skill extras (review=/security=/maintenance=/provider=)
# so the operator sees the full breakdown in a single comment.
# Defined here (not next to extract_verdict) because bash does not
# hoist functions — the bump-PR skip below at line ~317 needs it.
format_audit() {
  local verdict="${1:-MISSING}"; shift || true
  local args=( --run "local-$$" --job review-local --status success
               --verdict "$verdict" --source bin_review_local )
  for kv in "$@"; do args+=( --extra "$kv" ); done
  python3 -m lib.maintenance_gate --format-audit "${args[@]}"
}

# ---------------------------------------------------------------------------
# Arg parsing.
# ---------------------------------------------------------------------------
PR_NUMBER=""
PROVIDER_FLAG=""
AUTO_APPROVE=0
TOUCH_PROBE=1
DRY_RUN=0
RUN_REVIEW=1
RUN_SECURITY=1
RUN_MAINTENANCE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --pr)               [ $# -ge 2 ] || die "--pr requires N"; PR_NUMBER="$2"; shift 2 ;;
    --provider)         [ $# -ge 2 ] || die "--provider requires name"; PROVIDER_FLAG="$2"; shift 2 ;;
    --auto-approve)     AUTO_APPROVE=1; shift ;;
    --no-touch-probe)   TOUCH_PROBE=0; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --review-only)      RUN_SECURITY=0; RUN_MAINTENANCE=0; shift ;;
    --security-only)    RUN_REVIEW=0; RUN_MAINTENANCE=0; shift ;;
    --maintenance-only) RUN_REVIEW=0; RUN_SECURITY=0; shift ;;
    --all)              RUN_REVIEW=1; RUN_SECURITY=1; RUN_MAINTENANCE=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  die "unknown flag: $1 (try --help)" ;;
  esac
done

[ -n "$PR_NUMBER" ] || die "missing --pr N"
case "$PR_NUMBER" in
  *[!0-9]*) die "--pr must be numeric: '$PR_NUMBER'" ;;
esac

# ---------------------------------------------------------------------------
# 1. Resolve provider + read API key (mirrors review.yml:99-117).
#
# Order of resolution: --provider flag > CI_REVIEW_PROVIDER env >
# .env:CI_REVIEW_PROVIDER. The flag is read FIRST so the API key is
# resolved for the provider the operator actually wants (a previous
# bug resolved the .env provider's key and then silently swapped
# providers, leaking the wrong key to the wrong endpoint).
#
# PROVIDER_EXPLICIT tracks whether the operator ACTUALLY asked for a
# specific provider (flag / process env / a real, operator-managed
# `.env`) as opposed to `lib.ci_setup.read_provider()`'s silent
# "minimax" fallback (which also matches the repo's committed
# `.env.example:CI_REVIEW_PROVIDER=minimax` template default -- that
# file exists so ci-doctor can audit a fresh clone; it is NOT operator
# intent). An interactive local session almost always already has an
# authenticated `claude` CLI (a claude.ai login or a keychain-stored
# key) -- the ANTHROPIC_BASE_URL / API_KEY / AUTH_TOKEN injection below
# exists so a GH-Actions runner (no interactive login) can authenticate.
# Only an EXPLICIT provider ask with a missing key is a real
# misconfiguration worth failing loudly on (§2 below).
# ---------------------------------------------------------------------------
PROVIDER_EXPLICIT=0
if [ -n "$PROVIDER_FLAG" ]; then
  PROVIDER="$PROVIDER_FLAG"
  PROVIDER_EXPLICIT=1
elif [ -n "${CI_REVIEW_PROVIDER:-}" ]; then
  PROVIDER="$CI_REVIEW_PROVIDER"
  PROVIDER_EXPLICIT=1
else
  PROVIDER="$(python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, 'lib')
from ci_setup import read_provider
print(read_provider(Path('${REPO_ROOT}')))
")"
  if python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, 'lib')
from ci_setup import read_env_key
v = read_env_key(Path('${REPO_ROOT}') / '.env', 'CI_REVIEW_PROVIDER')
sys.exit(0 if v else 1)
"; then
    PROVIDER_EXPLICIT=1
  fi
fi

case "$PROVIDER" in
  minimax|anthropic|deepseek) ;;
  *) die "invalid provider '$PROVIDER'; allowed: minimax, anthropic, deepseek (set via --provider or bin/set-provider.sh)" ;;
esac

# Resolve the provider's API key secret NAME by name (not by index) so a
# future reorder of lib/ci_setup.required_secrets_for_provider() cannot
# silently pick the wrong secret. The current tuple is
# (DEV_KIT_GITHUB_TOKEN, <PROVIDER>_API_KEY); we want the second one.
read_provider_api_key() {
  python3 -c "
import sys
from pathlib import Path
sys.path.insert(0, 'lib')
from ci_setup import read_env_key, required_secrets_for_provider
provider = '${PROVIDER}'
target = Path('${REPO_ROOT}')
for name in required_secrets_for_provider(provider):
    if name == 'DEV_KIT_GITHUB_TOKEN':
        continue
    v = read_env_key(target / '.env', name)
    if v:
        print(v)
        sys.exit(0)
print('')
"
}
PROVIDER_VALUE="$(read_provider_api_key)"

# Process env can override the .env lookup so a CI runner can pass the
# key via env: without writing to .env. The KEY_NAME comes from
# `provider_config` (single source of truth — same helper that
# `provider_env_for` reads from) so adding a new provider is a
# one-line edit in lib/review_local_lib.sh.
PROVIDER_CFG="$(provider_config "$PROVIDER")"
KEY_NAME="${PROVIDER_CFG%%|*}"
if [[ -n "${!KEY_NAME:-}" ]]; then
  PROVIDER_VALUE="${!KEY_NAME}"
fi

# ---------------------------------------------------------------------------
# 2. No key found: EXPLICIT provider ask -> fail loudly (real
#    misconfiguration). No signal at all -> fall back to the local
#    `claude` CLI's own default authentication and skip the provider
#    env injection entirely (§3 below leaves claude_env_args empty).
#    Uppercasing via `tr` (not the bash-4-only caret-caret parameter
#    expansion) for portability -- macOS ships bash 3.2 (GPLv2 license
#    freeze), which lacks that operator; using it here would silently
#    break this exact error path on a stock Mac.
# ---------------------------------------------------------------------------
USE_LOCAL_AUTH=0
if [ -z "$PROVIDER_VALUE" ]; then
  if [ "$PROVIDER_EXPLICIT" = "1" ]; then
    PROVIDER_UPPER="$(printf '%s' "$PROVIDER" | tr '[:lower:]' '[:upper:]')"
    die "no API key for provider '$PROVIDER' (set .env:${PROVIDER_UPPER}_API_KEY or env var)"
  fi
  log "no provider explicitly configured and no API key found; falling back to local claude CLI auth (no key/base-url injection)"
  USE_LOCAL_AUTH=1
fi

# ---------------------------------------------------------------------------
# 3. Per-provider base URL / model mapping (mirrors review.yml:120-131
#    + 175-181). Sourced from `lib/review_local_lib.sh::provider_env_for`
#    so the case-statement lives in one place (tested hermetically in
#    tests/test_review_local_lib.py::TestProviderEnvFor). The API KEY
#    is NOT exported here -- it is scoped to the single `claude -p`
#    invocation via `env KEY=... claude -p ...` so the key never enters
#    the parent shell's persistent env (any subsequent subprocess,
#    /proc/<pid>/environ reader, or core dump cannot leak it).
#
#    USE_LOCAL_AUTH=1 leaves claude_env_args EMPTY -- `env` with a zero-
#    length array simply execs `claude` with the parent's inherited
#    environment (its own pre-existing auth), matching the local-session
#    fallback decided in §2.
# ---------------------------------------------------------------------------
claude_env_args=()
if [ "$USE_LOCAL_AUTH" = "0" ]; then
  PROVIDER_ENV=()
  while IFS= read -r line; do
    PROVIDER_ENV+=("$line")
  done < <(provider_env_for "$PROVIDER")

  # Guard against an empty PROVIDER_ENV (anthropic): an empty array must
  # NOT contribute an empty token, otherwise `env '' KEY=... cmd` fails
  # because '' is not a valid VAR= assignment.
  if [ "${#PROVIDER_ENV[@]}" -gt 0 ] && [ -n "${PROVIDER_ENV[0]}" ]; then
    claude_env_args+=("${PROVIDER_ENV[@]}")
  fi
  claude_env_args+=("ANTHROPIC_API_KEY=$PROVIDER_VALUE")
  claude_env_args+=("ANTHROPIC_AUTH_TOKEN=$PROVIDER_VALUE")
fi

# ---------------------------------------------------------------------------
# 4. Resolve PR metadata + bump-PR skip (mirrors review.yml:75).
# ---------------------------------------------------------------------------
PR_JSON="$(gh pr view "$PR_NUMBER" --json number,state,title,reviewDecision,body,files \
  --jq '{number, state, title, reviewDecision, body, files: [.files[].path]}' \
  2>/dev/null)" || die "gh pr view $PR_NUMBER failed (is gh authenticated? is the PR open?)"

# One python call returns all five fields, NUL-separated, so the
# five callers below each capture exactly one field regardless of how
# many newlines it contains internally. Single python startup vs five.
#
# NUL (not newline) delimiting is required: `body` (a PR description)
# and the files-join can each legitimately span many lines -- which
# is virtually every real-world PR. A newline-counting parser that
# caps at "first 5 lines total" cannot tell "line 4 of the body" from
# "field 5, the files list" -- it silently truncates/misaligns BODY
# and FILES for any body longer than ~1 line, which downgrades a
# production-code PR's touch-probe to "docs/infra-only" and lets
# --auto-approve pass without the required L3 evidence (a
# false-positive approval; discovered live against a real PR).
read_pr_fields() {
  python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
parts = [
    str(d.get('state') or ''),
    str(d.get('title') or ''),
    str(d.get('reviewDecision') or ''),
    str(d.get('body') or ''),
    '\n'.join(d.get('files') or []),
]
sys.stdout.write('\0'.join(parts))
"
}
# NUL-delimited read (bash 3.2 -- macOS default -- supports `read -d
# ''`). `|| [ -n "$field" ]` is the standard idiom for catching the
# FINAL field, which has no trailing NUL terminator (python's
# `'\0'.join(...)` does not append one after the last element).
PR_FIELDS=()
while IFS= read -r -d '' field || [ -n "$field" ]; do
  PR_FIELDS+=("$field")
done < <(printf '%s' "$PR_JSON" | read_pr_fields)
PR_STATE="${PR_FIELDS[0]:-}"
PR_TITLE="${PR_FIELDS[1]:-}"
PR_DECISION="${PR_FIELDS[2]:-}"
PR_BODY="${PR_FIELDS[3]:-}"
PR_FILES="${PR_FIELDS[4]:-}"

if [ "$PR_STATE" != "OPEN" ]; then
  die "PR #$PR_NUMBER is $PR_STATE (must be OPEN)"
fi

# Bump-PR skip mirrors review.yml:75.
if [ "$(is_bump_pr "$PR_TITLE")" = "yes" ]; then
  log "bump-PR detected — skipping LLM judge (auto-pass per review.yml:75)"
  # Append a trailing <!-- bump-PR skip --> comment so the parseable
  # quartet stays a stable 5-tuple and operators still see the
  # auto-pass signal in the rendered table.
  REPLY_BODY="$(format_audit Approve)
<!-- bump-PR skip -->"
  if [ "$DRY_RUN" = "0" ]; then
    gh pr comment "$PR_NUMBER" --body "$REPLY_BODY" >/dev/null \
      || log "warning: gh pr comment failed (audit skipped)"
  else
    log "would post: $REPLY_BODY"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# 5. Run the configured LLM-judge skills (mirrors review.yml:120-195).
#
# Each skill's stdout is captured into a per-skill variable so the
# verdict-extraction step (5) can pipe it directly into
# `lib.maintenance_gate --extract-verdict-from-stdin` without round-
# tripping through PR comments. The agent also posts inline comments
# directly via `gh pr comment` (the workflow's
# `mcp__github_inline_comment__create_inline_comment` is unavailable
# outside the claude-code-action; the agent adapter already supports
# `gh pr comment` per skills/review/SKILL.md).
# ---------------------------------------------------------------------------
REPO_FULL="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

run_skill() {
  local skill="$1"
  local prompt="$2"
  log "running /$skill via provider=$PROVIDER (dry_run=$DRY_RUN)"
  if [ "$DRY_RUN" = "1" ]; then
    # The dry-run log MUST mirror the real argv shape (including
    # --plugin-dir) so reviewers can audit the contract from the log
    # alone -- mirrors what issue #727 regression test asserts.
    log "would run: env <$PROVIDER env+key> claude --plugin-dir \"$PLUGIN_SRC\" -p \"$prompt\""
    LAST_SKILL_STDOUT=""
    return 0
  fi
  # Capture stdout into LAST_SKILL_STDOUT AND echo to the operator's
  # terminal in real time so progress stays visible.
  #
  # `${claude_env_args[@]+"${claude_env_args[@]}"}` (not the bare
  # `"${claude_env_args[@]}"`) -- under `set -u`, expanding an EMPTY
  # array with `[@]` raises "unbound variable" on bash < 4.4 (macOS
  # ships bash 3.2, GPLv2 license freeze). The `+` alternate-value form
  # only expands the array when it has at least one element, which is
  # exactly the local-auth-fallback case (USE_LOCAL_AUTH=1 leaves
  # claude_env_args empty on purpose -- see §2/§3 above).
  local out
  out="$(env ${claude_env_args[@]+"${claude_env_args[@]}"} claude --plugin-dir "$PLUGIN_SRC" -p "$prompt" 2>&1)" \
    || die "$skill: claude -p exited non-zero (review the output above)"
  LAST_SKILL_STDOUT="$out"
  printf '%s\n' "$out"
}

if [ "$RUN_REVIEW" = "1" ]; then
  run_skill "dev-kit:review" \
    "/dev-kit:review --diff $REPO_FULL/pull/$PR_NUMBER

Render the standard two-layer output (PR summary at top, per-finding
inline comments). The summary MUST begin with a single line exactly
of the form:

  **Verdict:** Approve
  **Verdict:** Changes Requested
  **Verdict:** Blocked

Map verdict strictly to severity (do NOT inflate):
  - critical >= 1     -> **Verdict:** Blocked
  - major >= 1, critical = 0 -> **Verdict:** Changes Requested
  - no critical, no major -> **Verdict:** Approve"
  REVIEW_OUTPUT="$LAST_SKILL_STDOUT"
fi

if [ "$RUN_SECURITY" = "1" ]; then
  run_skill "dev-kit:security" \
    "/dev-kit:security --diff $REPO_FULL/pull/$PR_NUMBER

Render the security summary (per-category breakdown table + Verdict).
The summary MUST begin with a single line exactly of the form:

  **Verdict:** Approve
  **Verdict:** Changes Requested
  **Verdict:** Blocked"
  SECURITY_OUTPUT="$LAST_SKILL_STDOUT"
fi

if [ "$RUN_MAINTENANCE" = "1" ]; then
  run_skill "dev-kit:maintenance" \
    "/dev-kit:maintenance --diff $REPO_FULL/pull/$PR_NUMBER

Apply the 20-checkbox code-sanity rubric (CC-1..8, OE-1..8, VM-1..4).
The summary MUST begin with a single line exactly of the form:

  **Verdict:** Approve
  **Verdict:** Changes Requested
  **Verdict:** Blocked"
  MAINTENANCE_OUTPUT="$LAST_SKILL_STDOUT"
fi

# ---------------------------------------------------------------------------
# 6. Extract verdicts from captured stdout (mirrors review.yml:220-225).
# ---------------------------------------------------------------------------
# Reuses the same helper the workflow shells out to: extracts the LAST
# `**Verdict:** <Word>` line from the captured judge output. Per-skill
# variables mean each judge is its own bucket, not three calls into the
# same PR-comment list.
extract_verdict() {
  printf '%s' "$1" | python3 -m lib.maintenance_gate --extract-verdict-from-stdin
}

REVIEW_V=""; SECURITY_V=""; MAINTENANCE_V=""
if [ "$DRY_RUN" = "1" ]; then
  log "would extract verdicts from captured stdout"
else
  [ "$RUN_REVIEW" = "1" ]      && REVIEW_V="$(extract_verdict "${REVIEW_OUTPUT:-}")"
  [ "$RUN_SECURITY" = "1" ]    && SECURITY_V="$(extract_verdict "${SECURITY_OUTPUT:-}")"
  [ "$RUN_MAINTENANCE" = "1" ] && MAINTENANCE_V="$(extract_verdict "${MAINTENANCE_OUTPUT:-}")"
fi
log "verdicts: review='${REVIEW_V:-<missing>}' security='${SECURITY_V:-<missing>}' maintenance='${MAINTENANCE_V:-<missing>}'"

# ---------------------------------------------------------------------------
# 7. Combined verdict gate (mirrors review.yml:539-561).
# ---------------------------------------------------------------------------
# `rank()` is sourced from lib/review_local_lib.sh (unit-tested in
# tests/test_review_local_lib.py).

# Default missing verdicts to Approve + warning (mirrors review.yml:521-522).
# This is the lenient workflow policy; the stricter --auto-approve gate
# below refuses on any missing verdict rather than synthesising one.
# Default missing verdicts to Approve + warning (mirrors review.yml:521-522).
# Lenient workflow policy; the stricter --auto-approve gate below
# refuses on any missing verdict rather than synthesising one. The
# check + replacement go through `verdict_default_for` so the
# canonical contract (empty → default-to-Approve) lives in one place
# (lib/review_local_lib.sh), hermetically tested in
# tests/test_review_local_lib.py::TestVerdictDefaultFor.
for _judge in REVIEW_V SECURITY_V MAINTENANCE_V; do
  # Indirect expansion must NOT use the :- form (bash rejects it).
  # eval a temp variable, fall back to empty when unset.
  _current="$(eval "printf '%s' \"\${$_judge:-}\"")"
  if [ "$(verdict_default_for "$_current")" = "yes" ]; then
    log "warning: $(printf '%s' "$_judge" | tr '[:upper:]' '[:lower:]' | tr -d '_') verdict missing; defaulting to Approve"
    eval "$_judge='Approve'"
  fi
done

# PARSE_FAILED → hard fail (mirrors review.yml:528-536).
if [ "$REVIEW_V" = "PARSE_FAILED" ] || [ "$SECURITY_V" = "PARSE_FAILED" ] || [ "$MAINTENANCE_V" = "PARSE_FAILED" ]; then
  die "verdict parser failed: review=$REVIEW_V security=$SECURITY_V maintenance=$MAINTENANCE_V"
fi

# Worst-of wins across the enabled skills.
WORST="Approve"
V_RANK=0
for V in "$REVIEW_V" "$SECURITY_V" "$MAINTENANCE_V"; do
  R=$(rank "$V")
  if [ "$R" -gt "$V_RANK" ]; then V_RANK="$R"; WORST="$V"; fi
done
log "combined verdict: $WORST"

# ---------------------------------------------------------------------------
# 8. L3-evidence gate (mirrors review.yml:471-491).
#
# `--no-touch-probe` disables the auto-detect (file-path regex) but
# still runs the L3 regex on the PR body -- the flag is a "treat every
# PR as production-touching" toggle, NOT a "skip the gate" toggle.
# Touch-probe regex covers every directory that ships production code,
# including `bin/` and `commands/` which were missing in the previous
# version.
# ---------------------------------------------------------------------------
L3_OK=1
TOUCHES_PROD=""
if [ "$TOUCH_PROBE" = "0" ]; then
  # --no-touch-probe: every PR is treated as production-touching so the
  # L3 evidence check ALWAYS runs. The flag's documented intent is
  # "treat every PR as a production-touching PR", which means stricter
  # gating, not bypass.
  TOUCHES_PROD="forced (--no-touch-probe)"
elif [ "$TOUCH_PROBE" = "1" ]; then
  TOUCHES_PROD="$(printf '%s\n' "$PR_FILES" | grep -E '^(bin|commands|lib|tools|hooks|skills|\.githooks|\.claude|\.codex|\.github)/' || true)"
fi
if [ -n "$TOUCHES_PROD" ]; then
  if [ "$(extract_pytest_tail "$PR_BODY")" = "yes" ]; then
    log "L3 evidence: pytest tail line found in PR body"
  else
    L3_OK=0
    log "L3 evidence: pytest tail line MISSING in PR body (touches_prod=$TOUCHES_PROD)"
  fi
else
  log "L3 evidence: docs/infra-only PR; advisory only"
fi

# ---------------------------------------------------------------------------
# 9. Auto-approve (mirrors review.yml:609-618, only on the local opt-in).
#
# --auto-approve is strict: it refuses on ANY missing judge verdict
# (the lenient default-to-Approve above stays for non-auto-approve
# runs, mirroring review.yml's workflow-level contract). A gate that
# approves when its input is missing is worse than no gate.
# ---------------------------------------------------------------------------
if [ "$AUTO_APPROVE" = "1" ]; then
  # Check whether any enabled judge failed to produce a verdict.
  MISSING=""
  [ "$RUN_REVIEW" = "1" ]      && [ -z "${REVIEW_OUTPUT:-}" ]      && MISSING="${MISSING:-}review "
  [ "$RUN_SECURITY" = "1" ]    && [ -z "${SECURITY_OUTPUT:-}" ]    && MISSING="${MISSING:-}security "
  [ "$RUN_MAINTENANCE" = "1" ] && [ -z "${MAINTENANCE_OUTPUT:-}" ] && MISSING="${MISSING:-}maintenance "
  if [ -n "$MISSING" ]; then
    die "auto-approve refused: empty judge output for: $MISSING(a missing verdict must not synthesise an approval)"
  fi
  if [ "$WORST" != "Approve" ]; then
    die "auto-approve refused: combined verdict=$WORST (must be Approve)"
  fi
  if [ "$L3_OK" != "1" ]; then
    die "auto-approve refused: L3-evidence gate failed (PR body lacks pytest tail line)"
  fi
  if [ "$PR_DECISION" = "APPROVED" ]; then
    log "PR already APPROVED; skipping auto-approve (idempotent)"
  else
    if [ "$DRY_RUN" = "1" ]; then
      log "would run: gh pr review $PR_NUMBER --approve --body 'Auto-approved by bin/review-local.sh on clean combined verdict (review=$REVIEW_V security=$SECURITY_V maintenance=$MAINTENANCE_V touches_prod=$([ -n "$TOUCHES_PROD" ] && echo true || echo false) L3-passed=$L3_OK). The operator still owns the final merge step.'"
    else
      TOUCHES_PROD_FLAG=$([ -n "$TOUCHES_PROD" ] && echo true || echo false)
      gh pr review "$PR_NUMBER" --approve \
        --body "Auto-approved by bin/review-local.sh on clean combined verdict (review=$REVIEW_V security=$SECURITY_V maintenance=$MAINTENANCE_V touches_prod=$TOUCHES_PROD_FLAG L3-passed=$L3_OK). The operator still owns the final merge step." \
        || die "gh pr review --approve failed"
      log "auto-approve posted for PR #$PR_NUMBER"
    fi
  fi
else
  log "auto-approve not requested (pass --auto-approve to enable)"
fi

# ---------------------------------------------------------------------------
# 10. Audit comment (mirrors review.yml:226-227).
# ---------------------------------------------------------------------------
# format_audit() (defined near extract_verdict() above) renders both the
# parseable quartet on line 1 and the human-facing markdown table —
# including per-skill breakdown rows for review=/security=/maintenance=/
# provider=. The worst-of (WORST) verdict is the headline.
AUDIT_BODY="$(format_audit "$WORST" \
  "review=$REVIEW_V" \
  "security=$SECURITY_V" \
  "maintenance=$MAINTENANCE_V" \
  "provider=$PROVIDER")"
if [ "$DRY_RUN" = "1" ]; then
  log "would post: $AUDIT_BODY"
else
  gh pr comment "$PR_NUMBER" --body "$AUDIT_BODY" >/dev/null \
    || log "warning: gh pr comment failed (audit skipped)"
fi

# ---------------------------------------------------------------------------
# 11. Final exit (mirrors review.yml:557-561).
# ---------------------------------------------------------------------------
case "$WORST" in
  Approve) exit 0 ;;
  "Changes"*) echo "error: Changes Requested (review=$REVIEW_V security=$SECURITY_V maintenance=$MAINTENANCE_V)" >&2; exit 1 ;;
  Blocked)   echo "error: Blocked (review=$REVIEW_V security=$SECURITY_V maintenance=$MAINTENANCE_V)" >&2; exit 1 ;;
  *)         echo "error: Unparseable verdict '$WORST'" >&2; exit 1 ;;
esac
