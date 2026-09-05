#!/usr/bin/env python3
"""Tests for lib/guard_mode_state (session-scoped tdd-guard / worktree-guard toggle).

Covers:
- read/write round-trip for the session state file
- missing/corrupt/invalid file defaults to both guards "on"
- `set` overrides one guard without touching the other
- `reset` forces every guard back to "on"
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

    def test_missing_file_defaults_to_all_on(self):
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "on", "worktree_guard": "on"})

    def test_corrupt_file_defaults_to_all_on(self):
        path = gms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "on", "worktree_guard": "on"})

    def test_invalid_value_in_file_defaults_that_guard_to_on(self):
        path = gms._state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tdd_guard": "bogus"}), encoding="utf-8")
        state = gms.read_state(self.root)
        self.assertEqual(state["tdd_guard"], "on")

    def test_write_state_round_trips_one_guard(self):
        gms.write_state({"tdd_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "off", "worktree_guard": "on"})

    def test_write_state_does_not_disturb_other_guard(self):
        gms.write_state({"tdd_guard": "off"}, root=self.root)
        gms.write_state({"worktree_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "off", "worktree_guard": "off"})

    def test_write_state_drops_unknown_guard_key(self):
        gms.write_state({"git_guard": "off"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "on", "worktree_guard": "on"})

    def test_write_state_drops_non_on_off_value(self):
        gms.write_state({"tdd_guard": "maybe"}, root=self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state["tdd_guard"], "on")

    def test_reset_state_forces_all_on(self):
        gms.write_state({"tdd_guard": "off", "worktree_guard": "off"}, root=self.root)
        gms.reset_state(self.root)
        state = gms.read_state(self.root)
        self.assertEqual(state, {"tdd_guard": "on", "worktree_guard": "on"})


class TestResolvedGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolved_guard_default_on(self):
        self.assertEqual(gms.resolved_guard("tdd_guard", self.root), "on")
        self.assertEqual(gms.resolved_guard("worktree_guard", self.root), "on")

    def test_resolved_guard_reflects_off(self):
        gms.write_state({"worktree_guard": "off"}, root=self.root)
        self.assertEqual(gms.resolved_guard("worktree_guard", self.root), "off")
        self.assertEqual(gms.resolved_guard("tdd_guard", self.root), "on")

    def test_resolved_guard_unknown_name_fails_closed_to_on(self):
        self.assertEqual(gms.resolved_guard("git_guard", self.root), "on")


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

    def test_cli_get_defaults_to_on(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gms.main(["get", "tdd_guard"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "on")

    def test_cli_set_then_get_reflects_off(self):
        import contextlib
        import io

        rc = gms.main(["set", "worktree_guard", "off"])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["get", "worktree_guard"])
        self.assertEqual(buf.getvalue().strip(), "off")

    def test_cli_reset_restores_on(self):
        import contextlib
        import io

        gms.main(["set", "tdd_guard", "off"])
        rc = gms.main(["reset"])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["get", "tdd_guard"])
        self.assertEqual(buf.getvalue().strip(), "on")

    def test_cli_show_json_contains_both_guards(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gms.main(["show", "--json"])
        parsed = json.loads(buf.getvalue())
        self.assertIn("tdd_guard", parsed)
        self.assertIn("worktree_guard", parsed)
        self.assertEqual(parsed["tdd_guard"]["value"], "on")


if __name__ == "__main__":
    unittest.main()
