"""test_ci_setup_gates.py — pin ci_setup ↔ gates.json precedence contract.

issue #834 acceptance:
  - gates.json absent + ci-setup (no --exclude) → all 5 runners; marker
    `gates_source = "ci-setup"`.
  - gates.json present + disable security → marker records only review
    + maintenance; `gates_source = "gates.json"`.
  - gates.json present + ci-setup --exclude security.yml → exclude is
    logged via `::notice::` and IGNORED; install set is gates-derived.
  - gates.json absent + ci-setup --exclude security.yml → legacy path:
    review.yml only; marker matches.
  - corrupt gates.json → install proceeds with legacy exclude semantics;
    `::warning::` emitted.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import ci_setup  # noqa: E402
import gates_state  # noqa: E402


def _install(target: Path, *, exclude=None, gates_json=None):
    """Helper: write optional gates.json, run install_ci_config, return report."""
    if gates_json is not None:
        (target / ".dev-kit").mkdir(parents=True, exist_ok=True)
        (target / ".dev-kit" / "gates.json").write_text(json.dumps(gates_json))
    kwargs = {"force": True}
    if exclude is not None:
        kwargs["exclude"] = exclude
    return ci_setup.install_ci_config(target, **kwargs)


class TestResolveRunnersFromGates(unittest.TestCase):
    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ci_setup._resolve_runners_from_gates(Path(td)))

    def test_present_returns_runners(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gates_state.write_state(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}},
                },
                target,
            )
            self.assertEqual(
                ci_setup._resolve_runners_from_gates(target),
                ["security.yml", "maintenance.yml"],
            )

    def test_corrupt_returns_none_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir(parents=True)
            (target / ".dev-kit" / "gates.json").write_text("{not json")
            import io
            from contextlib import redirect_stderr
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = ci_setup._resolve_runners_from_gates(target)
            self.assertIsNone(result)
            self.assertIn("::warning::gates.json", buf.getvalue())


class TestInstallWithoutGatesJson(unittest.TestCase):
    """No gates.json → legacy contract: `--exclude` drives runners."""

    def test_no_exclude_all_five_runners(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = _install(target)
            self.assertEqual(r.errors, [])
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertEqual(
                set(data["runners"]),
                {"ci.yml", "auto-fix-pr.yml", "review.yml", "security.yml", "maintenance.yml"},
            )
            self.assertEqual(data["gates_source"], "ci-setup")
            self.assertEqual(data["gates_path"], ".dev-kit/gates.json")

    def test_exclude_security_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = _install(target, exclude=frozenset({"security.yml"}))
            self.assertEqual(r.errors, [])
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertEqual(
                set(data["runners"]),
                {"ci.yml", "auto-fix-pr.yml", "review.yml", "maintenance.yml"},
            )
            # security.yml NOT installed.
            self.assertFalse((target / ".github" / "workflows" / "security.yml").is_file())
            self.assertEqual(data["gates_source"], "ci-setup")


class TestInstallWithGatesJson(unittest.TestCase):
    """gates.json present → its enabled flags drive runners + ignore --exclude."""

    def test_all_enabled_default_install(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = _install(target, gates_json={
                "schema_version": "1.0.0",
                "gates": {},
            })
            self.assertEqual(r.errors, [])
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertEqual(
                set(data["runners"]),
                {"ci.yml", "auto-fix-pr.yml", "review.yml", "security.yml", "maintenance.yml"},
            )
            self.assertEqual(data["gates_source"], "gates.json")

    def test_disable_security_drops_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            r = _install(target, gates_json={
                "schema_version": "1.0.0",
                "gates": {
                    "review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
                    "security": {"enabled": False, "workflow": "security.yml", "var": "GATES_SECURITY_ENABLED"},
                    "maintenance": {"enabled": True, "workflow": "maintenance.yml", "var": "GATES_MAINTENANCE_ENABLED"},
                },
            })
            self.assertEqual(r.errors, [])
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertNotIn("security.yml", data["runners"])
            self.assertIn("review.yml", data["runners"])
            self.assertIn("maintenance.yml", data["runners"])
            # On-disk: security.yml absent.
            self.assertFalse((target / ".github" / "workflows" / "security.yml").is_file())
            self.assertTrue((target / ".github" / "workflows" / "review.yml").is_file())
            self.assertEqual(data["gates_source"], "gates.json")

    def test_exclude_is_ignored_when_gates_present(self) -> None:
        """--exclude=security.yml + gates.json all-enabled: notice logged, exclude ignored."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            import io
            from contextlib import redirect_stderr
            buf = io.StringIO()
            with redirect_stderr(buf):
                r = _install(
                    target,
                    exclude=frozenset({"security.yml"}),
                    gates_json={
                        "schema_version": "1.0.0",
                        "gates": {},
                    },
                )
            self.assertEqual(r.errors, [])
            self.assertIn("::notice::gates.json present", buf.getvalue())
            # All 5 runners — exclude was ignored.
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertEqual(
                set(data["runners"]),
                {"ci.yml", "auto-fix-pr.yml", "review.yml", "security.yml", "maintenance.yml"},
            )
            self.assertTrue((target / ".github" / "workflows" / "security.yml").is_file())


class TestMarkerShapeWithGates(unittest.TestCase):
    def test_gates_source_field_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            _install(target, gates_json={"schema_version": "1.0.0", "gates": {}})
            data = json.loads((target / ".dev-kit" / "ci-config.json").read_text())
            self.assertEqual(data["gates_source"], "gates.json")
            self.assertEqual(data["gates_path"], ".dev-kit/gates.json")


if __name__ == "__main__":
    unittest.main()
