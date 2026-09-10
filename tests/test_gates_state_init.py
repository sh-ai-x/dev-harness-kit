"""Pin the `init` sub-command contract (issue #834 reviewer finding)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
import gates_state  # noqa: E402


class TestInitSubCommand(unittest.TestCase):
    def _run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = gates_state.main(list(argv))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, out.getvalue(), err.getvalue()

    def test_init_synthesizes_from_marker_runners(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir(parents=True)
            (target / ".dev-kit" / "ci-config.json").write_text(json.dumps({
                "schema_version": "1.0.0",
                "runners": ["ci.yml", "auto-fix-pr.yml", "review.yml", "maintenance.yml"],
            }))
            rc, out, _ = self._run_cli("init", "--root", td)
            self.assertEqual(rc, 0)
            gates_json = json.loads((target / ".dev-kit" / "gates.json").read_text())
            self.assertTrue(gates_json["gates"]["review"]["enabled"])
            self.assertFalse(gates_json["gates"]["security"]["enabled"])
            self.assertTrue(gates_json["gates"]["maintenance"]["enabled"])
            # Output is the persisted payload (round-trippable).
            self.assertEqual(json.loads(out)["gates"], gates_json["gates"])

    def test_init_no_marker_writes_all_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc, _, _ = self._run_cli("init", "--root", td)
            self.assertEqual(rc, 0)
            state = gates_state.read_state(Path(td))
            self.assertTrue(all(state["gates"][k]["enabled"] for k in state["gates"]))

    def test_init_corrupt_marker_falls_back_to_all_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir(parents=True)
            (target / ".dev-kit" / "ci-config.json").write_text("{not json")
            rc, _, _ = self._run_cli("init", "--root", td)
            self.assertEqual(rc, 0)
            state = gates_state.read_state(Path(td))
            self.assertTrue(all(state["gates"][k]["enabled"] for k in state["gates"]))


if __name__ == "__main__":
    unittest.main()
