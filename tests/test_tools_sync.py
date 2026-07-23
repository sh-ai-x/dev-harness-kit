#!/usr/bin/env python3
"""test_tools_sync.py — regression tests for hooks/tools-sync.sh.

Consumer projects install the dev-kit plugin and get commands/skills
whose bodies shell out to bundled tools/*.py scripts with a bare
relative path (e.g. /dev-kit:skill-usage -> `python3 tools/skill_usage.py`).
${CLAUDE_PLUGIN_ROOT} does not expand inside command markdown bodies
(anthropics/claude-code#9354), so those commands silently fail with
"No such file or directory" in any project that isn't dev-harness-kit's
own checkout. This SessionStart hook auto-copies the managed tool
scripts into the consumer project's ./tools/ so the bare relative path
resolves.

Verifies that the hook:
  - Copies the managed scripts into a throwaway consumer repo's
    ./tools/ when missing, from ${CLAUDE_PLUGIN_ROOT}/tools, emitting
    an additionalContext summary.
  - Copied files are byte-identical to the plugin-root source and
    executable.
  - Stays silent (no copy, no context) when the files already exist in
    cwd (dev-harness-kit's own checkout / worktree case).
  - Stays silent outside any git repo.
  - Empty payload: no crash, exit 0.
  - hooks.json (both Claude + Codex) wires tools-sync.sh into
    SessionStart.
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
HOOKS = REPO_ROOT / "hooks"
MANAGED_FILES = ("skill_usage.py", "skill_usage_normalize.py", "skill_usage_render.py")


def _run_hook(
    script: str,
    payload: dict,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(cwd) if cwd else None,
        env=full_env,
    )


def _session_payload(cwd: str = "") -> dict:
    p = {"hook_event_name": "SessionStart", "session_id": "test"}
    if cwd:
        p["cwd"] = cwd
    return p


def _init_git_repo() -> tempfile.TemporaryDirectory:
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return td


class TestToolsSyncConsumerRepo(unittest.TestCase):
    """Genuine consumer repo (no tools/ dir): hook must copy the
    managed scripts from ${CLAUDE_PLUGIN_ROOT}/tools."""

    def setUp(self):
        if not (HOOKS / "tools-sync.sh").exists():
            self.skipTest("tools-sync.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_copies_managed_scripts_and_emits_context(self):
        td = _init_git_repo()
        try:
            consumer_root = Path(td.name)
            for f in MANAGED_FILES:
                self.assertFalse((consumer_root / "tools" / f).exists())

            r = _run_hook(
                "tools-sync.sh",
                _session_payload(cwd=str(consumer_root)),
                cwd=consumer_root,
                env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
            )
            self.assertEqual(r.returncode, 0, f"rc={r.returncode} stderr={r.stderr}")
            doc = json.loads(r.stdout)
            ctx = doc.get("hookSpecificOutput", {}).get("additionalContext", "")
            self.assertIn("tools-sync:", ctx, f"missing tools-sync marker: {ctx!r}")

            for f in MANAGED_FILES:
                copied = consumer_root / "tools" / f
                self.assertTrue(copied.exists(), f"{f} not copied")
                self.assertEqual(
                    copied.read_bytes(), (REPO_ROOT / "tools" / f).read_bytes(),
                    f"{f} content mismatch vs plugin-root source",
                )
            executable = consumer_root / "tools" / "skill_usage.py"
            self.assertTrue(os.access(executable, os.X_OK), "copied skill_usage.py not executable")
        finally:
            td.cleanup()


class TestToolsSyncAlreadyPresent(unittest.TestCase):
    """Files already present (dev-harness-kit's own checkout/worktree
    case): hook must stay silent, no re-copy, no context."""

    def setUp(self):
        if not (HOOKS / "tools-sync.sh").exists():
            self.skipTest("tools-sync.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_silent_when_already_present(self):
        td = _init_git_repo()
        try:
            root = Path(td.name)
            (root / "tools").mkdir()
            marker = "already-here"
            for f in MANAGED_FILES:
                (root / "tools" / f).write_text(marker)

            r = _run_hook(
                "tools-sync.sh",
                _session_payload(cwd=str(root)),
                cwd=root,
                env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
            )
            self.assertEqual(r.returncode, 0, f"rc={r.returncode} stderr={r.stderr}")
            self.assertNotIn("tools-sync:", r.stdout)
            for f in MANAGED_FILES:
                self.assertEqual((root / "tools" / f).read_text(), marker, f"{f} was overwritten")
        finally:
            td.cleanup()


class TestToolsSyncOutsideGit(unittest.TestCase):
    """Outside any git repo: hook stays silent."""

    def setUp(self):
        if not (HOOKS / "tools-sync.sh").exists():
            self.skipTest("tools-sync.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_silent_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook(
                "tools-sync.sh",
                _session_payload(cwd=tmp),
                cwd=Path(tmp),
                env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
            )
            self.assertEqual(r.returncode, 0)
            self.assertNotIn("tools-sync:", r.stdout)
            self.assertFalse((Path(tmp) / "tools").exists())


class TestToolsSyncEmptyPayload(unittest.TestCase):
    """No cwd in payload: hook must not crash; exit 0."""

    def setUp(self):
        if not (HOOKS / "tools-sync.sh").exists():
            self.skipTest("tools-sync.sh not found")
        if not shutil.which("jq"):
            self.skipTest("jq not installed; hook fails open")

    def test_empty_payload(self):
        r = _run_hook("tools-sync.sh", _session_payload(), env={"CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)})
        self.assertEqual(r.returncode, 0, f"rc={r.returncode} stderr={r.stderr}")


class TestToolsSyncWiring(unittest.TestCase):
    """hooks.json (Claude + Codex) must register tools-sync.sh under SessionStart."""

    def _hooks_under(self, cfg: dict, event: str) -> list:
        flat = []
        for entry in cfg["hooks"].get(event, []):
            for h in entry.get("hooks", []):
                flat.append(h)
        return flat

    def test_wired_into_claude_sessionstart(self):
        path = REPO_ROOT / "hooks" / "hooks.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        hooks = self._hooks_under(cfg, "SessionStart")
        match = [h for h in hooks if "tools-sync.sh" in h.get("command", "")]
        self.assertTrue(match, f"tools-sync.sh not wired into Claude SessionStart. Got: {hooks}")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", match[0]["command"])

    def test_wired_into_codex_sessionstart(self):
        path = REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        hooks = self._hooks_under(cfg, "SessionStart")
        match = [h for h in hooks if "tools-sync.sh" in h.get("command", "")]
        self.assertTrue(match, f"tools-sync.sh not wired into Codex SessionStart. Got: {hooks}")
        self.assertIn("${PLUGIN_ROOT}", match[0]["command"])


if __name__ == "__main__":
    unittest.main()
