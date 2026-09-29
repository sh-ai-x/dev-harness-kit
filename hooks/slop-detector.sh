#!/usr/bin/env bash
# slop-detector.sh — PostToolUse hook. v3 (LLM judge).
#
# Replaces the v2 regex tier ladder (phrases.md / structures.md) with
# a single LLM-as-judge call via lib/llm_judge.py:call_judge(dim="slop").
# Per-dim prompt lives at eval/prompts/judge-slop.md; the judge returns
# a 0-10 `slop_score` (higher = cleaner) that we map to severity.
#
# Default advisory (exit 0). Opt-in strict via SLOP_STRICT=1 (exit 2 on HIGH).
#
# Severity (slop_score, higher = cleaner):
#   ≥8   OK     (silent)
#   ≥5   LOW    (advisory to stderr)
#   ≥2   MEDIUM (advisory to stderr)
#   <2   HIGH   (advisory to stderr; SLOP_STRICT=1 -> exit 2)
#
# Failure modes — exit 0 silently (advisory default):
#   - api_key missing            -> skip (no false-positive on dev box)
#   - judge call timeout/error   -> skip
#   - non-Latin CJK content      -> skip (judge prompt is English; not worth a translated rubric)
#
# Lockfile/minified path skip is a cheap signal that doesn't need the LLM.

set -eo pipefail
# harness-mode opt-out (workflow-fast-mode-lean): /dev-kit:harness-mode fast|custom
# can turn this gate off for the current session. Checked before the LLM machinery
# so the opt-out costs one Python process, not a wasted parse.
if command -v python3 >/dev/null 2>&1; then
  gate=$(cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null && python3 -m lib.harness_mode_state get slop_detector 2>/dev/null || echo on)
  if [ "$gate" = "off" ]; then
    echo "slop-detector: opted out via harness-mode" >&2
    exit 0
  fi
fi
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq slop-detector
read_stdin_json slop-detector
[ -z "$INPUT_JSON" ] && exit 0
hook_stage_active slop-detector || exit 0

FILE=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.file_path // ""')
extract_content
[ -z "$CONTENT" ] && exit 0

# Cheap pre-filter — lockfiles and minified assets are pure noise.
case "$FILE" in
  *.lock|*.min.js|*.min.css|*-lock.json|pnpm-lock.yaml|package-lock.json|yarn.lock) exit 0;;
esac

SLOP_QUIET="${SLOP_QUIET:-0}"
SLOP_STRICT="${SLOP_STRICT:-0}"

# Resolve project root for `.env` lookup by llm_judge.load_config.
# Prefer CLAUDE_PROJECT_DIR (set by Claude Code), fall back to git
# toplevel, then cwd. Mirrors how lib/push_intent_judge.py resolves
# --project-root.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$PROJECT_ROOT" ] && command -v git >/dev/null 2>&1; then
  PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
PROJECT_ROOT="${PROJECT_ROOT:-$PWD}"

# Ponytail: skip non-Latin CJK content. The judge prompt is English;
# asking the model to score Hangul/Japanese prose with English rubrics
# produces unreliable scores and burns tokens for noise. A future dim
# could carry a translated rubric — for v3 we just skip.
if printf '%s' "$CONTENT" | LC_ALL=C grep -q '[^[:print:][:space:]]'; then
  exit 0
fi

# Trim pathological inputs to ~8 KB so the judge call stays bounded.
# Score quality beyond 8 KB degrades faster than the marginal signal
# improves; pushing larger bodies through the model just buys cost.
if [ "${#CONTENT}" -gt 8192 ]; then
  CONTENT="${CONTENT:0:8192}"
fi

# Cap the call so a slow judge never blocks the PostToolUse gate.
# Default 10s — long enough for a small file on a quiet network, short
# enough that the operator notices if the API is dead.
SLOP_TIMEOUT="${SLOP_TIMEOUT:-10}"

