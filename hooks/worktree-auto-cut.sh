#!/usr/bin/env bash
# worktree-auto-cut.sh — Claude UserPromptSubmit adapter for the
# request-classification contract (issue #845).
#
# This hook used to be the authoritative worktree-cut path: it
# regex-classified the raw prompt, fetched origin/main, ran
# `git worktree add` (now lib.git_worktree.cut_worktree), bootstrapped
# log-on, optionally ran Linear auto-sync, and emitted a mixed
# Claude/Codex handoff envelope. Two failure modes followed:
#
#   1. A failed cut fell back to advice; the hard worktree-guard
#      then denied the user's first Edit, so they got two disconnected
#      failures.
#   2. The same prompt was auto-cut on Claude and unclassified on
#      Codex because Codex has no UserPromptSubmit event.
#
# After issue #845 this hook is a thin Claude adapter:
#
#   prompt ──▶ lib.request_intent.classify_request
#     │
#     ├─ read_only / proposal → stay in current context (no cut)
#     ├─ implementation → check `.dev-kit/round-0/intent.md`:
#     │     ├─ missing / pending / rejected → advisory envelope
#     │     │     naming the canonical capture path
#     │     └─ accepted → cut_worktree + handoff envelope
#     └─ uncertain → next-question advisory envelope (no cut)
#
# The shared worktree guard (`worktree-guard.sh`), `git-guard.sh`,
# and `lib.git_worktree.cut_worktree` are unchanged. The classifier
# itself (`lib/request_intent.py`) is also imported by the Codex
# adapter (`lib/codex_intent_adapter.py`) so the same prompt produces
# the same classification regardless of which client surfaces it.
#
# Note: the bash-side classifier dispatch uses `python3 -` so the
# PYTHONPATH-resolved import shape matches what
# ``tests/test_worktree_auto_cut.py`` already exercises via the
# pre-refactor ``python3 -c`` shape.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Resolve the repo root + plugin lib dir. The hook may run from a
# worktree (early-exits below) or from the main checkout; both cases
# need PYTHONPATH so `from request_intent import classify_request`
# resolves regardless of cwd.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LIB_DIR="$PLUGIN_ROOT/lib"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Fail open with a stderr warning if jq is missing — this hook is
# advisory; worktree-guard.sh is the hard block.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "worktree-auto-cut.sh"
  exit 0
fi

PROMPT="$(printf '%s' "$INPUT" | jq -r '.prompt // ""' 2>/dev/null)"
[ -z "$PROMPT" ] && exit 0

# Prefer cwd from the hook payload; fall back to PWD.
HOOK_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || exit 0
fi

# Discriminator: already populated by the preamble. Only fire in
# the main checkout. Worktree sessions already follow the rule;
# outside-git is out of scope.
case "$WORKTREE_DETECT" in
  worktree|outside|"") exit 0 ;;
  main) ;;
  *) exit 0 ;;
esac

# Run the shared classifier. The classifier is a pure function:
# it returns JSON with the structured Classification record. The
# hook reads mode + worktree_required + slug_hint + next_action
# and routes from there. The script body is fed to `python3 -` via
# a quoted heredoc so branch names containing shell metacharacters
# never reach the embedded Python.
#
# Belt-and-suspenders: ensure ``LIB_DIR`` is on ``sys.path`` even if
# the calling shell stripped it on exec (rare but observed on some
# sandbox invocations).
read -r -d '' CLASSIFY_SCRIPT <<'PYEOF' || true
import json
import os
import sys

lib_dir = os.environ.get("LIB_DIR") or ""
if lib_dir and lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from request_intent import classify_request  # noqa: E402

client = os.environ.get("CLIENT", "claude-code")
prompt = os.environ.get("PROMPT", "")
cls = classify_request(prompt, client=client)
print(json.dumps(cls.to_dict(), sort_keys=True))
PYEOF

CLASSIFICATION="$(PROMPT="$PROMPT" CLIENT="claude-code" LIB_DIR="$LIB_DIR" \
  python3 - "$LIB_DIR" <<<"$CLASSIFY_SCRIPT" 2>/dev/null)" || exit 0

