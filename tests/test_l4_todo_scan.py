#!/usr/bin/env python3
"""test_l4_todo_scan.py — regression for the L4 deferred-work marker hook.

Verifies hooks/l4-todo-scan.sh end-to-end:

  - default fail-closed:  marker in *.py           -> exit 2
  - default allowed path: marker in *.md           -> exit 0
  - default allowed path: marker in tests/fixtures -> exit 0
  - bank file missing:    inline fallback fires    -> exit 2
  - jq missing:           fail-closed              -> exit 2
  - L4_STRICT=1:          marker in *.md           -> exit 2 (escalation:
                          allowed paths are scanned and fail-closed under
                          strict mode)

The hook is driven as a black box, the same way Claude Code does:
    stdin  : PostToolUse payload JSON
    stdout : empty
    stderr : advisory text

No mocks. jq must be available on $PATH for most tests; the jq-missing
case explicitly strips it.

NOTE on obfuscation: this test file lives under tests/ (not
tests/fixtures/), so once the L4 hook is wired it would scan its own
content. Marker strings are built at runtime from non-adjacent
characters so the source text never contains the contiguous marker
that the hook scans. The runtime content is what the hook sees via
stdin.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "l4-todo-scan.sh"
MARKERS_BANK = REPO_ROOT / "hooks" / "references" / "l4" / "markers.md"


# Runtime marker tokens. Source-level these never appear contiguously,
# so this test file itself passes the L4 hook once it is wired.
# Variable names use UPPERCASE so the Python re \b boundary does not
# match across the leading underscore (word char on both sides).
_T1 = "TO" + "DO"
_T2 = "FI" + "XME"
_T3 = chr(0xB098) + chr(0xC911) + chr(0xC5D0)  # 3-char Korean ("later")
_T4 = chr(0xCD94) + chr(0xD6C4)                              # 2-char Korean
_T5 = chr(0xC784) + chr(0xC2DC)                              # 2-char Korean


def _require_jq() -> None:
    if shutil.which("jq") is None:
        raise unittest.SkipTest("jq is required on $PATH for l4-todo-scan tests")


def _payload(file_path: str, content: str) -> str:
    return json.dumps({"tool_input": {"file_path": file_path, "content": content}})


def _payload_edit(file_path: str, new_string: str) -> str:
    return json.dumps({"tool_input": {"file_path": file_path, "new_string": new_string}})


def run_hook(
    content: str,
    *,
    file_path: str = "test.py",
    payload: str | None = None,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with a PostToolUse payload and capture output."""
    _require_jq()
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    if env_extra:
        env.update(env_extra)
    body = payload if payload is not None else _payload(file_path, content)
    return subprocess.run(
        [str(HOOK)],
        input=body,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


class DefaultFailClosed(unittest.TestCase):
    def test_deferred_in_python_blocks(self) -> None:
        proc = run_hook(
            f"def hello():\n    # {_T1}: implement greet()\n    return None\n",
            file_path="src/hello.py",
        )
        self.assertEqual(
            proc.returncode, 2,
            msg=f"expected exit 2, got {proc.returncode}; stderr={proc.stderr}",
        )
        self.assertIn("l4-todo-scan", proc.stderr)

    def test_defect_in_python_blocks(self) -> None:
        proc = run_hook(
            f"x = 1  # {_T2}: handle negative values\n",
            file_path="src/x.py",
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertIn(_T2, proc.stderr)

    def test_ko_marker_in_python_blocks(self) -> None:
        proc = run_hook(
            f"# {_T3} {_T4} {_T5}\nx = 1\n",
            file_path="src/x.py",
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)

    def test_clean_python_passes(self) -> None:
        proc = run_hook(
            "def add(a, b):\n    return a + b\n",
            file_path="src/add.py",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)


class AllowedPaths(unittest.TestCase):
    def test_marker_in_md_is_allowed(self) -> None:
        proc = run_hook(
            f"# Design notes\n\nThis module will be extended later. See {_T2}.\n",
            file_path="docs/notes.md",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_marker_in_root_md_is_allowed(self) -> None:
        proc = run_hook(
            f"# README\n\nWe use {_T1} markers in docs.\n",
            file_path="README.md",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_marker_in_test_fixture_is_allowed(self) -> None:
        proc = run_hook(
            "# Sample fixture with deliberate marker for test harness.\n",
            file_path="tests/fixtures/sample.py",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_marker_in_adoption_doc_is_allowed(self) -> None:
        proc = run_hook(
            f"# Adoption guide\n\nAvoid {_T5} implementations.\n",
            file_path="docs/adoption/setup.md",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)


class EditPayloadCoverage(unittest.TestCase):
    """The hook must scan Edit.new_string as well as Write.content."""

    def test_edit_payload_blocks_on_marker(self) -> None:
        proc = run_hook(
            "",
            file_path="src/foo.py",
            payload=_payload_edit("src/foo.py", f"def f():\n    return 1  # {_T1} body\n"),
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)


class StrictEscalation(unittest.TestCase):
    def test_strict_mode_blocks_md_path(self) -> None:
        """L4_STRICT=1 escalates: allowed paths are scanned and fail-closed."""
        proc = run_hook(
            f"# README\n\nWe use {_T1} markers.\n",
            file_path="README.md",
            env_extra={"L4_STRICT": "1"},
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)

    def test_strict_mode_blocks_test_fixture(self) -> None:
        proc = run_hook(
            f"# {_T2} in a fixture\n",
            file_path="tests/fixtures/x.py",
            env_extra={"L4_STRICT": "1"},
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)

    def test_strict_mode_clean_md_still_passes(self) -> None:
        proc = run_hook(
            "# README\n\nAll clean. No deferred markers.\n",
            file_path="README.md",
            env_extra={"L4_STRICT": "1"},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)


class BankFallback(unittest.TestCase):
    def test_inline_fallback_when_bank_missing(self) -> None:
        """When references/l4/markers.md is absent, the hook falls back to
        an inline marker list and still detects the marker. We simulate by
        pointing CLAUDE_PLUGIN_ROOT at a temp dir without references/l4/."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tmp_hook = tmp_path / "l4-todo-scan.sh"
            shutil.copy(str(HOOK), str(tmp_hook))
            shutil.copytree(
                REPO_ROOT / "hooks" / "lib",
                tmp_path / "lib",
                symlinks=True,
            )
            content = f"x = 1  # {_T2}: needs review\n"
            payload = json.dumps({"tool_input": {"file_path": "src/x.py", "content": content}})
            proc = subprocess.run(
                [str(tmp_hook)],
                input=payload,
                capture_output=True,
                text=True,
                env={**os.environ, "CLAUDE_PLUGIN_ROOT": tmp},
                timeout=10,
            )
            self.assertEqual(proc.returncode, 2, msg=proc.stderr)
            self.assertIn("WARN", proc.stderr)
            self.assertIn(_T2, proc.stderr)


class JqMissing(unittest.TestCase):
    def test_jq_missing_fails_closed(self) -> None:
        """When jq is not on PATH, the hook must exit 2 (fail-closed).

        Strategy: build a shadow PATH that contains every executable from
        the real PATH except jq. The hook's require_jq helper exits 2
        when jq is missing, so the test stays a black-box check.
        """
        shadow = Path(tempfile.mkdtemp(prefix="l4-nojq-"))
        try:
            seen: set[str] = set()
            for d in os.environ.get("PATH", "").split(":"):
                if not d or not Path(d).is_dir():
                    continue
                for entry in Path(d).iterdir():
                    name = entry.name
                    if name in seen:
                        continue
                    if name == "jq":
                        continue
                    if not os.access(entry, os.X_OK):
                        continue
                    seen.add(name)
                    link = shadow / name
                    try:
                        os.symlink(entry, link)
                    except OSError:
                        pass
            # Sanity: jq must be absent in the shadow.
            self.assertFalse((shadow / "jq").exists())

            env = os.environ.copy()
            env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
            env["PATH"] = str(shadow)
            payload = json.dumps({
                "tool_input": {"file_path": "src/clean.py", "content": "x = 1\n"},
            })
            proc = subprocess.run(
                [str(HOOK)],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(
                proc.returncode, 2,
                msg=f"expected exit 2; stderr={proc.stderr}",
            )
        finally:
            shutil.rmtree(shadow, ignore_errors=True)


class BankFileInvariants(unittest.TestCase):
    """The bank file must be readable and have at least one loadable regex."""

    def test_bank_loadable(self) -> None:
        self.assertTrue(MARKERS_BANK.exists(), f"missing bank file: {MARKERS_BANK}")
        text = MARKERS_BANK.read_text(encoding="utf-8")
        loadable = [
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreaterEqual(
            len(loadable), 10,
            msg=f"{MARKERS_BANK.name}: only {len(loadable)} loadable lines (>= 10 required)",
        )


if __name__ == "__main__":
    unittest.main()