# Single Python invocation does: load .env, call judge, map score to
# severity, print advisory. No intermediate shell pipes. JSON-only
# stdout -> the hook reads the JSON, prints the advisory, and exits
# with the right code. Inline Python keeps the bash hook to one
# process and one heredoc — no temp files, no jq round-trips.
#
# SLOP_FIXTURE (test seam): when set, the inline Python reads the JSON
# file at $SLOP_FIXTURE instead of calling the real judge. The file
# schema matches the judge's score output: {"slop_score": <0-10>,
# "reason": "<=100 chars>"}. Tests use this to hermetically exercise
# HIGH/MEDIUM/LOW/OK verdicts without touching the network or the .env.
PAYLOAD_JSON=$(CONTENT="$CONTENT" SLOP_TIMEOUT="$SLOP_TIMEOUT" PROJECT_ROOT="$PROJECT_ROOT" SLOP_FIXTURE="${SLOP_FIXTURE:-}" \
  python3 - "$PROJECT_ROOT" "$CONTENT" <<'PY'
import os, sys, json, traceback

try:
    from lib import llm_judge
except ImportError:
    import llm_judge  # type: ignore

project_root = sys.argv[1]
content = sys.argv[2]
timeout = int(os.environ.get("SLOP_TIMEOUT", "10"))
fixture_path = os.environ.get("SLOP_FIXTURE") or ""

if fixture_path:
    # Test seam — load canned verdict, skip the LLM call.
    with open(fixture_path, encoding="utf-8") as f:
        canned = json.load(f)
    score = float(canned.get("slop_score", 10.0))
    reason = str(canned.get("reason", ""))
else:
    cfg = llm_judge.load_config(__import__("pathlib").Path(project_root))
    if not cfg.get("api_key"):
        sys.exit(0)
    prompt = llm_judge.format_prompt(
        __import__("pathlib").Path(project_root),
        "judge-slop.md",
        {"CONTENT": content},
    )
    if not prompt:
        sys.exit(0)
    try:
        result = llm_judge.call_judge(
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            prompt=prompt,
            axes=llm_judge.DIM_AXES["slop"],
            dim="slop",
            base_url=cfg.get("base_url", "https://api.minimax.io/anthropic"),
            timeout=timeout,
            max_tokens=192,
        )
    except Exception as exc:
        sys.stderr.write(f"[slop-detector] judge call failed: {exc}\n")
        sys.exit(0)

    scores = result.get("scores") or {}
    try:
        score = float(scores.get("slop_score", 10.0))
    except (TypeError, ValueError):
        score = 10.0
    raw = (result.get("raw") or "").strip()
    try:
        import re as _re
        m = _re.search(r'"reason"\s*:\s*"([^"]+)"', raw)
        reason = m.group(1).strip() if m else ""
    except Exception:
        reason = ""

if score >= 8.0:
    severity = "OK"
elif score >= 5.0:
    severity = "LOW"
elif score >= 2.0:
    severity = "MEDIUM"
else:
    severity = "HIGH"

out = {"severity": severity, "score": score, "reason": reason}
sys.stdout.write(json.dumps(out))
sys.stdout.write("\n")
PY
) || exit 0

# Empty stdout = judge silently skipped. JSON parse failure = same.
[ -z "$PAYLOAD_JSON" ] && exit 0
SEVERITY=$(printf '%s' "$PAYLOAD_JSON" | jq -r '.severity // "OK"')
SCORE=$(printf '%s' "$PAYLOAD_JSON" | jq -r '.score // 10')
REASON=$(printf '%s' "$PAYLOAD_JSON" | jq -r '.reason // ""')

[ "$SEVERITY" = "OK" ] && exit 0
[ "$SLOP_QUIET" = "1" ] && exit 0

echo "[slop-detector] ${SEVERITY} — ${FILE} (score=${SCORE})" >&2
if [ -n "$REASON" ]; then
  echo "[slop-detector]   ${REASON}" >&2
fi
echo "[slop-detector] AI slop detected. If intentional, ignore; otherwise revise the edit." >&2

if [ "$SEVERITY" = "HIGH" ] && [ "$SLOP_STRICT" = "1" ]; then
  exit 2
fi

exit 0
