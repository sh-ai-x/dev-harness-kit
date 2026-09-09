"""test_gate_select_v2.py — pin the gate-select v2 contract.

The v2 contract:
  * Sub-commands: show / pick / enable / disable / set / sync / init /
    install-project / install-session (default = show).
  * `pick` writes gates.json + dispatches ci-setup without `--exclude=`.
  * The legacy exclude_arg string pin in test_gate_select_review_only.py
    is REPLACED by a gates.json-write pin here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

SKILL = PROJECT_ROOT / "skills/gate-select/SKILL.md"


class TestSubCommandsExposed(unittest.TestCase):
    """The v2 sub-command table must list every entry the picker dispatches."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")

    def test_subcommand_table_present(self) -> None:
        # Each sub-command must appear (possibly with a trailing mode
        # arg like `install-session <fast|full|custom>`). The check
        # tolerates the markdown backtick + optional arg form.
        import re
        for cmd in ("show", "pick", "enable", "disable", "set", "sync", "init",
                    "install-project", "install-session"):
            pat = re.compile(rf"`{re.escape(cmd)}(?:[^`]*`)?")
            self.assertRegex(
                self.skill,
                pat,
                f"sub-command `{cmd}` missing from SKILL.md",
            )

    def test_default_is_show(self) -> None:
        # Default is `show` per MUST-21 0-arg UX.
        self.assertIn(
            "# = show",
            self.skill,
            "default sub-command (no args) must map to `show`",
        )


class TestPickRewritesGatesJson(unittest.TestCase):
    """The legacy `exclude_arg = ...` thread is replaced by gates.json writes."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")

    def test_pick_dispatches_cisetup_without_exclude(self) -> None:
        # The pick flow ends by dispatching ci-setup. It must NOT thread
        # `--exclude=` anymore — the legacy thread is gone (issue #823
        # contract moved to gates.json per issue TBD).
        # Pin by negative assertion: the literal `exclude=` should NOT
        # appear in a gate-select dispatch line.
        import re
        bad = re.search(r"Skill\([^)]*exclude=", self.skill)
        self.assertIsNone(
            bad,
            "gate-select SKILL.md still threads `exclude=` in a Skill() dispatch — "
            "the SSOT moved to gates.json (issue TBD)",
        )

    def test_pick_writes_gates_json(self) -> None:
        # The pick flow must write gates.json. Pin by string: a
        # `lib.gates_state enable|disable` invocation line is the wire.
        self.assertIn(
            "lib.gates_state enable",
            self.skill,
            "pick flow must call `python -m lib.gates_state enable` to write gates.json",
        )
        self.assertIn(
            "lib.gates_state disable",
            self.skill,
            "pick flow must call `python -m lib.gates_state disable` to enable/disable gates",
        )

    def test_pick_defines_review_only_shortcut(self) -> None:
        # The `review` pick enables review and disables the others.
        for line in (
            "review` | `enable review`, `disable security`, `disable maintenance`",
        ):
            self.assertIn(line, self.skill)


class TestSyncDocumented(unittest.TestCase):
    def test_sync_explains_gh_failure_mode(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("gh", skill)
        self.assertIn("sync", skill)
        # Mentions degraded/gh-not-authed path.
        self.assertTrue(
            "gh" in skill and "::warning::" in skill,
            "sync section must mention `::warning::` gh-degraded path",
        )


class TestInitDocumented(unittest.TestCase):
    """`init` synthesizes gates.json from current marker.runners."""

    def test_init_subcommand_present(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("`init`", skill)
        self.assertIn("synthesize", skill.lower())
        self.assertIn("marker.runners", skill)


class TestRulesPresent(unittest.TestCase):
    def test_0_arg_ux_rule(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("0-arg UX", skill)

    def test_hotl_rule(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("HOTL", skill)


class TestCiSetupCompatibility(unittest.TestCase):
    """The old `exclude=` thread is gone; install-project defers to ci-setup."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")

    def test_install_project_dispatches_cisetup(self) -> None:
        self.assertIn("install-project", self.skill)
        self.assertIn("ci-setup", self.skill)

    def test_install_session_dispatches_harness_mode(self) -> None:
        self.assertIn("install-session", self.skill)
        self.assertIn("harness-mode", self.skill)


if __name__ == "__main__":
    unittest.main()
