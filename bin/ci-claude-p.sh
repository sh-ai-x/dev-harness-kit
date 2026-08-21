#!/usr/bin/env bash
# bin/ci-claude-p.sh — Dispatched-run workaround for the claude-code-action
# `workflow_dispatch` no-op bug.
#
# Background:
#   The fork-pr-review maintainer-approval gate dispatches review.yml +
#   maintenance.yml via `workflow_dispatch`. Inside those workflows, the
#   `anthropics/claude-code-action@v1` step (pinned at
#   558b1d6cab4085c7753fe402c10bef0fbb92ac7a) treats `workflow_dispatch` as
#   "agent mode": it writes only `claude-prompt.txt`, NOT
#   `claude-user-request.txt`. The Claude Code SDK treats a missing
#   user-request file as a plain string prompt
#   (base-action/src/run-claude-sdk.ts:30-37), so the slash command
#   `/dev-kit:review --diff <PR>` is NOT parsed -- Claude receives it as
#   literal text. Combined with the `isEntityContext()` gate that disables
#   the `mcp__github_inline_comment__create_inline_comment` MCP server for
#   `workflow_dispatch` (src/mcp/install-mcp-server.ts:134), dispatched runs
#   exit silently with `num_turns: 0, duration_ms: 21, is_error: false` --
#   the run is GREEN but no AI review comments are posted, and the audit
#   log records `verdict=MISSING` (observed against PRs #682 and #687 in
#   August 2026).
#
#   Same-repo `pull_request` runs work fine because tag mode writes both
#   prompt files AND initializes the MCP servers.
#
# Workaround:
#   For `workflow_dispatch` events only, skip the broken claude-code-action
#   step and call Claude Code directly via `claude -p <prompt>`. The
#   downstream skill (review / security / maintenance) reads its own PR
#   metadata via `gh pr view` + `gh pr diff`, so the prompt just needs to
#   name the slash command and the diff URL. The verdict-extraction step
#   in the workflow already reads `**Verdict:**` lines from PR comments,
#   so the assistant's output is what the gate consumes -- nothing more.
#
#   This script is intentionally the ONLY point where the `claude -p`
#   invocation shape lives; the workflow steps just call
#   `bin/ci-claude-p.sh <skill> <pr_number>`. Three judges
#   (review / security / maintenance) x three providers
#   (minimax / anthropic / deepseek) = nine call sites that all share
#   this single helper.
#
# Upstream issues (referenced in the workflow comments):
#   - anthropics/claude-code-action#635  (workflow_dispatch silent no-op)
#   - anthropics/claude-code-action#1644 (slash command not parsed)
#
# Usage:
#   bin/ci-claude-p.sh <skill> <pr_number>
#
# Args:
#   skill       One of: review | security | maintenance.
#   pr_number   Numeric PR id.
#
# Required env (set by the calling workflow step):
#   ANTHROPIC_BASE_URL     Model provider base URL.
#   ANTHROPIC_MODEL        Model id (e.g. MiniMax-M3[1m]).
#   ANTHROPIC_API_KEY      Provider API key (or ANTHROPIC_AUTH_TOKEN).
#   GITHUB_REPOSITORY      owner/repo slug.
#   GH_TOKEN               PAT for `gh pr comment`, `gh pr diff`, etc.
set -euo pipefail

# -----------------------------------------------------------------------------
# Arg parsing.
# -----------------------------------------------------------------------------
if [ $# -ne 2 ]; then
  echo "::error::usage: $0 <skill> <pr_number>  (skill: review|security|maintenance)" >&2
  exit 1
fi

SKILL="$1"
PR_NUMBER="$2"

case "$SKILL" in
  review|security|maintenance) ;;
  *) echo "::error::unknown skill '$SKILL' (expected review|security|maintenance)" >&2; exit 1 ;;
esac

case "$PR_NUMBER" in
  ''|*[!0-9]*) echo "::error::pr_number must be numeric, got '$PR_NUMBER'" >&2; exit 1 ;;
esac

# -----------------------------------------------------------------------------
# Required env.
# -----------------------------------------------------------------------------
: "${ANTHROPIC_BASE_URL:?ANTHROPIC_BASE_URL must be set by the workflow (provider base URL)}"
: "${ANTHROPIC_MODEL:?ANTHROPIC_MODEL must be set by the workflow (model id for the active provider)}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set (workflow always sets it)}"
: "${GH_TOKEN:?GH_TOKEN must be set so the assistant can call gh pr comment}"

# ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN -- accept either name (claude
# code reads API_KEY by default, ANTHROPIC_AUTH_TOKEN works for newer
# providers). Error out if neither is set.
if [ -z "${ANTHROPIC_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-}}" ]; then
  echo "::error::either ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be set" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Install Claude Code CLI if missing. The action normally installs it
