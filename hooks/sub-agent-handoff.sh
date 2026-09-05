#!/usr/bin/env bash

# Mode gate: short-circuit unless DEV_KIT_MODE matches
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# sub-agent-handoff.sh — PostToolUse Agent hook. SHO-154.
#
# Advisory check that the sub-agent response shape can support the
# standard handoff template:
#
#   1. STATUS marker     — `**Status**:` line + ✅/⚠️/❌ (or
#                          success/partial/failed)
#   2. EVIDENCE block    — at least one quoted `exit N` reference
#                          (canonical: `cmd -> result (exit N)`;
#                          accepted: any `(exit N)`, `exit N` in
#                          prose, `Ran 'cmd': result` form)
#   3. NEXT-ACTION line  — `### Next action` heading, a list-item
#                          prefix, OR a final imperative sentence
#                          (KO + EN forms).
#
# If any of those three are absent, the hook emits an advisory to
# stderr listing which pieces the orchestrator should add before
# relaying the result to the user. Always exit 0 (advisory). Per
# #539 ("Linear failures are non-blocking for implicit workflow
# calls"), payload parse errors are also non-blocking so unrelated
# sessions cannot hit a soft-bricked hook.
#
# Fail-CLOSED only when jq OR python3 is missing (exit 2 with a
# plain stderr ERROR) — without these, the payload cannot be
# parsed/scanned and the rule silently lapses. NOTE: PostToolUse
# cannot actually block (the tool has already executed by the time
# we run), so the exit code is a *signal* to the harness and the
# stderr line is what surfaces to the user. We deliberately do NOT
# emit a `permissionDecision: "deny"` JSON envelope here — that
# field is decorative in PostToolUse and misleads readers about
# the actual contract. PreToolUse hooks in this repo use the
# `permissionDecision` envelope; PostToolUse hooks (slop-detector,
# secret-scan) emit plain stderr text. We follow the latter pattern.
#
# Per-worktree opt-out: write `<repo>/.dev-kit/.sub-agent-handoff-disabled`.
# The hook prints a one-shot notice and exits 0; structurally the
# hook pretends the response is complete so no advisory surfaces.

set -uo pipefail

INPUT="$(cat)"

# ── opt-out (per-worktree) ──────────────────────────────────────────────────
# Look for .dev-kit/.sub-agent-handoff-disabled either at the
# canonical repo CWD (Claude's session cwd when the hook fires) or
# at the worktree rooted at the current directory. The hook is
# read-only — does not create the file.
OPT_OUT=""
probe_path() {
  local p="$1"
  [ -n "$p" ] || return 1
  [ -f "$p/.dev-kit/.sub-agent-handoff-disabled" ] && OPT_OUT="$p/.dev-kit/.sub-agent-handoff-disabled" && return 0
  return 1
}
probe_path "${CLAUDE_PROJECT_DIR:-}" || probe_path "${PWD}" || true
if [ -n "$OPT_OUT" ]; then
  echo "[sub-agent-handoff] disabled via $OPT_OUT" >&2
  exit 0
fi

# ── jq presence (fail-closed) ──────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
  echo "[sub-agent-handoff] ERROR: jq is required but not installed. Install jq (apt/brew/apk) — without it, this hook cannot parse the PostToolUse payload and the handoff contract silently lapses." >&2
  exit 2
fi

# ── python3 presence (fail-closed) ─────────────────────────────────────────
# The inner scan runs as a Python script (tolerates dict+list payload
# shapes, single regex scan). Without python3, the scan silently
# no-ops via `|| true`, defeating the contract — same shape as the
# jq-missing gap. Loop through common binary names parallel to
# linear-autosync.sh:52-58.
PYTHON=""
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1; then
    PYTHON="$(command -v "$py")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "[sub-agent-handoff] ERROR: python3 is required but not installed. The hook scans the payload in Python (to handle dict/list shapes); without it the handoff contract silently lapses. Install python3 (apt/brew/apk)." >&2
  exit 2
