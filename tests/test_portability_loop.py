from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
PORTABILITY = ROOT / "tools" / "portability_check.py"
LOOP = ROOT / "tools" / "loop_engine.py"


class TestPortabilityCheck(unittest.TestCase):
    def test_current_manifests_are_portable(self):
        proc = subprocess.run(
            [sys.executable, str(PORTABILITY), "--json", "--project-root", str(ROOT)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["portable"], report["findings"])
        self.assertEqual(report["providers"], ["claude", "codex"])

    def test_detects_provider_hook_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "hooks").mkdir()
            (root / ".codex-plugin" / "hooks").mkdir(parents=True)
            manifest = {"name": "dev-kit", "version": "1"}
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
            (root / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest))
            claude = {"hooks": {"Stop": [{"hooks": [{"command": "stop.sh"}]}]}}
            codex = {"hooks": {"Stop": []}}
            (root / "hooks" / "hooks.json").write_text(json.dumps(claude))
            (root / ".codex-plugin" / "hooks" / "hooks.json").write_text(json.dumps(codex))
            proc = subprocess.run(
                [sys.executable, str(PORTABILITY), "--json", "--project-root", str(root)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1)
            report = json.loads(proc.stdout)
            self.assertFalse(report["portable"])
            self.assertTrue(any("hook parity" in item for item in report["findings"]))

    def test_dev_kit_agent_prefix_does_not_break_parity(self):
        """A per-runtime `DEV_KIT_AGENT=<value> ` command prefix (added so
        the harness-effectiveness stability submetric can see which agent
        emitted an event) is an intentional, expected divergence between
        the Claude and Codex hooks.json — the portability check must not
        flag it as drift.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "hooks").mkdir()
            (root / ".codex-plugin" / "hooks").mkdir(parents=True)
            manifest = {"name": "dev-kit", "version": "1"}
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
            (root / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest))
            claude = {"hooks": {"Stop": [{"hooks": [{
                "command": "DEV_KIT_AGENT=claude-code bash ${CLAUDE_PLUGIN_ROOT}/hooks/trace-session-end.sh"
            }]}]}}
            codex = {"hooks": {"Stop": [{"hooks": [{
                "command": "DEV_KIT_AGENT=codex bash ${PLUGIN_ROOT}/hooks/trace-session-end.sh"
            }]}]}}
            (root / "hooks" / "hooks.json").write_text(json.dumps(claude))
            (root / ".codex-plugin" / "hooks" / "hooks.json").write_text(json.dumps(codex))
            proc = subprocess.run(
                [sys.executable, str(PORTABILITY), "--json", "--project-root", str(root)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report["portable"], report["findings"])


class TestLoopEngine(unittest.TestCase):
    def _fixture(self, features: list[dict], test_body: str = "") -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "features.json").write_text(json.dumps(features), encoding="utf-8")
        (root / "check.py").write_text(test_body or "print('green')\n", encoding="utf-8")
        return root

    def test_iterate_records_result_without_marking_feature_done(self):
        root = self._fixture([{
            "id": "F-1", "description": "first", "status": "failing",
            "depends_on": [], "test_path": "check.py",
        }])
        proc = subprocess.run(
            [sys.executable, str(LOOP), "iterate", "--project-root", str(root),
             "--feature-list", "features.json"], capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        checkpoint = json.loads((root / ".dev-kit" / "loop-checkpoint.json").read_text())
        self.assertEqual(checkpoint["iteration"], 1)
        self.assertEqual(checkpoint["last"]["feature_id"], "F-1")
        self.assertEqual(checkpoint["last"]["exit_code"], 0)
        self.assertEqual(json.loads((root / "features.json").read_text())[0]["status"], "failing")

    def test_verify_rejects_checkpoint_for_unknown_feature(self):
        root = self._fixture([{
            "id": "F-1", "description": "first", "status": "failing",
            "depends_on": [], "test_path": "check.py",
        }])
        state_dir = root / ".dev-kit"
        state_dir.mkdir()
        (state_dir / "loop-checkpoint.json").write_text(json.dumps({
            "schema_version": "1", "iteration": 1,
            "last": {"feature_id": "MISSING", "exit_code": 0},
        }))
        proc = subprocess.run(
            [sys.executable, str(LOOP), "verify", "--project-root", str(root),
             "--feature-list", "features.json"], capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown feature", proc.stderr)


if __name__ == "__main__":
    unittest.main()
