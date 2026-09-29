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
}

# Channel-level prompt-injection guards (iron-law L9). Wired under
# PostToolUse:Agent (sub-agent output) and PostToolUse:WebFetch (fetched
# body). Fail-closed is false (advisory-only) because PostToolUse cannot
# block a tool that already executed — the strict signal is via
# INJECTION_STRICT=1 (env-controlled).
EXPECTED_INJECTION_GUARDS = {
    "injection-content-guard:Agent":    "Agent",
    "injection-content-guard:WebFetch": "WebFetch",
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

    def test_injection_content_guard_wired_for_agent_and_webfetch(self) -> None:
        """injection-content-guard.sh (iron-law L9) must fire on both
        sub-agent output channels: PostToolUse:Agent (sub-agent
        responses) and PostToolUse:WebFetch (fetched body)."""
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                # Flatten: list of (matcher, hook_command) per PostToolUse entry.
                flat: list[tuple[str, str]] = []
                for entry in cfg.get("PostToolUse", []):
                    matcher = entry.get("matcher", "")
                    for h in entry.get("hooks", []):
                        flat.append((matcher, h.get("command", "")))
                for key, expected_matcher in EXPECTED_INJECTION_GUARDS.items():
                    hook_name, channel = key.split(":", 1)
                    with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0], channel=channel):
                        matches = [
                            cmd for m, cmd in flat
                            if hook_name in cmd and m == expected_matcher
                        ]
                        self.assertTrue(
                            matches,
                            f"{hook_name} not wired under PostToolUse:{expected_matcher} "
                            f"in {hooks_json.relative_to(ROOT)}; got matchers={[m for m, _ in flat]}",
                        )

    def test_posttooluse_agent_injection_guard_only(self) -> None:
        """Only `injection-content-guard.sh` is wired under PostToolUse:Agent
        after the l4-todo-scan / sub-agent-handoff prune. Lint check:
        make sure no stale sub-agent-handoff wiring crept back in."""
        for hooks_json in HOOKS_JSONS:
            if not hooks_json.exists():
                self.skipTest(f"{hooks_json.relative_to(ROOT)} missing")
            with self.subTest(runtime=hooks_json.relative_to(ROOT).parts[0]):
                cfg = self._load_hooks_cfg(hooks_json)
                flat = [
                    (entry.get("matcher", ""), h.get("command", ""))
                    for entry in cfg.get("PostToolUse", [])
                    for h in entry.get("hooks", [])
                ]
                for hook_name in ("sub-agent-handoff", "l4-todo-scan"):
                    leaks = [cmd for matcher, cmd in flat if hook_name in cmd]
                    self.assertEqual(
                        leaks, [],
                        f"{hook_name} should be pruned but appears in "
                        f"{hooks_json.relative_to(ROOT)}: {leaks}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
