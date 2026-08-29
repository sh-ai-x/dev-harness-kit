#!/usr/bin/env bash
# scripts/audit_hook_stdout.sh
#
# Hook stdout determinism check (G4 in
# docs/proposals/cache-hit-rate/structural-fix.yaml §Validation gates).
#
# Invokes each `hooks/*.sh` twice on identical input and asserts the
# stdout bytes match. Volatile content in hook stdout (timestamps, PIDs,
# ephemeral ids) invalidates the prompt cache on every Claude Code turn
# — see `rules/session-hygiene.md` Iron Law 3 (volatile content stays
# in the prompt tail, never the prefix).
#
# Exit codes:
#   0  — every hook's stdout is byte-identical between two invocations
#   1  — one or more hooks emitted non-deterministic stdout
#   2  — usage error
#
# Stdlib only (bash + sha256sum + diff). No third-party deps.
#
# Usage:
#   bash scripts/audit_hook_stdout.sh           # audit all hooks/*.sh
#   bash scripts/audit_hook_stdout.sh --json    # emit JSON report

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOOKS_DIR="${REPO_ROOT}/hooks"

if [[ ! -d "${HOOKS_DIR}" ]]; then
    echo "error: ${HOOKS_DIR} not found" >&2
    exit 2
fi

emit_json=false
if [[ "${1:-}" == "--json" ]]; then
    emit_json=true
fi

bad_hooks=()
checked=0

# Stream every .sh in hooks/; capture stdout bytes for two sequential
# invocations on identical stdin, then sha256sum-compare. We feed
# /dev/null because every hook under hooks/*.sh is a PreToolUse /
# PostToolUse / SessionStart / UserPromptSubmit handler that reads
# JSON from stdin — but their *stdout* (the part Claude Code sees
# as prompt-prefix injection) should still be deterministic when
# the input is identical.
while IFS= read -r -d '' hook; do
    # /dev/null guarantees we send the same input both times. We don't
    # try to synthesize valid hook payloads — a hook that *requires*
    # a payload and errors out is still informative (its error message
    # on the same payload should be byte-identical). The 5s per-call
    # timeout guards against a hook that blocks on stdin (none should,
    # but defensive). On macOS ``gtimeout`` is preferred when present;
    # fall back to a portable ``( sleep 5 ; kill ... ) &`` wrapper.
    if command -v gtimeout >/dev/null 2>&1; then
        a="$(gtimeout 5 bash "${hook}" </dev/null 2>/dev/null | sha256sum | awk '{print $1}')"
        b="$(gtimeout 5 bash "${hook}" </dev/null 2>/dev/null | sha256sum | awk '{print $1}')"
    else
        # Portable fallback. The hook path is passed via argv
        # (``bash -c '... _ "$hook"``) so a filename containing a `"` or
        # `;` cannot break out of the wrapper; SIGKILL on the 5s
        # deadline matches ``gtimeout`` and handles hooks that catch
        # SIGTERM.
        a="$(bash -c 'bash "$1" </dev/null 2>/dev/null & sleep 5; kill -9 $! 2>/dev/null; wait $! 2>/dev/null' _ "${hook}" | sha256sum | awk '{print $1}')"
        b="$(bash -c 'bash "$1" </dev/null 2>/dev/null & sleep 5; kill -9 $! 2>/dev/null; wait $! 2>/dev/null' _ "${hook}" | sha256sum | awk '{print $1}')"
    fi
    checked=$((checked + 1))
    if [[ "${a}" != "${b}" ]]; then
        bad_hooks+=("${hook}")
    fi
done < <(find "${HOOKS_DIR}" -maxdepth 1 -name '*.sh' -print0 | sort -z)

if [[ "${emit_json}" == true ]]; then
    bad_json="$(printf '"%s",' "${bad_hooks[@]:-}" | sed 's/,$//')"
    if [[ -z "${bad_json}" ]]; then
        bad_json=""
    fi
    printf '{"checked":%d,"bad":[%s]}\n' "${checked}" "${bad_json}"
    # Mirror the non-JSON exit contract: 1 when any hook is bad, 0 when clean.
    if [[ ${#bad_hooks[@]} -gt 0 ]]; then
        exit 1
    fi
    exit 0
else
    if [[ ${#bad_hooks[@]} -eq 0 ]]; then
        echo "[ok] ${checked} hook(s) byte-deterministic between two invocations"
        exit 0
    fi
    echo "[bad] ${#bad_hooks[@]}/${checked} hook(s) emitted non-deterministic stdout:"
    for h in "${bad_hooks[@]}"; do
        echo "  - ${h}"
    done
    exit 1
fi
