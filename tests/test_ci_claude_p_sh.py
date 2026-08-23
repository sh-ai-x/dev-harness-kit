#!/usr/bin/env python3
"""test_ci_claude_p_sh.py — shell-level tests for bin/ci-claude-p.sh.

The script is the dispatched-run workaround for
`anthropics/claude-code-action@v1`'s workflow_dispatch silent no-op
(issues #635 + #1644). For `workflow_dispatch` events, the
claude-code-action step writes only `claude-prompt.txt` (not the
`claude-user-request.txt` the SDK needs to parse a slash command), so
Claude receives the slash command as literal text and exits with
`num_turns: 0, duration_ms: 21, is_error: false` -- a GREEN run that
posts no AI review comments and is logged as `verdict=MISSING`.

The workaround: skip the claude-code-action step on dispatch and call
`claude -p "<prompt>"` directly. This script is the single point where
that invocation shape lives (9 call sites = 3 providers x 3 judges,
each currently duplicating ~30 lines of claude-code-action yaml).

These tests are hermetic subprocess tests — they invoke the script
with a stub `claude` on PATH (or, for env-validation tests, with
deliberately-missing env vars). End-to-end smoke tests against a real
provider run manually.

Pin tests:
  - File exists and is executable (the workflow calls it directly).
  - bash -n syntax check (catches parse errors before CI).
  - The script does NOT reference claude-code-action in its invocation
    code (it's the workaround for that exact tool).
  - The script does NOT include the broken
    `mcp__github_inline_comment__create_inline_comment` MCP server in
    its --allowedTools.
  - Argument validation: < 2 args, unknown skill, non-numeric pr.
  - Required env vars: missing ANTHROPIC_MODEL / GITHUB_REPOSITORY /
    GH_TOKEN exits non-zero with the var name in stderr.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "bin" / "ci-claude-p.sh"


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the script with the given args + env, return CompletedProcess.

    `env` REPLACES the parent's environment (subprocess.run semantics),
    so callers MUST pass a complete env. Tests that want to drop a
    key (e.g. "missing ANTHROPIC_MODEL") must remove it from the
    returned dict before passing in.
    """
    if env is None:
        env = {}
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _minimal_env() -> dict:
    """Return the COMPLETE env the script needs.

    Returns a fresh dict so callers can .pop() to test missing-key
    branches. The dict intentionally omits any keys the script
    doesn't read (so `subprocess.run(env=...)` doesn't inherit the
    parent's full env).

    PATH: derived from the parent's PATH but with all dirs that
    contain a real `claude` binary stripped out — so the script's
    `command -v claude` lookup fails cleanly (the install branch is
    exercised but the curl will fail in CI; the env-validation tests
    must NOT reach that branch).
    """
    parent_path = os.environ.get("PATH", "/usr/bin:/bin")
    safe_dirs = []
    for d in parent_path.split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, "claude")
        if os.path.exists(candidate):
            continue  # skip — this dir has a real claude
        safe_dirs.append(d)
    return {
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "ANTHROPIC_MODEL": "MiniMax-M3[1m]",
        "ANTHROPIC_API_KEY": "test-key",
        "GITHUB_REPOSITORY": "owner/repo",
        "GH_TOKEN": "x",
        "GITHUB_WORKSPACE": "/tmp/fake-workspace",
        "PATH": os.pathsep.join(safe_dirs) if safe_dirs else "/usr/bin:/bin",
        "HOME": "/tmp",
    }


