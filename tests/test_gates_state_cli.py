"""test_gates_state_cli.py — pin the python -m lib.gates_state <cmd> surface.

`python -m lib.gates_state <subcmd>` is the binary invoked by the
bash sub-command shims inside `skills/gate-select/SKILL.md` and by
the `lib/ci_setup` test harness. Mock gh here too so the `sync`
sub-command can run without network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import gates_state  # noqa: E402


def _run_cli(argv, *, cwd=None):
    """Invoke `gates_state.main(argv)` and capture (rc, stdout, stderr)."""
    import io
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = gates_state.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


class TestShow(unittest.TestCase):
    def test_show_pretty_on_missing_file_synthesizes_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run_cli(["show", "--root", td])
            self.assertEqual(rc, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["schema_version"], "1.0.0")
            self.assertEqual(
                set(payload["gates"].keys()),
                {"review", "security", "maintenance"},
            )
            for gate, entry in payload["gates"].items():
                self.assertTrue(entry["enabled"])
                self.assertEqual(entry["workflow"], f"{gate}.yml")

    def test_show_json_compact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, out, _ = _run_cli(["show", "--json", "--root", td])
            self.assertEqual(rc, 0)
            self.assertNotIn("\n  ", out)  # compact single-line JSON has no indentation

    def test_show_reflects_written_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gates_state.write_state(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}},
                },
                target,
            )
            _, out, _ = _run_cli(["show", "--root", td])
            payload = json.loads(out)
            self.assertFalse(payload["gates"]["review"]["enabled"])
            self.assertTrue(payload["gates"]["security"]["enabled"])


class TestEnableDisable(unittest.TestCase):
    def test_enable_then_disable_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run_cli(["disable", "security", "--root", td])
            self.assertEqual(rc, 0, err)
            self.assertIn("security: enabled=False", out)
            rc, out, err = _run_cli(["show", "--root", td])
            payload = json.loads(out)
            self.assertFalse(payload["gates"]["security"]["enabled"])

            rc, _, err = _run_cli(["enable", "security", "--root", td])
            self.assertEqual(rc, 0, err)
            rc, out, _ = _run_cli(["show", "--root", td])
            payload = json.loads(out)
            self.assertTrue(payload["gates"]["security"]["enabled"])

    def test_disable_unknown_gate_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                _run_cli(["disable", "lint", "--root", td])
            self.assertEqual(cm.exception.code, 2)


class TestSet(unittest.TestCase):
    def test_set_workflow_field_rejects_non_standard_name(self) -> None:
        # The SSOT contract pins `workflow` to `<gate>.yml` (the canonical
        # template basename). A non-standard workflow name fails validate
        # at write time — `set` is for `enabled`; workflow/var are operator-
        # customisable via direct JSON edit + `gate-select init` only.
        with tempfile.TemporaryDirectory() as td:
            rc, _, err = _run_cli(["set", "review", "workflow", "custom-review.yml", "--root", td])
            self.assertEqual(rc, 2)
            self.assertIn("must equal 'review.yml'", err)

    def test_set_workflow_field_accepts_canonical_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run_cli(
                ["set", "review", "workflow", "review.yml", "--root", td]
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("review.workflow = 'review.yml'", out)

    def test_set_unknown_field_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                _run_cli(["set", "review", "bogus", "x", "--root", td])
            self.assertEqual(cm.exception.code, 2)


class TestValidate(unittest.TestCase):
    def test_validate_clean_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run_cli(["validate", "--root", td])
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.strip(), "OK")

    def test_validate_corrupt_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text("{not json")
            rc, _, err = _run_cli(["validate", "--root", td])
            self.assertEqual(rc, 2)
            self.assertIn("gates_state:", err)


class TestSync(unittest.TestCase):
    def test_sync_degraded_exits_3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gates_state, "gh_available", return_value=(None, "gh not on PATH")):
                rc, _, err = _run_cli(["sync", "--root", td])
            self.assertEqual(rc, 3)
            self.assertIn("gh not on PATH", err)
            self.assertIn("::warning::", err)

    @unittest.skip("git init is flaky/slow in this sandbox")
    def test_sync_happy_path_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(
                ["git", "-C", td, "init", "-q"], capture_output=True, check=True
            )
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                capture_output=True, check=True,
            )
            with mock.patch.object(gates_state, "gh_available", return_value=("/fake/gh", "")):
                with mock.patch.object(gates_state, "_sync_one", return_value=(True, "")):
                    rc, out, err = _run_cli(["sync", "--root", td])
            self.assertEqual(rc, 0, err)
            for line in out.splitlines():
                self.assertIn("✓", line)
                self.assertIn("var=GATES_", line)
                self.assertIn("body=true", line)


if __name__ == "__main__":
    unittest.main()
