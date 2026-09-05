#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/lib/mode-resolve.sh"
dev_kit_mode_require full,lite
# session-start-check.sh — SessionStart hook.
#
# Gentle reminder layer for the "every task = new worktree" rule.
#
# Fires once at session start. If the session cwd is the MAIN repo
# checkout (not a worktree), emit an additionalContext reminder so
# Claude remembers the rule from the very first turn. Claude can then
# either nudge the user to cut a worktree, or — if the session is
# legitimately a read-only investigation in the main checkout — proceed
# carefully knowing that worktree-guard.sh will block any Edit/Write.
#
# This hook never blocks. The hard block is worktree-guard.sh.
#
# Discriminator: --git-dir == --git-common-dir ⇒ main checkout.
#
# Fails open (with stderr warning) when `jq` is missing — the rule is
# advisory in this hook. worktree-guard.sh is the hard-block layer.

# Source the shared preamble (set -uo pipefail, INPUT=$(cat),
# worktree_detect, jq-missing warning).
# shellcheck source=lib/hook-preamble.sh
source "${BASH_SOURCE[0]%/*}/lib/hook-preamble.sh"

# Warn (not fail) if jq is missing. The preamble already ran
# worktree_detect, which leaves $WORKTREE_DETECT="" when jq is absent;
# the case statement below treats "" as silent / no-op.
if ! command -v jq >/dev/null 2>&1; then
  worktree_detect_jq_missing_warn "session-start-check.sh"
  exit 0
fi

# extract_hook_cwd — read HOOK_CWD from stdin payload and cd into it.
# Falls back to current $PWD if the payload cwd is missing or not a directory.
HOOK_CWD="$(printf '%s' "${INPUT:-$(cat 2>/dev/null)}" | jq -r '.cwd // ""' 2>/dev/null)"
if [ -n "$HOOK_CWD" ] && [ -d "$HOOK_CWD" ]; then
  cd "$HOOK_CWD" || true
fi

# Re-derive an effective cwd after the cd: an empty HOOK_CWD above
# would otherwise leave us with the bare "/.dev-kit/logs" path below
# and `mkdir -p` would target the filesystem root in containers /
# CI images running as root. Issue #676 review finding (CC-4).
EFFECTIVE_CWD="${HOOK_CWD:-$PWD}"

# Regenerate .dev-kit/.active-hooks.json (MUST-13 SSOT) so any
# downstream check that consumes the matrix sees a fresh snapshot.
# This MUST run before any check that depends on the matrix. Cheap
# (Python over hooks/hooks.json, ~50ms) and idempotent. Best-effort:
# if python3 or hooks/hooks.json is missing, skip silently — this hook
# never blocks. Regen failures are routed to .dev-kit/logs/ (the dev-kit
# error sink, alongside hand-off/) so the next SessionStart or
# /dev-kit:hook-doctor has something to read instead of a silent
# /dev/null swallowing.
if command -v python3 >/dev/null 2>&1; then
  DEV_KIT_LOGS="${EFFECTIVE_CWD}/.dev-kit/logs"
  if [ ! -d "$DEV_KIT_LOGS" ] && command -v mkdir >/dev/null 2>&1; then
    mkdir -p "$DEV_KIT_LOGS" 2>/dev/null || DEV_KIT_LOGS=""
  fi
  if [ -d "$DEV_KIT_LOGS" ]; then
    python3 "${BASH_SOURCE[0]%/*}/../tools/regenerate_active_hooks.py" --root "$EFFECTIVE_CWD" --quiet 2>>"$DEV_KIT_LOGS/session-start-check.log" || true
  else
    python3 "${BASH_SOURCE[0]%/*}/../tools/regenerate_active_hooks.py" --root "$EFFECTIVE_CWD" --quiet 2>/dev/null || true
  fi
fi

# Discriminator: already populated by the preamble.
# Issue #702: emit a session-scoped step.started so the harness-effectiveness
# reducer's event_coverage denominator is non-zero in every worktree.
# Best-effort: never blocks session start. Mirrors the append-event
# pattern at hooks/lib/payload-parse.sh:108.
SESSION_ID=$(printf '%s' "${INPUT:-}" | jq -r '.session_id // .sessionId // empty' 2>/dev/null || true)
if [ -n "$SESSION_ID" ] && [ -n "$EFFECTIVE_CWD" ]; then
  python3 -m lib.trace_log append-event \
    --root "$EFFECTIVE_CWD" --type step.started \
    --run-id "session:${SESSION_ID}" --workflow-id "session-lifecycle" \
    --stage session --subject-id "session:${SESSION_ID}" \
    --outcome started --source "hook:trace-session-start" \
    --evidence-json "$(jq -nc --arg sid "$SESSION_ID" '{session_id:$sid, hook_event:"SessionStart"}')" \
    >/dev/null 2>&1 || true
fi

case "$WORKTREE_DETECT" in
  worktree|outside|"") exit 0 ;;
  main) ;;
  *) exit 0 ;;
esac

# In main checkout → emit nudge.
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo detached)"
NUDGE="GIT-WORKFLOW REMINDER (rules/git-workflow.md): this session started in the main repo checkout (branch='$BRANCH'). For any new implementation task, the rule requires a new worktree + client handoff + new branch. The hard edit-block is hooks/worktree-guard.sh (PreToolUse). If the user is just investigating or asking questions, proceed; before any Edit/Write, cut a worktree with: git fetch origin main && git worktree add -b <type>/<slug> .worktrees/<slug> origin/main. Claude Code then opens a new session in that path; Codex spawns/hand-offs a subagent with that path as its working directory."

jq -nc --arg ctx "$NUDGE" --arg ev "SessionStart" \
  '{hookSpecificOutput:{hookEventName:$ev,additionalContext:$ctx}}'
exit 0
