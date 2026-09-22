"""Regression tests for the single-command SessionStart dispatcher."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = ROOT / "hooks" / "session-start.sh"
CHILDREN = (
    "session-start-check.sh",
    "log-on-session-start.sh",
    "provider-divergence-check.sh",
    "linear-session-start.sh",
    "worktree-janitor-session-start.sh",
    "session-start-harness-mode-reset.sh",
    "session-start-guard-mode-reset.sh",
    "plugin-cache-refresh.sh",
)


class TestSessionStartDispatcher(unittest.TestCase):
    def test_registry_count_and_stop_order_are_pinned(self) -> None:
        for manifest in (ROOT / "hooks" / "hooks.json", ROOT / ".codex-plugin" / "hooks" / "hooks.json"):
            config = json.loads(manifest.read_text(encoding="utf-8"))
            entries = [
                hook
                for groups in config["hooks"].values()
                for group in groups
                for hook in group.get("hooks", [])
            ]
            self.assertEqual(len(entries), 28, manifest.as_posix())
            stop_hooks = config["hooks"]["Stop"]
            self.assertIn("trace-session-end.sh", stop_hooks[0]["hooks"][0]["command"])
            self.assertIn("stop-verify.sh", stop_hooks[1]["hooks"][0]["command"])

    def test_manifest_registers_one_dispatcher_per_runtime(self) -> None:
        for manifest in (ROOT / "hooks" / "hooks.json", ROOT / ".codex-plugin" / "hooks" / "hooks.json"):
            config = json.loads(manifest.read_text(encoding="utf-8"))
            entries = config["hooks"]["SessionStart"]
            commands = [h["command"] for e in entries for h in e.get("hooks", [])]
            self.assertEqual(len(commands), 1, manifest.as_posix())
            self.assertIn("session-start.sh", commands[0])
            self.assertNotIn("session-start-check.sh", " ".join(commands))

    def test_dispatcher_fans_out_payload_and_merges_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy2(DISPATCHER, root / "session-start.sh")
            capture = root / "capture"
            capture.mkdir()
            for index, child in enumerate(CHILDREN):
                (root / child).write_text(
                    "#!/usr/bin/env bash\n"
                    "payload=$(cat)\n"
                    f"printf '%s' \"$payload\" > \"$DISPATCH_CAPTURE_DIR/{index}\"\n"
                    f"printf '{{\"hookSpecificOutput\":{{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"child-{index}\"}}}}\\n'\n",
                    encoding="utf-8",
                )
                (root / child).chmod(0o755)
            env = {**os.environ, "TMPDIR": temp, "DISPATCH_CAPTURE_DIR": str(capture)}
            payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "dispatch-test"})
            result = subprocess.run(
                ["bash", str(root / "session-start.sh")],
                input=payload,
                capture_output=True,
                text=True,
                cwd=root,
                env=env,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(context, "\n".join(f"child-{index}" for index in range(len(CHILDREN))))
            for index in range(len(CHILDREN)):
                self.assertIn(f"child-{index}", context)
                self.assertEqual((capture / str(index)).read_text(encoding="utf-8"), payload)
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")


if __name__ == "__main__":
    unittest.main()