if [ -z "$CLASSIFICATION" ]; then
  # Classifier failed (e.g. module not importable). Stay silent —
  # the worktree-guard will fire on the user's first Edit and the
  # user can recover via the documented manual-cut path.
  exit 0
fi

# Read the four fields the rest of this script routes on. Anything
# more exotic (prompt_hash, reason_codes) lands in the additionalContext
# envelope so downstream clients can audit.
MODE="$(printf '%s' "$CLASSIFICATION" | jq -r '.mode // "uncertain"')"
WORKTREE_REQ="$(printf '%s' "$CLASSIFICATION" | jq -r '.worktree_required // false')"
NEXT_ACTION="$(printf '%s' "$CLASSIFICATION" | jq -r '.next_action // "stay_in_current_context"')"
SLUG_HINT="$(printf '%s' "$CLASSIFICATION" | jq -r '.slug_hint // ""')"
REQ_ID="$(printf '%s' "$CLASSIFICATION" | jq -r '.request_id // ""')"

# Emit the classifier audit context on every call. The shape is
# the same ``additionalContext`` envelope Claude consumes for
# downstream handoff; the metrics emitter inside the Python
# helper additionally records ``client_parity_observed`` to the
# .dev-kit/cache/intent-metrics.jsonl sink.
audit_context() {
  local body="$1"
  local ctx
  ctx="request classified
  request_id:    $REQ_ID
  mode:          $MODE
  next_action:   $NEXT_ACTION
  worktree:      $WORKTREE_REQ
  $body"
  jq -nc --arg ctx "$ctx" \
    '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'
}

# Mode → action table per the issue brief.
case "$MODE" in
  read_only|proposal)
    # Per the brief: "stay in current context, no worktree cut."
    # Stay silent — no envelope, no advice. The metrics emission
    # already happened inside the classifier; an empty stdout is
    # the cleanest "stay in current context" the harness consumes.
    exit 0
    ;;
  uncertain)
    # Never cut. Surface a next-question fallback that names the
    # canonical capture path so the user can confirm intent before
    # a worktree is created.
    audit_context "uncertain classification (confidence < threshold) — clarifying question required
  no worktree was cut. To proceed, write a one-paragraph description of the request, then run:
    python3 -c \"from lib.request_intent import classify_request; print(classify_request('<rewritten prompt>', client='claude-code'))\"
  When mode becomes 'implementation', capture intent at .dev-kit/round-0/intent.md and re-submit."
    exit 0
    ;;
  implementation)
    # Implementation requested. Gate the cut on an accepted intent
    # record at .dev-kit/round-0/intent.md. The brief is explicit:
    # "The cut only proceeds after mode: accepted."
    :  # fall through to the intent-gate below
    ;;
  *)
    # Unknown mode from the classifier. Fail closed — never cut.
    audit_context "unknown classifier mode ($MODE) — no worktree cut
    inspect the classifier output and re-submit"
    exit 0
    ;;
esac

# Resolve the intent record path. The brief says
# ".dev-kit/round-0/intent.md" inside an orch worktree, but the
# hook may fire from the main checkout. We resolve against the
# repo root so the path stays stable across cwd.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
INTENT_PATH="$REPO_ROOT/.dev-kit/round-0/intent.md"

# Read the intent record (if any). The classifier output already
# confirmed mode=implementation; the gate below checks
# decision_mode=accepted before invoking cut_worktree.
INTENT_JSON="$(PROMPT="" CLIENT="" LIB_DIR="$LIB_DIR" \
  python3 - "$INTENT_PATH" "$LIB_DIR" <<'PYEOF' 2>/dev/null
import json
import os
import sys

path = sys.argv[1]
lib_dir = sys.argv[2] if len(sys.argv) > 2 else ""
if lib_dir and lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from request_intent import load_intent, validate_intent  # noqa: E402

intent_path = path
loaded = load_intent(__import__("pathlib").Path(intent_path))
ok, reason = validate_intent(loaded)
print(json.dumps({
    "exists": loaded is not None,
    "decision_mode": getattr(loaded, "decision_mode", None),
    "is_cut_eligible": bool(loaded and loaded.is_cut_eligible()),
    "validate_ok": ok,
    "validate_reason": reason,
    "goal": getattr(loaded, "goal", "") if loaded else "",
    "request_id": getattr(loaded, "request_id", "") if loaded else "",
}, sort_keys=True))
PYEOF
)" || INTENT_JSON='{"exists":false,"is_cut_eligible":false,"validate_ok":false,"validate_reason":"missing_intent"}'

