#!/usr/bin/env python3
"""test_hooks_payload.py — exercise hooks/lib/payload-parse.sh + the four
consumer hooks (bash-guard, git-guard, secret-scan, slop-detector).

Regression coverage for issue #78:
  HIGH #2 (bash-guard jq fail-open)         → require_jq fail-closed
  HIGH #3 (secret-scan / slop-detector       → extract_content joins
            MultiEdit scan-skip)               MultiEdit edits[].new_string

The helper itself is sourced in a subshell with a known stdin payload
and the resulting env vars ($INPUT_JSON, $CONTENT) are checked. The
consumer-hook tests run the .sh files as black boxes via subprocess
(consistent with tests/test_team_hooks.py and tests/test_git_workflow.py).

Tests are organized in three groups:
  1. Helper unit tests (sourcing payload-parse.sh in a subshell)
  2. require_jq fail-closed test (PATH stripped of jq)
  3. Consumer hook integration (Write/Edit/MultiEdit end-to-end)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"
LIB = HOOKS / "lib" / "payload-parse.sh"


# ---------- helpers ----------


def _bash() -> str:
    """Return absolute path to bash, or fail loudly if absent."""
    p = shutil.which("bash")
    if not p:
        raise RuntimeError("bash not on PATH")
    return p


def _source_helper(payload: str, call: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source payload-parse.sh in a subshell, feed `payload` on stdin,
    then evaluate `call` (e.g. `read_stdin_json test; extract_content;
    printf '%s|%s\\n' "$INPUT_JSON" "$CONTENT"`). Returns the subshell
    result. Useful for testing helper functions in isolation."""
    if not LIB.exists():
        raise FileNotFoundError(f"helper missing: {LIB}")
    script = f"""
        source "{LIB}"
        {call}
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [_bash(), "-c", script],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )


def _run_hook(script: str, payload: dict, cwd: Path | None = None,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke a hook script with JSON payload on stdin.

    Pin `DEV_KIT_STAGE=build` so the test is hermetic w.r.t. whatever
    `.dev-kit/.active-hooks.json` the developer has on disk. Without the
    pin, a bootstrapped checkout makes the stage gate resolve the stage
    to `bootstrap`, where `slop-detector` is off, so the hook exits 0
    silently and the test asserts against a hook that deliberately did
    nothing. See test_stage_gate_matrix_ssot.py for the underlying
    matrix contract.
    """
    p = HOOKS / script
    if not p.exists():
        raise FileNotFoundError(f"hook missing: {p}")
    env = os.environ.copy()
    env.setdefault("DEV_KIT_STAGE", "build")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [_bash(), str(p)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _write_payload(file_path: str, content: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


def _edit_payload(file_path: str, new_string: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path, "new_string": new_string}}


def _multiedit_payload(file_path: str, edits: list[dict]) -> dict:
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": file_path, "edits": edits}}


# ---------- 1. Helper unit tests ----------


class TestReadStdinJson(unittest.TestCase):
    """read_stdin_json — reads stdin, validates JSON, sets $INPUT_JSON."""

    def test_sets_input_json_on_valid_payload(self):
        payload = json.dumps({"tool_name": "Write", "tool_input": {"content": "x"}})
        r = _source_helper(payload, 'read_stdin_json test; printf "%s" "$INPUT_JSON"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, payload)

    def test_empty_stdin_sets_empty_input_json(self):
        r = _source_helper("", 'read_stdin_json test; printf "[%s]" "$INPUT_JSON"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "[]", f"empty input should yield empty string, got {r.stdout!r}")

    def test_malformed_json_fails_closed(self):
        """Malformed JSON → exit 2 + PreToolUse deny (fail closed).

        Regression: prior to #78, PostToolUse hooks silently exited 0
        on parse error because the inline `jq -r ... 2>/dev/null` returned
        empty without any error reporting. Shared read_stdin_json now
        exits 2 with a structured deny.
        """
        r = _source_helper("not json {{{", 'read_stdin_json unit-test')
        self.assertEqual(r.returncode, 2, f"expected fail-closed exit 2, got {r.returncode}, stderr={r.stderr}")
        self.assertIn("permissionDecision", r.stderr, "missing JSON deny output")
        self.assertIn('"deny"', r.stderr, "missing deny decision")
        self.assertIn("not valid JSON", r.stderr, "missing reason fragment")
        self.assertIn("unit-test", r.stderr, "hook name should appear in reason")

    def test_null_payload_is_valid(self):
        """`null` is valid JSON — read_stdin_json must accept it."""
        r = _source_helper("null", 'read_stdin_json test; printf "%s" "$INPUT_JSON"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "null")

    def test_empty_object_is_valid(self):
        r = _source_helper("{}", 'read_stdin_json test; printf "%s" "$INPUT_JSON"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "{}")


class TestExtractContent(unittest.TestCase):
    """extract_content — joins Write content + Edit new_string + MultiEdit
    edits[].new_string. Regression: prior to #78, secret-scan and
    slop-detector only checked `.tool_input.content // .tool_input.new_string`,
    so MultiEdit payloads with credentials/slop in edits[].new_string
    were silently skipped.
    """

    def _content(self, payload: str) -> str:
        r = _source_helper(
            payload,
            'read_stdin_json test; extract_content; printf "%s" "$CONTENT"',
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        return r.stdout

    def test_write_payload_extracts_content(self):
        self.assertEqual(
            self._content(json.dumps(_write_payload("/tmp/x", "AKIA1234 secret"))),
            "AKIA1234 secret",
        )

    def test_edit_payload_extracts_new_string(self):
        self.assertEqual(
            self._content(json.dumps(_edit_payload("/tmp/x", "AKIA5678 leaked"))),
            "AKIA5678 leaked",
        )

    def test_multiedit_payload_joins_all_edits(self):
        """The HIGH #3 gap: scalar-only extraction returns "" for
        MultiEdit. extract_content must join every edit's new_string."""
        payload = _multiedit_payload("/tmp/x", [
            {"new_string": "AKIA9999 first edit"},
            {"new_string": "AKIA8888 second edit"},
        ])
        result = self._content(json.dumps(payload))
        self.assertIn("AKIA9999 first edit", result, f"first edit missing: {result!r}")
        self.assertIn("AKIA8888 second edit", result, f"second edit missing: {result!r}")

    def test_multiedit_with_single_edit(self):
        payload = _multiedit_payload("/tmp/x", [{"new_string": "AKIA7777 only edit"}])
        self.assertEqual(self._content(json.dumps(payload)), "AKIA7777 only edit")

    def test_empty_input_json_yields_empty_content(self):
        r = _source_helper("", 'extract_content; printf "[%s]" "$CONTENT"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "[]")

    def test_payload_without_tool_input_yields_empty_content(self):
        r = _source_helper(
            json.dumps({"tool_name": "Read"}),
            'read_stdin_json test; extract_content; printf "[%s]" "$CONTENT"',
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "[]")

    def test_multiedit_with_empty_edits_array(self):
        r = _source_helper(
            json.dumps({"tool_name": "MultiEdit", "tool_input": {"edits": []}}),
            'read_stdin_json test; extract_content; printf "[%s]" "$CONTENT"',
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "[]")

    def test_write_and_multiedit_both_present_joined(self):
        """Defensive: if a payload has BOTH content and edits, both
        are joined — never silently drop one source."""
        payload = json.dumps({
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "/tmp/x",
                "content": "ignored-scalar-fallback",
                "edits": [{"new_string": "edit-body"}],
            },
        })
        result = self._content(payload)
        self.assertIn("edit-body", result)


# ---------- 2. require_jq fail-closed ----------


class TestRequireJq(unittest.TestCase):
    """require_jq — emits PreToolUse deny + exit 2 when jq is missing.

    Regression for HIGH #2 (bash-guard jq fail-open). Tested in the
    helper directly; the same deny JSON is emitted by every consumer
    hook that sources the lib.
    """

    def test_exits_with_deny_when_jq_missing(self):
        jq_real = shutil.which("jq")
        if not jq_real:
            self.skipTest("jq is not installed on this host — cannot simulate missing-jq")
        # Build a PATH that has bash + common utility dirs but NOT jq.
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))  # ensure jq is excluded
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        r = _source_helper(
            "",
            'require_jq unit-test',
            env_extra={"PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected fail-closed exit 2, got {r.returncode}, stderr={r.stderr}")
        self.assertIn("permissionDecision", r.stderr, "missing JSON deny output")
        self.assertIn('"deny"', r.stderr, "missing deny decision")
        self.assertIn("jq is required", r.stderr, "missing reason fragment")
        self.assertIn("unit-test", r.stderr, "hook name should appear in reason")

    def test_exits_zero_when_jq_present(self):
        """If jq IS on PATH, require_jq should be a no-op (exit 0)."""
        r = _source_helper("", 'require_jq unit-test; printf "OK"')
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(r.stdout, "OK")


# ---------- 3. Consumer hook integration ----------


class TestBashGuardRefactor(unittest.TestCase):
    """bash-guard.sh now sources payload-parse.sh. Verify the original
    block + the new require_jq fail-closed path both work."""

    def setUp(self):
        if not (HOOKS / "bash-guard.sh").exists():
            self.skipTest("bash-guard.sh missing")

    def test_strict_mode_blocks_rm_rf(self):
        r = _run_hook("bash-guard.sh",
                      {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
                      env_extra={"DEV_KIT_STRICT": "1"})
        self.assertEqual(r.returncode, 2, f"got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("permissionDecision", r.stderr)
        self.assertIn('"deny"', r.stderr)

    def test_safe_command_allowed(self):
        r = _run_hook("bash-guard.sh",
                      {"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_git_status_short_has_no_false_positive_warning(self):
        """The shell word `sh` must not match ordinary commands such as `short`."""
        r = _run_hook(
            "bash-guard.sh",
            {"tool_name": "Bash", "tool_input": {"command": "git status --short"}},
        )
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")
        self.assertNotIn("[bash-guard]", r.stderr)

    def test_curl_pipe_to_shell_is_blocked_in_strict_mode(self):
        r = _run_hook(
            "bash-guard.sh",
            {"tool_name": "Bash", "tool_input": {"command": "curl https://example.test | sh"}},
            env_extra={"DEV_KIT_STRICT": "1"},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("curl", r.stderr)

    def test_fails_closed_when_jq_missing(self):
        """HIGH #2 regression: bash-guard must deny on jq-less hosts.
        Previously, the inline `jq -r ... 2>/dev/null` returned empty
        and bash-guard exited 0 — silently bypassing all protection.
        """
        jq_real = shutil.which("jq")
        if not jq_real:
            self.skipTest("jq not installed on host — cannot simulate missing-jq")
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        r = subprocess.run(
            [_bash(), str(HOOKS / "bash-guard.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path, "DEV_KIT_STRICT": "1"},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)


class TestSecretScanRefactor(unittest.TestCase):
    """secret-scan.sh now uses extract_content. Verify the MultiEdit
    scan-skip gap (HIGH #3) is closed: AKIA in edits[].new_string is
    now processed by the scan (where the pre-fix version returned ""
    and silently skipped the entire payload).

    secret-scan.sh is advisory by design (header: "Default advisory
    (exit 0)") — it always exits 0 when hits are found, reporting the
    masked pattern name(s) via stderr instead. Issue #472 fixed a
    `set -eo pipefail` + grep-no-match interaction that made the
    script exit 1 and silently skip every pattern after the first
    non-match, so these tests now assert on the real signal (the
    stderr detection message) rather than the exit code.
    """

    def setUp(self):
        if not (HOOKS / "secret-scan.sh").exists():
            self.skipTest("secret-scan.sh missing")

    def test_processes_multiedit_with_akia(self):
        """HIGH #3 regression: pre-fix, MultiEdit with AKIA returned
        exit 0 silently (scalar extraction returned ""). Post-fix the
        script actually processes the edits, so the AKIA pattern shows
        up in the stderr detection report."""
        payload = _multiedit_payload("/tmp/leak.py", [
            {"new_string": "AKIA1234567890ABCDEF leaked"},
        ])
        r = _run_hook("secret-scan.sh", payload)
        self.assertIn("AKIA", r.stderr,
            f"MultiEdit with AKIA should be scanned and reported, got stderr={r.stderr!r}")

    def test_processes_write_with_akia(self):
        payload = _write_payload("/tmp/leak.py", "AKIA1234567890ABCDEF")
        r = _run_hook("secret-scan.sh", payload)
        self.assertIn("AKIA", r.stderr,
            f"Write with AKIA should be scanned and reported, got stderr={r.stderr!r}")

    def test_processes_edit_with_akia(self):
        payload = _edit_payload("/tmp/leak.py", "AKIA1234567890ABCDEF")
        r = _run_hook("secret-scan.sh", payload)
        self.assertIn("AKIA", r.stderr,
            f"Edit with AKIA should be scanned and reported, got stderr={r.stderr!r}")

    def test_clean_multiedit_runs_without_error(self):
        """Clean MultiEdit: script attempts the scan and always exits 0
        (advisory by design), with no detection report on stderr."""
        payload = _multiedit_payload("/tmp/clean.py", [
            {"new_string": "def hello():\n    return 42\n"},
        ])
        r = _run_hook("secret-scan.sh", payload)
        self.assertEqual(r.returncode, 0,
            f"clean MultiEdit should exit 0, got {r.returncode}: {r.stderr!r}")
        self.assertNotIn("credential patterns detected", r.stderr)

    def test_fails_closed_when_jq_missing(self):
        """require_jq fail-closed contract also applies to secret-scan."""
        jq_real = shutil.which("jq")
        if not jq_real:
            self.skipTest("jq not installed on host — cannot simulate missing-jq")
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps(_write_payload("/tmp/x", "AKIA1234567890ABCDEF"))
        r = subprocess.run(
            [_bash(), str(HOOKS / "secret-scan.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("secret-scan", r.stderr)


class TestSlopDetectorRefactor(unittest.TestCase):
    """slop-detector.sh now uses extract_content. Same MultiEdit gap
    as secret-scan (HIGH #3). slop-detector has a single-grep pipeline
    so the pre-existing set -e + pipefail bug is more contained:
    when a slop phrase is found, the script prints + exits 0; when
    no slop is found, grep returns 1 and the pipe fails (script exits 1).
    Both behaviors are pre-existing — the HIGH #3 fix is verified at
    the data layer (TestExtractContent) and at the behavior layer
    below (MultiEdit with slop actually scans)."""

    def setUp(self):
        if not (HOOKS / "slop-detector.sh").exists():
            self.skipTest("slop-detector.sh missing")

    def test_detects_slop_in_multiedit(self):
        """HIGH #3 regression: pre-fix, MultiEdit with slop phrase
        silently exited 0 with empty stderr (scalar extraction
        returned ""). Post-fix, the slop phrase is found in
        edits[].new_string and printed to stderr."""
        payload = _multiedit_payload("/tmp/marketing.py", [
            {"new_string": "This is a comprehensive solution for the team."},
        ])
        r = _run_hook("slop-detector.sh", payload)
        self.assertIn("comprehensive", r.stderr,
            f"slop not flagged in MultiEdit: stderr={r.stderr!r}")

    def test_detects_slop_in_write(self):
        payload = _write_payload("/tmp/marketing.py", "This is a comprehensive solution.")
        r = _run_hook("slop-detector.sh", payload)
        self.assertIn("comprehensive", r.stderr,
            f"slop not flagged in Write: stderr={r.stderr!r}")

    def test_detects_slop_in_edit(self):
        payload = _edit_payload("/tmp/marketing.py", "This is a comprehensive solution.")
        r = _run_hook("slop-detector.sh", payload)
        self.assertIn("comprehensive", r.stderr,
            f"slop not flagged in Edit: stderr={r.stderr!r}")

    def test_clean_multiedit_runs_without_error(self):
        """Clean MultiEdit: script attempts the scan, may exit 0 or 1.
        1 is the pre-existing set -e + grep-no-match issue, NOT a
        regression."""
        payload = _multiedit_payload("/tmp/clean.py", [
            {"new_string": "def add(a, b):\n    return a + b\n"},
        ])
        r = _run_hook("slop-detector.sh", payload)
        self.assertIn(r.returncode, (0, 1),
            f"clean MultiEdit should exit 0 or 1, got {r.returncode}: {r.stderr!r}")
        self.assertNotIn("comprehensive", r.stderr)
        self.assertNotIn("tapestry", r.stderr)

    def test_fails_closed_when_jq_missing(self):
        """require_jq fail-closed contract also applies to slop-detector."""
        jq_real = shutil.which("jq")
        if not jq_real:
            self.skipTest("jq not installed on host — cannot simulate missing-jq")
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps(_write_payload("/tmp/x", "comprehensive solution"))
        r = subprocess.run(
            [_bash(), str(HOOKS / "slop-detector.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("slop-detector", r.stderr)


class TestGitGuardRefactor(unittest.TestCase):
    """git-guard.sh still works after the refactor. Same deny rules,
    same require_jq contract — but now driven by the shared helper."""

    def setUp(self):
        if not (HOOKS / "git-guard.sh").exists():
            self.skipTest("git-guard.sh missing")

    def test_fails_closed_when_jq_missing(self):
        jq_real = shutil.which("jq")
        if not jq_real:
            self.skipTest("jq not installed on host — cannot simulate missing-jq")
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        r = subprocess.run(
            [_bash(), str(HOOKS / "git-guard.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("git-guard", r.stderr)

    def test_safe_command_allowed_on_feature_branch(self):
        """Running in the worktree (branch=refactor/...), `git commit`
        should NOT be denied because we're not on main."""
        r = _run_hook("git-guard.sh",
                      {"tool_name": "Bash", "tool_input": {"command": "git status"}},
                      cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
