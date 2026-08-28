#!/usr/bin/env python3
"""test_settings_permissions.py — regression for `.claude/settings.json` allow rules.

Claude Code's permission matcher only matches `Edit(path)` rules for
file-editing tools (Edit/Write/MultiEdit all share the same allow-rule
matcher, keyed on `Edit`). A `Write(path)` rule is silently dropped and
surfaces as a "Permission allow rule: Write(.worktrees/**) is not
matched by file permission checks" warning on every Write call.

This test pins the structural contract so the bad rule cannot be
re-introduced (regression for PR #228, reverted by this fix).

  T1: `.claude/settings.json` parses as valid JSON.
  T2: `permissions.allow` exists and is a list.
  T3: no rule uses the `Write(` prefix (silent no-op; Edit covers Write).
  T4: `Edit(.worktrees/**)` is present (the rule that actually covers
      worktree writes for /dev-kit:build sub-agents).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
CODEX_HOOKS = REPO_ROOT / ".codex" / "hooks.json"


def _hook_command(entry: dict) -> str:
    hooks = entry.get("hooks", [])
    return hooks[0].get("command", "") if hooks else ""


class SettingsPermissionsContract(unittest.TestCase):
    def test_settings_json_parses(self) -> None:
        with SETTINGS.open() as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)

    def test_allow_list_exists_and_is_list(self) -> None:
        with SETTINGS.open() as f:
            data = json.load(f)
        allow = data.get("permissions", {}).get("allow")
        self.assertIsInstance(allow, list, "permissions.allow must be a list")

    def test_no_write_prefixed_rule(self) -> None:
        """Write(path) is silently dropped by the permission matcher.

        The matcher only recognizes Edit(path) for file-editing tools
        (Edit/Write/MultiEdit). Listing Write(...) here produces a noisy
        stderr warning on every Write call but grants nothing.
        """
        with SETTINGS.open() as f:
            data = json.load(f)
        allow = data.get("permissions", {}).get("allow", [])
        write_rules = [r for r in allow if r.startswith("Write(")]
        self.assertEqual(
            write_rules,
            [],
            f"Write(...)-prefixed allow rules are silent no-ops; remove them "
            f"(Edit(...) covers Edit/Write/MultiEdit). Found: {write_rules}",
        )

    def test_edit_worktrees_rule_present(self) -> None:
        """The pre-allow for worktree writes must remain as Edit(.worktrees/**).

        /dev-kit:build spawns sub-agents that Edit/Write inside .worktrees/;
        without this rule, every tool call prompts the user.
        """
        with SETTINGS.open() as f:
            data = json.load(f)
        allow = data.get("permissions", {}).get("allow", [])
        self.assertIn(
            "Edit(.worktrees/**)",
            allow,
            "Edit(.worktrees/**) must remain in permissions.allow so "
            "/dev-kit:build sub-agents can write to worktrees without prompting.",
        )

    def test_trace_hook_precedes_managed_log_hook(self) -> None:
        """Pin the trace-first lifecycle ordering in committed manifests."""
        with SETTINGS.open() as f:
            claude_hooks = json.load(f)["hooks"]
        with CODEX_HOOKS.open() as f:
            codex_hooks = json.load(f)["hooks"]

        for manifest, events in (
            ("Claude", claude_hooks),
            ("Codex", codex_hooks),
        ):
            event_names = ("SessionEnd", "Stop") if manifest == "Claude" else ("Stop",)
            for event_name in event_names:
                entries = events[event_name]
                self.assertGreaterEqual(
                    len(entries), 2, f"{manifest} {event_name} needs both lifecycle hooks"
                )
                self.assertIn(
                    "trace-session-end.sh",
                    _hook_command(entries[0]),
                    f"{manifest} {event_name} must run trace-session-end first",
                )
                self.assertTrue(
                    entries[1].get("_loghooks_managed") is True,
                    f"{manifest} {event_name} managed marker must stay on save_log",
                )
                self.assertIn("save_log.py", _hook_command(entries[1]))


if __name__ == "__main__":
    unittest.main()
