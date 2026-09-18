from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import gate_artifacts  # noqa: E402
import gates_state  # noqa: E402


class TestPlanGate(unittest.TestCase):
    def test_plan_maps_custom_gate(self) -> None:
        plan = gate_artifacts.plan_gate(Path("."), "perf-smoke")
        self.assertEqual(plan["workflow"], ".github/workflows/perf-smoke.yml")
        self.assertEqual(plan["var"], "GATES_PERF_SMOKE_ENABLED")
        self.assertEqual(plan["manifest"], ".dev-kit/gate-artifacts.json")

    def test_plan_rejects_builtin(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gate_artifacts.plan_gate(Path("."), "review")


class TestCreateGate(unittest.TestCase):
    def test_create_writes_workflow_state_manifest_and_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".gitignore").write_text(".dev-kit/\n", encoding="utf-8")
            result = gate_artifacts.create_gate(root, "perf-smoke")
            self.assertTrue(result["changed"])
            workflow = root / ".github" / "workflows" / "perf-smoke.yml"
            self.assertTrue(workflow.is_file())
            self.assertIn("vars.GATES_PERF_SMOKE_ENABLED", workflow.read_text(encoding="utf-8"))

            state = gates_state.read_state(root)
            self.assertIn("perf-smoke", state["gates"])
            self.assertEqual(state["gates"]["perf-smoke"]["workflow"], "perf-smoke.yml")

            manifest = json.loads((root / ".dev-kit" / "gate-artifacts.json").read_text(encoding="utf-8"))
            record = manifest["gates"]["perf-smoke"]
            self.assertEqual(record["managed_by"], gate_artifacts.MANAGED_BY)
            self.assertEqual(record["artifacts"][0]["path"], ".github/workflows/perf-smoke.yml")

            ignores = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn(".gjc/", ignores)
            self.assertIn(".worktrees/", ignores)
            self.assertNotIn("skills/", ignores)
            self.assertNotIn("lib/", ignores)

    def test_create_is_idempotent_for_managed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = gate_artifacts.create_gate(root, "perf-smoke")
            second = gate_artifacts.create_gate(root, "perf-smoke")
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["reason"], "already managed")

    def test_create_refuses_existing_unmanaged_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workflow = root / ".github" / "workflows" / "perf-smoke.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: custom\n", encoding="utf-8")
            with self.assertRaises(gate_artifacts.GateArtifactError):
                gate_artifacts.create_gate(root, "perf-smoke")

    def test_create_rejects_reserved_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(gates_state.ValidationError):
                gate_artifacts.create_gate(Path(td), "ci")


class TestDeleteGate(unittest.TestCase):
    def test_delete_removes_only_matching_managed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate_artifacts.create_gate(root, "perf-smoke")
            result = gate_artifacts.delete_gate(root, "perf-smoke")
            self.assertTrue(result["changed"])
            self.assertFalse((root / ".github" / "workflows" / "perf-smoke.yml").exists())
            state = gates_state.read_state(root)
            self.assertNotIn("perf-smoke", state["gates"])
            manifest = json.loads((root / ".dev-kit" / "gate-artifacts.json").read_text(encoding="utf-8"))
            self.assertNotIn("perf-smoke", manifest["gates"])

    def test_delete_refuses_unmanaged_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(gate_artifacts.GateArtifactError):
                gate_artifacts.delete_gate(Path(td), "perf-smoke")

    def test_delete_refuses_checksum_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate_artifacts.create_gate(root, "perf-smoke")
            workflow = root / ".github" / "workflows" / "perf-smoke.yml"
            workflow.write_text(workflow.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
            with self.assertRaises(gate_artifacts.GateArtifactError) as cm:
                gate_artifacts.delete_gate(root, "perf-smoke")
            self.assertIn("refusing to delete modified", str(cm.exception))
            self.assertTrue(workflow.exists())

    def test_delete_rejects_builtin_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(gates_state.ValidationError):
                gate_artifacts.delete_gate(Path(td), "review")


class TestCli(unittest.TestCase):
    def test_cli_plan_outputs_json(self) -> None:
        rc = gate_artifacts.main(["plan", "perf-smoke"])
        self.assertEqual(rc, 0)

    def test_cli_invalid_returns_2(self) -> None:
        rc = gate_artifacts.main(["plan", "ci"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