EXISTS="$(printf '%s' "$INTENT_JSON" | jq -r '.exists // false')"
DECISION_MODE="$(printf '%s' "$INTENT_JSON" | jq -r '.decision_mode // ""')"
IS_CUT_ELIGIBLE="$(printf '%s' "$INTENT_JSON" | jq -r '.is_cut_eligible // false')"
VALIDATE_OK="$(printf '%s' "$INTENT_JSON" | jq -r '.validate_ok // false')"
VALIDATE_REASON="$(printf '%s' "$INTENT_JSON" | jq -r '.validate_reason // ""')"

if [ "$EXISTS" != "true" ]; then
  audit_context "implementation requested but no intent record at $INTENT_PATH
  write a proto-spec with goal + acceptance_criteria, then set:
    decision_mode: accepted
  The canonical helper for capture is the lib/request_intent.render_intent function. After capture, re-submit this prompt."
  exit 0
fi

if [ "$VALIDATE_OK" != "true" ]; then
  audit_context "implementation requested but intent at $INTENT_PATH failed validation ($VALIDATE_REASON)
  fix the intent record (request_id must start with req-, client must be claude-code or codex, decision_mode must be one of accepted/rejected/pending, goal must be non-empty), then re-submit."
  exit 0
fi

if [ "$IS_CUT_ELIGIBLE" != "true" ]; then
  audit_context "implementation requested but intent at $INTENT_PATH has decision_mode='$DECISION_MODE' (not 'accepted')
  to proceed: flip decision_mode to 'accepted' (after the originator review), then re-submit."
  exit 0
fi

# Precondition 1: main is clean (ignoring untracked files —
# the proto-spec at .dev-kit/round-0/intent.md is untracked until
# the orchestrator commits it, and we don't want that to block
# the cut).
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  audit_context "main checkout is dirty; stash or commit its changes before cutting a clean worktree"
  exit 0
fi

# Derive the branch slug. Prefer the classifier's hint; fall back to
# a generic fix/-slug of the request id (already deterministically
# derived from the prompt).
if [ -n "$SLUG_HINT" ]; then
  SLUG="$SLUG_HINT"
else
  SLUG="fix/task-$REQ_ID"
fi

# Validate the slug matches the project's branch-naming regex; if
# not, fall back to a date-based slug.
if ! printf '%s' "$SLUG" | grep -qE '^(fix|feat|refactor|docs|test|chore|perf|hotfix)/[a-z0-9-]{2,40}$'; then
  SLUG="fix/task-$(date +%s | tail -c 7)"
fi

# Reject forbidden slugs (per .claude/rules/git-workflow.md).
case "$SLUG" in
  */wip|*/tmp|*/foo|*/bar|*/asdf|*/test|*/scratch|*/untitled) SLUG="fix/task-$(date +%s | tail -c 7)" ;;
esac

# Append a numeric suffix when the branch already exists so a
# re-run after a prior failed cut does not collide.
BRANCH="$SLUG"
n=1
while git show-ref --verify --quiet "refs/heads/${BRANCH}" 2>/dev/null; do
  BRANCH="${SLUG}-${n}"
  n=$((n + 1))
done
DIRNAME="${BRANCH#*/}"  # strip type prefix for the worktree dir name

# Resolve which main ref to branch from. Prefer origin/main (just-
# fetched); fall back to local main (no-remote case, e.g. tests).
MAIN_REF=""
if git remote get-url origin >/dev/null 2>&1; then
  if git fetch origin main >/dev/null 2>&1; then
    MAIN_REF="origin/main"
  else
    audit_context "fetching origin/main failed; the worktree must start from the latest remote main"
    exit 0
  fi
fi
if [ -z "$MAIN_REF" ] && git rev-parse --verify main >/dev/null 2>&1; then
  MAIN_REF="main"
fi
[ -n "$MAIN_REF" ] || {
  audit_context "no main branch is available as a worktree base"
  exit 0
}

