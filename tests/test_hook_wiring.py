"""tests/test_hook_wiring.py — Regression tests that verify every PostToolUse
hook the harness promises is actually wired into BOTH runtimes
(Claude + Codex).

The dual-runtime scan (Claude + Codex) closes the gap from issue #672
("Wiring test covers Claude only, not Codex") — the previous version of
this test only loaded `hooks/hooks.json`, so deleting any of the
expected entries from `.codex-plugin/hooks/hooks.json` passed CI green.
Two-runtime iteration below covers both at no extra test cost.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
HOOKS_JSONS = [
    ROOT / "hooks" / "hooks.json",                       # Claude Code
    ROOT / ".codex-plugin" / "hooks" / "hooks.json",    # Codex
]


# Hooks that must be wired under PostToolUse:Write|Edit|MultiEdit.
# Each entry: hook-name -> (event, matcher). `event` is always
# "PostToolUse" so we keep it implicit; only the matcher matters here.
EXPECTED_POSTTOOLUSE_WRITE_EDIT = {
    "secret-scan":     "Write|Edit|MultiEdit",
    "slop-detector":   "Write|Edit|MultiEdit",
    "l4-todo-scan":    "Write|Edit|MultiEdit",
}


class TestHookWiring(unittest.TestCase):
    def _load_hooks_cfg(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")).get("hooks", {})

    def _find(self, hooks_cfg: dict, hook_name: str):
        """Return (event, matcher) where the hook is wired, or None."""
        for event, entries in hooks_cfg.items():
            for entry in entries:
                matcher = entry.get("matcher")
                for h in entry.get("hooks", []):
                    if hook_name in h.get("command", ""):
                        return (event, matcher)
        return None

    def test_posttooluse_write_edit_hooks_are_wired_in_both_runtimes(self) -> None:
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                for hook, expected_matcher in EXPECTED_POSTTOOLUSE_WRITE_EDIT.items():
                    with self.subTest(hook=hook):
                        found = self._find(cfg, hook)
                        self.assertIsNotNone(
                            found,
                            f"{hook} is not wired in {hooks_json.relative_to(ROOT)}",
                        )
                        event, actual_matcher = found
                        self.assertEqual(
                            event, "PostToolUse",
                            f"{hook} in {hooks_json.relative_to(ROOT)} is wired under "
                            f"{event!r}, expected 'PostToolUse'",
                        )
                        self.assertEqual(
                            actual_matcher, expected_matcher,
                            f"{hook} matcher in {hooks_json.relative_to(ROOT)} is "
                            f"{actual_matcher!r}, expected {expected_matcher!r}",
                        )

    def test_l4_todo_scan_hook_file_exists(self) -> None:
        """Sanity check: the hook shell file referenced in hooks.json
        actually exists under hooks/."""
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                for entry in cfg.get("PostToolUse", []):
                    if entry.get("matcher") != "Write|Edit|MultiEdit":
                        continue
                    for h in entry.get("hooks", []):
                        cmd = h.get("command", "")
                        if "l4-todo-scan" not in cmd:
                            continue
                        # Extract the path after bash ${...}
                        if "${CLAUDE_PLUGIN_ROOT}" in cmd:
                            root_var = "CLAUDE_PLUGIN_ROOT"
                        elif "${PLUGIN_ROOT}" in cmd:
                            root_var = "PLUGIN_ROOT"
                        else:
                            self.fail(f"unexpected root var in command: {cmd}")
                        # The path component is literal here (relative to plugin root).
                        path_part = cmd.split("$" + "{" + root_var + "}", 1)[1].strip()
                        self.assertTrue(
                            path_part.startswith("/hooks/"),
                            msg=f"unexpected path part: {path_part!r}",
                        )
                        hook_path = ROOT / path_part.lstrip("/")
                        self.assertTrue(
                            hook_path.exists(),
                            msg=f"hook file missing: {hook_path}",
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
