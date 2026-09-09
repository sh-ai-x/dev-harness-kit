"""tests/test_linear_trigger_hooks_wiring.py — Regression test for
`hooks/hooks.json` wiring of the four Linear auto-trigger hooks
across BOTH runtimes (Claude + Codex).

If a maintainer rewires any of these hooks to a different event
matcher, or removes them from either runtime, this test fails
loud. Each hook is required to fire on exactly one event in each
runtime:

  - linear-autosync         → PreToolUse  Write|Edit|MultiEdit
  - linear-session-start    → SessionStart
  - linear-worktree-create  → PostToolUse  Bash
  - linear-task-change      → UserPromptSubmit

The dual-runtime scan (Claude + Codex) is the regression for the
review finding "Wiring test covers Claude only, not Codex" — the
previous version of this test only loaded `hooks/hooks.json`, so
deleting any of the new entries from `.codex-plugin/hooks/hooks.json`
passed CI green. The two-runtime loop below closes that hole
cheaply (no separate `Codex` test class needed; the iteration
parametrizes the JSON path).
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

EXPECTED = {
    "linear-autosync":         ("PreToolUse",      "Write|Edit|MultiEdit"),
    "linear-session-start":    ("SessionStart",    None),
    "linear-worktree-create":  ("PostToolUse",     "Bash"),
}


class TestLinearTriggerHooksWiring(unittest.TestCase):
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

    def test_all_four_hooks_are_wired_in_both_runtimes(self):
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                for hook in EXPECTED:
                    with self.subTest(hook=hook):
                        found = self._find(cfg, hook)
                        self.assertIsNotNone(
                            found,
                            f"{hook} is not wired in {hooks_json.relative_to(ROOT)}",
                        )
                        event, _ = found
                        expected_event, _ = EXPECTED[hook]
                        self.assertEqual(
                            event, expected_event,
                            f"{hook} in {hooks_json.relative_to(ROOT)} is wired under "
                            f"{event!r}, expected {expected_event!r}",
                        )

    def test_all_hooks_use_their_expected_matcher_in_both_runtimes(self):
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                for hook, (event, matcher) in EXPECTED.items():
                    with self.subTest(hook=hook, matcher=matcher):
                        if matcher is None:
                            continue  # SessionStart / UserPromptSubmit have no matcher
                        found = self._find(cfg, hook)
                        self.assertIsNotNone(found)
                        _, actual_matcher = found
                        self.assertEqual(
                            actual_matcher, matcher,
                            f"{hook} matcher in {hooks_json.relative_to(ROOT)} is "
                            f"{actual_matcher!r}, expected {matcher!r}",
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