WT_PARENT="$REPO_ROOT/.worktrees"
mkdir -p "$WT_PARENT"
WT_PATH="$WT_PARENT/$DIRNAME"

# Precondition: worktree doesn't already exist on disk.
if [ -d "$WT_PATH" ]; then
  audit_context "the target worktree path already exists: $WT_PATH"
  exit 0
fi

# Auto-cut. Routed through the canonical ``lib.git_worktree.cut_worktree``
# helper (issue #322) so the safe-mode contract (fail closed when branch
# or dir exists; preserve pre-existing branches on failure) matches
# ``lib/execute.py`` and ``lib/acp_dispatch.py``. A future contract
# change must therefore touch one place, not three. The hook-level
# timeout (120s in hooks.json) bounds the whole operation;
# ``cut_worktree`` itself is normally <2s.
read -r -d '' CUT_SCRIPT <<'PYEOF' || true
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
branch = sys.argv[2]
wt_path = Path(sys.argv[3])
base = sys.argv[4]
lib_dir = sys.argv[5] if len(sys.argv) > 5 else ""
if lib_dir and lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from git_worktree import cut_worktree  # noqa: E402

try:
    cut_worktree(
        repo_root=repo_root,
        branch=branch,
        worktree_path=wt_path,
        base=base,
    )
except Exception as exc:
    print(f"cut_worktree failed: {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF

if ! python3 - "$REPO_ROOT" "$BRANCH" "$WT_PATH" "$MAIN_REF" "$LIB_DIR" <<<"$CUT_SCRIPT" 2>/dev/null; then
  # Per the brief: failed cut leaves no "ready" envelope — the
  # fallback names the canonical retry command and exits without
  # handoff.
  audit_context "worktree cut failed for branch $BRANCH
  no worktree was created. Retry with:
    python3 -c \"from lib.git_worktree import cut_worktree; from pathlib import Path; cut_worktree(repo_root=Path('$REPO_ROOT'), branch='$BRANCH', worktree_path=Path('$WT_PATH'), base='$MAIN_REF')\"
  Codex: spawn a subagent with cwd=$REPO_ROOT after the worktree exists."
  exit 0
fi

# Bootstrap: run /dev-kit:log setup + /dev-kit:log on inside the new
# worktree so the delegated subagent's work is captured. Falls
# through silently if either script is missing.
LOG_SETUP="$PLUGIN_ROOT/skills/log/scripts/log-setup.sh"
LOG_ON="$PLUGIN_ROOT/skills/log/scripts/log-on.sh"
if [ -f "$LOG_SETUP" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_SETUP" >/dev/null 2>&1) || true
fi
if [ -f "$LOG_ON" ]; then
  (cd "$WT_PATH" && TARGET_DIR="$WT_PATH" bash "$LOG_ON" >/dev/null 2>&1) || true
fi

# Linear bootstrap: trigger one auto-sync round in the new worktree.
# The owner gate inside `auto_sync` bails silently for non-owners.
if [ -f "$WT_PATH/tools/linear_sync.py" ]; then
  for py in python3 python py; do
    if command -v "$py" >/dev/null 2>&1; then
      (cd "$WT_PATH" && "$py" "$WT_PATH/tools/linear_sync.py" auto-sync) || true
      break
    fi
  done
fi

# Build additionalContext — the harness consumes this as the
# client-specific handoff envelope. Per the brief, the envelope is
# emitted only after the cut succeeded and an accepted intent was
# loaded.
CTX="worktree cut ready
  branch:        $BRANCH
  path:          $WT_PATH
  request_id:    $REQ_ID
  mode:          $MODE
  intent:        $INTENT_PATH
  Claude Code next: open a new session in $WT_PATH
  Codex next: spawn a subagent with cwd=$WT_PATH and branch=$BRANCH
  fallback: if any of the above fails, re-run the canonical helper:
            python3 -c \"from lib.git_worktree import cut_worktree; cut_worktree(repo_root=Path('$REPO_ROOT'), branch='$BRANCH', worktree_path=Path('$WT_PATH'), base='$MAIN_REF')\""
jq -nc --arg ctx "$CTX" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'
exit 0