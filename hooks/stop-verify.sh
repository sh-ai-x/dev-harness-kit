#!/usr/bin/env bash
# stop-verify.sh — Stop event hook. Runs AC checks before letting session end.
# Default fail-open for malformed hook input; completion checklist failures block.

set -eo pipefail
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full,lite
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
INPUT=$(cat)
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null)
[ -z "$LAST_MSG" ] && exit 0
hook_stage_active stop-verify || exit 0

# Detect completion-claim patterns (KO + EN)
CLAIM_RE='(완료|통과|작동|fixed|done|passes|should work|should be working|it works)'
EVIDENCE_RE='(exit code|passed [0-9]+|failed [0-9]+|tests:|Traceback|AssertionError|OK \(|FAIL|test_)'

if echo "$LAST_MSG" | grep -qE "$CLAIM_RE"; then
  pre_completion_checklist_active || exit 0

  ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  CHECK_RESULT=$(cd "$ROOT" && PYTHONPATH="$ROOT" python3 -c '
import json
import subprocess
from pathlib import Path
from lib.pre_completion_checklist import check, read_user_request

root = Path.cwd()
diff = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True, check=False).stdout
files = subprocess.run(["git", "diff", "HEAD~1", "--name-only"], capture_output=True, text=True, check=False).stdout.splitlines()
result = check(read_user_request(root), diff, files)
print(json.dumps({"passed": result.passed, "failed_items": result.failed_items, "blocking": result.blocking}))
' 2>/dev/null || true)

  if [ -n "$CHECK_RESULT" ] && echo "$CHECK_RESULT" | jq -e '.blocking == true' >/dev/null 2>&1; then
    echo "[stop-verify] Pre-completion checklist blocked this completion claim." >&2
    echo "$CHECK_RESULT" | jq -r '.failed_items[] | "[stop-verify] Missing evidence: \(.)"' >&2
    echo "[stop-verify] Address the blocking checklist items before declaring done." >&2
    exit 1
  fi

  if ! echo "$LAST_MSG" | grep -qE "$EVIDENCE_RE"; then
    echo "[stop-verify] You claimed completion but cited no exit code / test count / build output." >&2
    echo "[stop-verify] Run the verify command and quote the output. (Iron Law #3)" >&2
  fi
fi
exit 0
