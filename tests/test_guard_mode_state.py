#!/usr/bin/env python3
"""Tests for lib/guard_mode_state and the scoped guard policy.

Covers:
- read/write round-trip for the session state file
- missing/corrupt/invalid file defaults to repository guards "off"
- `set` overrides one guard without touching the other
- `reset` applies the all-off default or explicit policy
- unknown guard name resolves "on" (fail closed)
- write_state() drops unknown guard keys and non on/off values
- CLI: get / set / reset / show
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import guard_mode_state as gms  # noqa: E402


class TestReadWriteRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_defaults_to_repository_guards_off(self):
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_corrupt_file_defaults_to_repository_guards_off(self):
        path = gms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_invalid_value_in_file_defaults_that_guard_to_on(self):
        path = gms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tdd_guard": "bogus"}), encoding="utf-8")
        state = gms.read_state(self.root)
        self.assertEqual(state["tdd_guard"], "off")

    def test_write_state_round_trips_one_guard(self):
        gms.write_state({"tdd_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_write_state_does_not_disturb_other_guard(self):
        gms.write_state({"tdd_guard": "off"}, root=self.root)
        gms.write_state({"worktree_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_write_state_drops_unknown_guard_key(self):
        gms.write_state({"unknown_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_write_state_drops_non_on_off_value(self):
        gms.write_state({"tdd_guard": "maybe"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state["tdd_guard"], "off")

    def test_reset_state_applies_off_policy(self):
        gms.write_state({"tdd_guard": "on", "worktree_guard": "on", "git_guard": "on"}, root=self.root)
        gms.reset_state(self.root)
        state = gms.read_state(self.root)
        self.assertEqual(
            state,
            {"tdd_guard": "off", "worktree_guard": "off", "git_guard": "off",
             "push_confirm": "on", "fork_pr_confirm": "off", "policy": "off",
             "policy_source": "default", "branch_class": "unknown"},
        )

    def test_reset_state_records_explicit_policy_metadata(self):
        state = gms.reset_state(self.root, policy="on", policy_source="project",
                                branch_class="main")
        self.assertEqual(state["policy"], "on")
        self.assertEqual(state["policy_source"], "project")
        self.assertEqual(state["branch_class"], "main")
        for guard in gms.POLICY_GUARDS:
            self.assertEqual(state[guard], "on")

    def test_fork_pr_confirm_round_trips(self):
        gms.write_state({"fork_pr_confirm": "on"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state["fork_pr_confirm"], "on")
        self.assertEqual(gms.resolved_guard("fork_pr_confirm", self.root), "on")

    def test_fork_pr_confirm_defaults_off(self):
        # Opt-in guard → missing file + reset both yield "off".
        self.assertEqual(gms.resolved_guard("fork_pr_confirm", self.root), "off")
        gms.write_state({"fork_pr_confirm": "on"}, root=self.root)
        gms.reset_state(self.root)
        self.assertEqual(gms.resolved_guard("fork_pr_confirm", self.root), "off")


class TestResolvedGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolved_guard_default_off(self):
        self.assertEqual(gms.resolved_guard("tdd_guard", self.root), "off")
        self.assertEqual(gms.resolved_guard("worktree_guard", self.root), "off")
        self.assertEqual(gms.resolved_guard("git_guard", self.root), "off")

    def test_resolved_guard_reflects_off(self):
        gms.write_state({"worktree_guard": "off"}, root=self.root)
        self.assertEqual(gms.resolved_guard("worktree_guard", self.root), "off")
        self.assertEqual(gms.resolved_guard("tdd_guard", self.root), "off")

    def test_resolved_guard_unknown_name_fails_closed_to_on(self):
        self.assertEqual(gms.resolved_guard("unknown_guard", self.root), "on")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_cwd = Path.cwd()
        import os

        os.chdir(self.root)

    def tearDown(self):
        import os

        os.chdir(self._orig_cwd)
        self.tmp.cleanup()

    def _run(self, argv, capsys):
        rc = gms.main(argv)
        out = capsys.readouterr()
        return rc, out

    def test_cli_get_defaults_to_off(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gms.main(["get", "tdd_guard"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "off")

    def test_cli_set_then_get_reflects_off(self):
        import contextlib
        import io

        rc = gms.main(["set", "worktree_guard", "off"])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["get", "worktree_guard"])
        self.assertEqual(buf.getvalue().strip(), "off")

    def test_cli_reset_restores_off(self):
        import contextlib
        import io

        gms.main(["set", "tdd_guard", "off"])
        rc = gms.main(["reset"])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["get", "tdd_guard"])
        self.assertEqual(buf.getvalue().strip(), "off")

    def test_cli_show_json_contains_every_guard(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["show", "--json"])
        parsed = json.loads(buf.getvalue())
        for guard in gms.GUARDS:
            with self.subTest(guard=guard):
                self.assertIn(guard, parsed)
                self.assertIn("value", parsed[guard])
                self.assertIn("description", parsed[guard])
                # Repository guards default off; push confirmation remains
                # an independent ask-tier surface.
                expected = "on" if guard == "push_confirm" else "off"
                self.assertEqual(parsed[guard]["value"], expected)


if __name__ == "__main__":
    unittest.main()
