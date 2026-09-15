#!/usr/bin/env bash
# pr-create-route.sh — PreToolUse:Bash hook.
#
# Fires on every `gh pr create` invocation. Classifies the local repo
# + HEAD branch via `lib/actor_classifier` and:
#
#   1. ALWAYS writes the result to `.dev-kit/.pr-route.json` so any
#      follow-up composite action can dedupe the four duplicated
#      `if:` predicates in review.yml / maintenance.yml /
#      fork-pr-review.yml (deferred; see plan §6).
#   2. Prints a one-line route summary to stderr so the operator
#      sees which CI gate will judge the PR.
#   3. Optionally `ask`s the operator before the PR is opened — only
#      when `.dev-kit/guard-mode.session.json:fork_pr_confirm == "on"`
#      (default off; mirrors the `push_confirm` opt-in pattern).
#
# Non-blocking: every step is wrapped in `|| true` so a missing
# classifier, a read-only filesystem, a missing jq, or any other
# degradation path exits 0 — the worst case is "the route summary
# was not printed", and the PR is still opened. The server-side
# review.yml / maintenance.yml `if:` predicate + author_association
# check is the authoritative gate; this hook is the client-side
# breadcrumb + UX nudge.
#
# Per feedback-tmux-long-running-safety.md:
#   - Idempotent across re-invocation within a session (same HEAD →
#     same breadcrumb).
#   - No global state outside project tree (.dev-kit/.pr-route.json).
#   - No daemonized processes, no listeners, no SIGTERM concerns.
#   - 120-second timeout in hooks.json.

set -uo pipefail

HOOK_PREFIX="PR-CREATE-ROUTE"

# Fail open if jq / python3 / git are missing — non-blocking hook.
# The deny helpers below never fire because we never call them.
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh" 2>/dev/null || true
require_jq "$HOOK_PREFIX" || { echo "[$HOOK_PREFIX] jq missing — fail-open (route skipped)" >&2; exit 0; }

read_stdin_json "$HOOK_PREFIX"
[ -z "${INPUT_JSON:-}" ] && exit 0

CMD=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""' 2>/dev/null || true)
[ -z "$CMD" ] && exit 0

# Match `gh pr create` only. NOT `gh pr create` aliases (`pr open` is
# aliased to `pr create` in some gh versions — match the exact
# subcommand so we don't false-positive on `gh pr create-review`.
if ! printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+create([;&|[:space:]]|$)'; then
    exit 0
fi

# python3 must exist for the classifier to run. Without it, fail open.
command -v python3 >/dev/null 2>&1 || { echo "[$HOOK_PREFIX] python3 missing — fail-open (route skipped)" >&2; exit 0; }

# Resolve the project root. Prefer DEV_KIT_TDD_ROOT (the convention
# the tdd-guard / tdd-scope-judge hooks already established) and
# fall back to `git rev-parse --show-toplevel`.
ROOT="${DEV_KIT_TDD_ROOT:-$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null || pwd)}"
HEAD=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# Resolve the directory that owns `lib/actor_classifier.py` so the
# subprocess can `import lib`. In a Claude Code session the hook's
# cwd IS the project root, but Codex and tmux-detached sessions may
# run with a different cwd; this fallback is robust either way.
LIB_PARENT=""
if [ -d "$ROOT/lib" ]; then
    LIB_PARENT="$ROOT"
elif [ -d "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/lib" ]; then
    LIB_PARENT="${CLAUDE_PLUGIN_ROOT}"
fi
if [ -n "$LIB_PARENT" ]; then
    export PYTHONPATH="$LIB_PARENT${PYTHONPATH:+:$PYTHONPATH}"
fi

# Pull fork_pr_confirm from guard-mode.session.json (default "off").
# Mirror the destructive-confirm.sh pattern: missing/corrupt file →
# "off" so a fresh checkout never auto-asks.
CONFIRM="off"
if [ -f "$ROOT/.dev-kit/guard-mode.session.json" ]; then
    CONFIRM=$(jq -r '.fork_pr_confirm // "off"' "$ROOT/.dev-kit/guard-mode.session.json" 2>/dev/null || echo "off")
fi
[ "$CONFIRM" = "on" ] || CONFIRM="off"

# Run the classifier. --json so we can parse; --write-breadcrumb so
# the CI follow-up composite action can consume it. Both steps are
# best-effort; a non-zero exit silently swallows (|| true) per the
# non-blocking contract.
RESULT=$(python3 -m lib.actor_classifier \
    --root "$ROOT" \
    --head-branch "$HEAD" \
    --write-breadcrumb \
    --json 2>/dev/null || true)

if [ -z "$RESULT" ]; then
    # Classifier failed (missing module, malformed repo, etc.). Fail
    # open — the server-side review.yml `if:` is still authoritative.
    echo "[$HOOK_PREFIX] classifier unavailable — fail-open (route skipped)" >&2
    exit 0
fi

GATE=$(printf '%s' "$RESULT" | jq -r '.recommended_gate // "unknown"' 2>/dev/null || echo "unknown")
TYPE=$(printf '%s' "$RESULT" | jq -r '.actor_type // "unknown"' 2>/dev/null || echo "unknown")
REASON=$(printf '%s' "$RESULT" | jq -r '.reason // ""' 2>/dev/null || echo "")

# Default silent route — print to stderr only.
if [ "$CONFIRM" = "off" ]; then
    echo "[$HOOK_PREFIX] $TYPE → $GATE  (set fork_pr_confirm=on in guard-mode for ask mode)" >&2
    exit 0
fi

# Opt-in ask mode. Surface the classification to the operator so a
# misclassification can be caught BEFORE the PR is opened. The PR
# still goes through — the human just gets one extra prompt.
ask "$HOOK_PREFIX" "$TYPE detected — PR will route via $GATE. $REASON"