# (and adds it to PATH) on pull_request runs; for workflow_dispatch runs we
# skip the action entirely, so install ourselves. Idempotent: if claude
# is already on PATH or at $HOME/.local/bin/claude, no-op.
# -----------------------------------------------------------------------------
CLAUDE_BIN="$HOME/.local/bin/claude"
if ! command -v claude >/dev/null 2>&1 && [ ! -x "$CLAUDE_BIN" ]; then
  echo "::notice::claude CLI not on PATH; installing to $CLAUDE_BIN"
  if ! curl -fsSL https://claude.ai/install.sh | bash; then
    echo "::error::claude CLI install failed (curl https://claude.ai/install.sh)" >&2
    exit 1
  fi
fi

# Prepend the install location so the just-installed binary wins over any
# stale PATH entry.
export PATH="$HOME/.local/bin:$PATH"

if ! command -v claude >/dev/null 2>&1; then
  echo "::error::claude CLI not found on PATH after install ($PATH)" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Install the dev-kit plugin into the freshly-installed Claude Code's
# plugin directory. The action normally installs the plugin via the
# `claude-code-action` step; we're bypassing the action so we have to
# do it ourselves. Without this, Claude Code prints "Unknown command:
# /dev-kit:<skill>" and exits with num_turns=0.
#
# The plugin source is $GITHUB_WORKSPACE (the workflow's checkout of
# the PR head). The plugin must contain a `.claude-plugin/plugin.json`
# manifest. We symlink to ~/.claude/plugins/marketplaces/dev-kit/ so the
# standard Claude Code marketplace plugin loader picks it up.
# -----------------------------------------------------------------------------
PLUGIN_SRC="${GITHUB_WORKSPACE:-}"
if [ -z "$PLUGIN_SRC" ] || [ ! -f "$PLUGIN_SRC/.claude-plugin/plugin.json" ]; then
  echo "::error::GITHUB_WORKSPACE not set or missing .claude-plugin/plugin.json ($PLUGIN_SRC)" >&2
  exit 1
fi

mkdir -p "$HOME/.claude/plugins/marketplaces"
# -f (force) replaces any stale symlink; -n prevents creating a nested
# link if the target already exists as a directory.
ln -sfn "$PLUGIN_SRC" "$HOME/.claude/plugins/marketplaces/dev-kit"
# Verify the symlink resolves to a valid plugin (manifest present).
if [ ! -f "$HOME/.claude/plugins/marketplaces/dev-kit/.claude-plugin/plugin.json" ]; then
  echo "::error::dev-kit plugin symlinked but .claude-plugin/plugin.json missing" >&2
  exit 1
fi
# Note: we deliberately do NOT verify skills/$SKILL/ exists. Some
# workflow prompts (e.g. maintenance) reference /dev-kit:<skill>
# slash commands that historically don't have a corresponding
# skills/<skill>/ directory — the rubric instructions in the prompt
# body are sufficient for the judge to complete the task even when
# the slash command itself doesn't resolve. The upstream claude-code-action
# silently tolerates this same case; we mirror that behavior here.

# -----------------------------------------------------------------------------
# Build the prompt. The slash command syntax is required because the
# downstream skill (/dev-kit:review / /dev-kit:security / /dev-kit:maintenance)
# is the actual reviewer; we just feed it the PR diff URL. The verdict
# format requirement is what the workflow's verdict-extraction step
# expects (it scans for `**Verdict:**` on the first line of any claude
# PR comment).
# -----------------------------------------------------------------------------
PROMPT="/dev-kit:${SKILL} --diff ${GITHUB_REPOSITORY}/pull/${PR_NUMBER}

The first line of your final PR comment MUST be exactly one of:

  **Verdict:** Approve
  **Verdict:** Changes Requested
  **Verdict:** Blocked

Post the summary as a PR comment via \`gh pr comment\`. Use
\`gh pr diff\` to read the PR diff. Apply the skill's rubric strictly
and do NOT inflate the verdict."

# -----------------------------------------------------------------------------
# Invoke claude -p. --permission-mode bypassPermissions lets the assistant
# run gh pr comment / Read / Grep / Glob without per-call approval prompts
# (workflow runs are non-interactive). --allowedTools whitelists ONLY the
# gh + read tools we want; in particular we exclude
# `mcp__github_inline_comment__create_inline_comment` because that MCP
# server is itself broken on workflow_dispatch (issue #635).
# -----------------------------------------------------------------------------
exec claude \
  --plugin-dir "$PLUGIN_SRC" \
  --model "$ANTHROPIC_MODEL" \
  --permission-mode bypassPermissions \
  --allowedTools "Bash(gh pr comment:*),Bash(gh pr diff:*),Bash(gh pr view:*),Read,Grep,Glob" \
  -p "$PROMPT"
