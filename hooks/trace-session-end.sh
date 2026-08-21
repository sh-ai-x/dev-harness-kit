#!/usr/bin/env bash
# trace-session-end.sh — emit step.completed for the session-scoped
# subject so the harness-effectiveness reducer's event_coverage metric
# has a clean lifecycle pair. Issue #702.
#
# Fires on SessionEnd (Claude Code) and Stop (Codex). Best-effort: any
# failure (missing jq, missing session_id, missing .dev-kit/trace dir)
# is suppressed with `|| true` so this hook never gates session end.
# When Stop / SessionEnd does not fire (SIGKILL, OOM, ExitWorktree),
# the matching step.started is left orphaned; the
# subject_observability submetric surfaces the orphan via its finding
# string (no heartbeat needed).

# Source the shared preamble (set -uo pipefail, INPUT=$(cat), jq warn).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Warn (not fail) if jq is missing — fail-open contract.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "trace-session-end.sh"
  exit 0
fi

# Read the session_id from the stdin payload. Fall back gracefully
# if the runtime does not provide it (Codex may use a different
# field name).
SESSION_ID=$(printf '%s' "${INPUT:-}" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || true)
[ -z "$SESSION_ID" ] && exit 0

# Resolve the worktree root. HOOK_CWD is set by hook-preamble.sh from
# the payload's .cwd field; fall back to $PWD for non-Claude runtimes.
EFFECTIVE_CWD="${HOOK_CWD:-$PWD}"
[ -z "$EFFECTIVE_CWD" ] && exit 0

# Emit the matching step.completed. subject_id is identical to the
# step.started subject so the reducer's set-intersection finds the pair.
python3 -m lib.trace_log append-event \
  --root "$EFFECTIVE_CWD" --type step.completed \
  --run-id "session:${SESSION_ID}" --workflow-id "session-lifecycle" \
  --stage session --subject-id "session:${SESSION_ID}" \
  --outcome completed --source "hook:trace-session-end" \
  --evidence-json "$(jq -nc --arg sid "$SESSION_ID" '{session_id:$sid, hook_event:"SessionEnd"}')" \
  >/dev/null 2>&1 || true

exit 0