fi

# ── payload validity (non-blocking warn on parse error per #539) ───────────
# If the payload is not valid JSON we log a one-line warn to stderr
# and exit 0 — silently lapping the check here would re-enable the
# bypass for malformed payloads and a single bad frame would
# soft-brick unrelated sessions (parallel: linear-autosync.sh).
if [ -n "$INPUT" ] && ! printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1; then
  echo "[sub-agent-handoff] warn: stdin payload is not valid JSON; skipping (non-blocking parse error)" >&2
  exit 0
fi

# ── tool name filter (matcher already enforces this; body belt-and-suspenders) ──
TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || true)"
if [ "$TOOL_NAME" != "Agent" ]; then
  exit 0
fi

# ── payload extraction + scan (single Python call) ─────────────────────────
# Python is used to (a) tolerate string-vs-object tool_response and
# (b) scan for handoff pieces in one process. Bypasses jq's
# type-discrimination limits and avoids re-forking.
#
# We dump the script to a tempfile and exec it directly so the
# `>&2` on the python invocation is unambiguous (heredoc + `>&2`
# interactions in `python3 - ... >&2 <<PY` race the file-descriptor
# setup and silently swallow print output).
SCRIPT_TMP="$(mktemp -t sub-agent-handoff.XXXXXX)"
trap 'rm -f "$SCRIPT_TMP"' EXIT
cat > "$SCRIPT_TMP" <<'PY'
import json
import os
import re
import sys

raw = os.environ.get("INPUT", "")
try:
    payload = json.loads(raw) if raw.strip() else {}
except (json.JSONDecodeError, ValueError):
    print("[sub-agent-handoff] warn: stdin payload is not valid JSON; skipping (non-blocking)")
    sys.exit(0)

tool_response = payload.get("tool_response", "")
text = ""

if isinstance(tool_response, str):
    text = tool_response
elif isinstance(tool_response, dict):
    content = tool_response.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", None):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts)
    elif isinstance(content, str):
        text = content
elif tool_response is None:
    text = ""

text = text or ""
if not text.strip():
    # Empty/probe payloads produce no advisory; the hook is silent.
    sys.exit(0)

# --- STATUS detection ---
status_present = bool(
    re.search(r"\*\*(?:Status|status)\*\*\s*[:：]?\s*(?:✅|⚠️|❌|success|partial|failed|Success|Partial|Failed)", text)
    or re.search(r"(?m)^Status\s*[:：]\s*(?:✅|⚠️|❌|success|partial|failed|Success|Partial|Failed)", text)
)