class TestCiClaudePShStatic(unittest.TestCase):
    """Static checks on bin/ci-claude-p.sh — file exists, syntax valid, no broken-path references."""

    def test_script_exists(self):
        self.assertTrue(SCRIPT.exists(), f"missing helper: {SCRIPT}")

    def test_script_is_executable(self):
        # The workflow invokes it as `bin/ci-claude-p.sh <skill> <pr_number>`
        # (no `bash` prefix), so non-executable permission would fail.
        mode = SCRIPT.stat().st_mode
        self.assertTrue(mode & 0o100,
                        f"{SCRIPT} is not executable (mode={oct(mode)})")

    def test_bash_n_syntax_check(self):
        # `bash -n` parses without executing — catches syntax errors at CI time.
        r = subprocess.run(["bash", "-n", str(SCRIPT)],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0,
                         f"bash -n failed:\nstdout={r.stdout}\nstderr={r.stderr}")

    def test_does_not_reference_claude_code_action_in_code(self):
        """The whole point of this script is to BYPASS
        `anthropics/claude-code-action` on workflow_dispatch. If a
        future maintainer reintroduces it, the workaround is broken."""
        text = SCRIPT.read_text(encoding="utf-8")
        # Strip ALL bash comments (lines starting with `#` or `#!`)
        # so the test only inspects code that actually runs. The
        # header block AND inline ` # ...` comments are both excluded.
        code_lines = [
            line for line in text.split("\n")
            if not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn("claude-code-action", code,
                         "bin/ci-claude-p.sh code (comments stripped) must "
                         "NOT reference claude-code-action — the whole "
                         "point is to bypass it on workflow_dispatch.")

    def test_does_not_use_inline_comment_mcp_tool_in_allowed_tools(self):
        """The MCP server `mcp__github_inline_comment__create_inline_comment`
        is broken on workflow_dispatch (issue #635). The workaround
        uses ONLY `Bash(gh pr comment:*)` for posting — the inline-
        comment MCP server must NOT appear in the script's
        --allowedTools list."""
        text = SCRIPT.read_text(encoding="utf-8")
        # Find the line containing `--allowedTools` and assert the MCP
        # name does NOT appear on it. (The header comment may name
        # the MCP server to explain the bug — that's allowed.)
        allowed_tools_lines = [
            line for line in text.splitlines()
            if "--allowedTools" in line
        ]
        self.assertTrue(allowed_tools_lines,
                        "bin/ci-claude-p.sh must have a --allowedTools line "
                        "(the assistant needs whitelisted tools to post comments).")
        for line in allowed_tools_lines:
            self.assertNotIn("mcp__github_inline_comment", line,
                             f"--allowedTools line {line!r} must NOT include "
                             "mcp__github_inline_comment__create_inline_comment — "
                             "that MCP server is broken on workflow_dispatch "
                             "(anthropics/claude-code-action#635).")

    def test_installs_claude_cli_when_missing(self):
        """The script must attempt to install Claude Code CLI when
        `command -v claude` fails AND `$HOME/.local/bin/claude`
        doesn't exist. We can't test the curl install (no network in
        CI), but we can pin that the install branch is present in
        the source text."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("claude.ai/install.sh", text,
                      "bin/ci-claude-p.sh must install Claude Code CLI "
                      "from https://claude.ai/install.sh when missing.")


class TestCiClaudePShArgValidation(unittest.TestCase):
    """Argument validation — wrong arity / skill / pr_number must exit non-zero with a hint."""

    def test_too_few_args_exits_nonzero(self):
        r = _run("review", env=_minimal_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr.lower())

    def test_one_arg_exits_nonzero(self):
        r = _run("review", env=_minimal_env())
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_skill_exits_nonzero(self):
        r = _run("banana", "42", env=_minimal_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("banana", r.stderr)

    def test_non_numeric_pr_exits_nonzero(self):
        r = _run("review", "forty-two", env=_minimal_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("forty-two", r.stderr)

    def test_empty_pr_exits_nonzero(self):
        r = _run("review", "", env=_minimal_env())
        self.assertNotEqual(r.returncode, 0)


class TestCiClaudePShRequiredEnv(unittest.TestCase):
    """Required env vars — missing vars must exit non-zero BEFORE attempting `claude`."""

    def test_missing_anthropic_model_exits_nonzero(self):
        env = _minimal_env()
        env.pop("ANTHROPIC_MODEL")
        r = _run("review", "42", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ANTHROPIC_MODEL", r.stderr)

    def test_missing_github_repository_exits_nonzero(self):
        env = _minimal_env()
        env.pop("GITHUB_REPOSITORY")
        r = _run("review", "42", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GITHUB_REPOSITORY", r.stderr)

    def test_missing_gh_token_exits_nonzero(self):
        env = _minimal_env()
        env.pop("GH_TOKEN")
        r = _run("review", "42", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GH_TOKEN", r.stderr)

    def test_missing_github_workspace_exits_nonzero(self):
        env = _minimal_env()
        env.pop("GITHUB_WORKSPACE")
        r = _run("review", "42", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GITHUB_WORKSPACE", r.stderr)


if __name__ == "__main__":
    unittest.main()
