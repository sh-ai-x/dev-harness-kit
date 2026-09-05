#!/usr/bin/env bash
# bash-guard.sh — PreToolUse hook for Bash. Blocks destructive commands.
#
# Two tiers, because "destructive" is not one category:
#
#   CATASTROPHIC — unrecoverable, no legitimate agent use case.
#     Always hard-denies. Ignores DEV_KIT_STRICT *and* the stage gate.
#     `rm -rf /` is never correct in any stage of any workflow, so
#     gating it behind either switch is indefensible.
#
#   RECOVERABLE — destructive but sometimes legitimate (git reset --hard
#     on your own branch, docker system prune on a dev box). Advisory by
#     default; hard-denies under DEV_KIT_STRICT=1. Stage-gated, so it
#     only speaks up in the stages where the matrix enables bash-guard.
#
# Why the tier split (this file's history): a single flat list gated on
# DEV_KIT_STRICT meant the default install printed a warning for `rm -rf /`
# and then EXECUTED IT. Worse, bash-guard is stage-gated off everywhere
# except `build` (lib/active_hooks_codec.py), so even DEV_KIT_STRICT=1 left
# `rm -rf /` unguarded in the other six stages. The catastrophic tier is
# checked before the stage gate for exactly that reason.
#
# Root-subpath + `--` separator coverage (B1/B2, review on commit d3476e9):
# the original bare-root pattern (`rm -rf /` followed by space-or-end) let
# `rm -rf /etc`, `rm -rf /usr/local/bin`, and `rm -rf -- /` through
# unguarded — each at least as destructive as the bare-root case the tier
# exists to catch. Fixed by widening the pattern to system-directory
# subpaths (any depth) plus the canonical `--` end-of-flags form; /home
# and /root only deny on an exact top-level hit so routine absolute-path
# cleanup under a user's own directory still works.

set -eo pipefail
source "${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}/hooks/lib/mode-resolve.sh"
dev_kit_mode_require full
# Use %/* parameter expansion (POSIX, no external `dirname` required) so
# the source line still works when PATH is broken (jq-less test envs
# strip dirname along with jq — see TestBashGuardRefactor.fails_closed).
# shellcheck source=lib/payload-parse.sh
source "${BASH_SOURCE[0]%/*}/lib/payload-parse.sh"
source "${BASH_SOURCE[0]%/*}/lib/stage-gate.sh"
require_jq bash-guard
read_stdin_json bash-guard
[ -z "$INPUT_JSON" ] && exit 0
CMD=$(printf '%s' "$INPUT_JSON" | jq -r '.tool_input.command // ""')
[ -z "$CMD" ] && exit 0

# ---- Tier 1: catastrophic. Unconditional deny, pre-stage-gate. ----
# Filesystem/root destruction, credential-file clobber, remote code
# execution, publish-to-the-world. None have a legitimate in-session use;
# all are unrecoverable or externally visible.
CATASTROPHIC_PATTERNS=(
  # Bare root, with or without the `--` end-of-flags separator agents
  # reflexively add before an absolute path (`rm -rf -- /`).
  "rm -rf (-- )?/([[:space:]]|$)"
  # Root subpaths under system directories, at any depth — not just an
  # exact `/etc` hit. `rm -rf /usr/local/bin` is as catastrophic as
  # `rm -rf /usr`; the boundary after the dir name is space, `/`
  # (continuing into a subpath), or end-of-string so `/etcetera` does
  # not false-positive on `/etc`.
  "rm -rf (-- )?/(etc|var|usr|opt|srv|boot|bin|sbin|lib|lib64)([[:space:]/]|$)"
  # /home and /root: only the exact top-level target denies (wiping ALL
  # users' data), not every subpath — `rm -rf /home/user/build-cache` is
  # a normal cleanup op an agent should be able to run.
  "rm -rf (-- )?/(home|root)([[:space:]]|$)"
  "rm -rf --no-preserve-root"
  "rm -rf ~"
  "rm -rf [\"']?\\\$HOME[\"']?"
  "chown -R /"
  "chmod -R 777 /"
  ">>?[[:space:]]*/etc/(passwd|shadow)"
  "mkfs\\."
  "dd[[:space:]].*of=/dev/(sd|nvme|disk|hd)"
  "curl.*[|][[:space:]]*(ba)?sh([[:space:]]|$)"
  "wget.*[|][[:space:]]*(ba)?sh([[:space:]]|$)"
  "npm publish"
  "terraform destroy.*-auto-approve"
  "kubectl delete (namespace|ns)[[:space:]]"
  "aws s3 (rm|rb) .*--recursive"
  # Self-protection: an agent disabling its own guard is the single
  # highest-value block this hook makes. Never advisory.
  "DEV_KIT_HOOK_OFF=.bash-guard"
)

for pattern in "${CATASTROPHIC_PATTERNS[@]}"; do
  if echo "$CMD" | grep -qE "$pattern"; then
    deny "BASH GUARD (catastrophic)" \
      "pattern '$pattern' is denied unconditionally — not overridable by DEV_KIT_STRICT or the stage matrix. Command: ${CMD:0:80}"
  fi
done

# ---- Tier 2: recoverable. Stage-gated, advisory unless strict. ----
hook_stage_active bash-guard || exit 0

RECOVERABLE_PATTERNS=(
  "git push --force([[:space:]]|$)"
  "git push .*--force.* main"
  "git push -f .* main"
  "git reset --hard"
  "git clean -f"
  "git branch -D (main|master)"
  "DROP TABLE"
  "DROP DATABASE"
  "TRUNCATE TABLE"
  "chmod 777"
  "docker system prune"
  "docker volume rm"
  "eval \$"
  "find .* -delete"
  "pkill -9"
  "terraform destroy"
)

for pattern in "${RECOVERABLE_PATTERNS[@]}"; do
  if echo "$CMD" | grep -qE "$pattern"; then
    if [ "${DEV_KIT_STRICT:-0}" = "1" ]; then
      deny "BASH GUARD (strict)" "pattern '$pattern' blocked."
    fi
    echo "[bash-guard] Pattern '$pattern' in command: ${CMD:0:60}... (advisory). strict mode required to block." >&2
    exit 0
  fi
done
exit 0