def _final_imperative(text: str) -> bool:
    """Heuristic: the last non-empty line reads as an imperative.

    Trigger when the last line ends with sentence-final punctuation
    AND the line reads as a directive. Covers:

    - EN imperatives starting with a known-imperative verb
      ("Open the PR…", "Run the suite…", "Ship it.")
    - KO imperatives containing a common verb form ("…해주세요.",
      "…기다려주세요.", "…진행하세요.") — we do not require the
      verb to be at the start because polite KO forms often lead
      with adverbs or context ("마지막으로 PR을 열고 … 기다려주세요.")
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].strip()
    if not last:
        return False
    # Sentence-final punctuation (en + ko)
    if not re.search(r"[.!?。!?]$", last):
        return False
    # Imperative must start with a verb; reject question-form
    # sentences (they end in ? but are requests, not directives).
    if last.endswith("?"):
        return False
    # Reject very short lines (likely a heading or trailing label,
    # not an imperative).
    if len(last.split()) < 2:
        return False
    # EN: starts with a verb in the imperative mood.
    en_openers = (
        "open", "run", "fix", "merge", "wait", "check", "verify",
        "create", "add", "remove", "delete", "update", "push", "ship",
        "build", "deploy", "rebase", "review", "rerun",
        "switch", "bump", "tag", "land", "send", "file", "close",
        "monitor", "iterate",
    )
    first = last.split()[0].lower().rstrip(".,!?")
    if first in en_openers:
        return True
    # KO: any common imperative/polite-imperative signal anywhere in
    # the last line. "주세요" is the canonical polite-imperative
    # suffix (verb + 주세요 = "please do X"). We also check for
    # formal-imperative forms (세요/십시오) and a short list of
    # common KO verbs.
    if "주세요" in last or "해주" in last or "기다리" in last:
        return True
    if re.search(r"(세요|십시오)[.!?]?$", last):
        return True
    ko_verbs = (
        "열어", "실행", "확인", "진행", "기다려", "오픈", "생성",
        "추가", "삭제", "업데이트", "푸시", "머지", "리뷰",
    )
    if any(v in last for v in ko_verbs):
        return True
    return False


# --- EVIDENCE detection ---
# Canonical: quoted `<cmd> -> <result> (exit N)` (or `→`).
# Accepted loose forms (prose):
#   - any `(exit N)` substring (parenthesized, what we test for)
#   - any `exit N` substring in prose (a paragraph that mentions
#     "exit 0" or "exit 1" is a strong evidence signal)
#   - `Ran 'cmd': result` form (Claude's narrative style)
#   - common shell-tool invocation prefix on its own line
# We deliberately accept loose forms because the orchestrator's
# relay is what quotes the exit code back to the user; missing the
# advisory on a prose-shaped evidence line defeats the hook's
# primary purpose (finding SHO-154 / /dev-kit:review finding 3).
evidence_present = bool(
    re.search(r"`[^`]+`\s*(?:->|→)\s*`[^`]+`\s*\(exit\s*-?\d+\)", text)
    or re.search(r"\(exit\s*-?\d+\)", text)
    or re.search(r"\bexit\s*-?\d+\b", text)
    or re.search(r"(?im)^\s*Ran\s+['`\"][^'`\"]+['`\"]\s*[:：]", text)
    or re.search(
        r"(?im)^\s*(?:pytest|bash|python3|python|npm|make|cargo|go|git|gh|ruff|node|ruby|swift)\s+\S",
        text,
    )
)

# --- NEXT-ACTION detection ---
# `### Next action` heading, list-item prefix, or final-imperative
# sentence (KO + EN). The loose final-imperative check covers
# responses that close with "Open the PR and wait for CI." or
# "마지막으로 PR을 열고 CI 결과를 기다려주세요." without an explicit
# `Next action` label (finding SHO-154 / /dev-kit:review finding 4).
next_action_present = bool(
    re.search(r"(?im)^#{2,4}\s*next\s+action\b", text)
    or re.search(
        r"(?m)^(?:[-*]\s+)?(?:Next action|Next steps?|후속 작업|다음 작업)\s*[:：]?\s*\S",
        text,
    )
    or _final_imperative(text)
)

missing = []
if not status_present:    missing.append("STATUS")
if not evidence_present:  missing.append("EVIDENCE")
if not next_action_present: missing.append("NEXT-ACTION")

if not missing:
    print("[sub-agent-handoff] STATUS OK -- handoff template pieces all present.")
    sys.exit(0)

print(
    "[sub-agent-handoff] advisory: agent response is missing "
    + ", ".join(m for m in missing)
    + " piece(s) of the standard handoff template "
    + "(feedback-subagent-handoff-template). The orchestrator's "
    + "next relay cannot quote exit codes / status markers / next "
    + "action without these. To suppress this advisory, write "
    + "`<repo>/.dev-kit/.sub-agent-handoff-disabled` or add the "
    + "missing piece to the agent's response."
)
# Emit one line per missing piece so downstream greps / test
# assertions can match `missing STATUS`, `missing EVIDENCE`,
# `missing NEXT-ACTION` independently.
for m in missing:
    print(f"[sub-agent-handoff] missing {m} piece")
PY
INPUT="$INPUT" "$PYTHON" "$SCRIPT_TMP" >&2 || true
exit 0
