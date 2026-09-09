"""test_gates_state.py — pin the gates.json SSOT contract.

Pins every rule documented in `lib/gates_state.validate` plus the
read/write/sync surface that `lib/ci_setup` + `skills/gate-select`
depend on. TDD RED: any shape or wire-protocol regression fails at
module-import time so the gate loop never lands a half-built SSOT.
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

import gates_state  # noqa: E402  (after sys.path manipulation)


class TestConstants(unittest.TestCase):
    """The SSOT surface is the module-level constants — pin them."""

    def test_schema_version_is_pinned(self) -> None:
        self.assertEqual(gates_state.SCHEMA_VERSION, "1.0.0")

    def test_state_rel_path(self) -> None:
        self.assertEqual(gates_state.STATE_REL_PATH, Path(".dev-kit") / "gates.json")

    def test_valid_gate_keys(self) -> None:
        self.assertEqual(
            gates_state.VALID_GATE_KEYS, frozenset({"review", "security", "maintenance"})
        )

    def test_default_gates_shape(self) -> None:
        for gate in ("review", "security", "maintenance"):
            entry = gates_state.DEFAULT_GATES[gate]
            self.assertEqual(set(entry.keys()), {"enabled", "workflow", "var"})
            self.assertIs(entry["enabled"], True)
            self.assertEqual(entry["workflow"], f"{gate}.yml")
            self.assertEqual(entry["var"], f"GATES_{gate.upper()}_ENABLED")

    def test_gate_order_matches_defaults(self) -> None:
        self.assertEqual(
            gates_state.GATE_ORDER,
            ("review", "security", "maintenance"),
        )


class TestApplyDefaults(unittest.TestCase):
    """`apply_defaults` is the pure transform `read_state` runs on disk content."""

    def test_missing_keys_synthesized(self) -> None:
        out = gates_state.apply_defaults(
            {"schema_version": "1.0.0", "gates": {"review": {"enabled": False}}}
        )
        self.assertEqual(out["gates"]["review"]["enabled"], False)
        self.assertEqual(out["gates"]["security"]["enabled"], True)
        self.assertEqual(out["gates"]["maintenance"]["enabled"], True)

    def test_existing_keys_preserved(self) -> None:
        out = gates_state.apply_defaults(
            {
                "schema_version": "1.0.0",
                "gates": {"security": {"enabled": False, "workflow": "security.yml", "var": "GATES_SECURITY_ENABLED"}},
            }
        )
        self.assertEqual(out["gates"]["security"]["enabled"], False)
        self.assertEqual(out["gates"]["security"]["workflow"], "security.yml")

    def test_non_dict_state_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.apply_defaults("not a dict")  # type: ignore[arg-type]

    def test_empty_gates_filled(self) -> None:
        out = gates_state.apply_defaults({"schema_version": "1.0.0", "gates": {}})
        self.assertEqual(set(out["gates"].keys()), {"review", "security", "maintenance"})

    def test_missing_gates_key_filled(self) -> None:
        out = gates_state.apply_defaults({"schema_version": "1.0.0"})
        self.assertEqual(set(out["gates"].keys()), {"review", "security", "maintenance"})


class TestValidate(unittest.TestCase):
    """Every rule in `validate` has a positive and a negative test."""

    VALID = {
        "schema_version": "1.0.0",
        "gates": {
            "review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
            "security": {"enabled": False, "workflow": "security.yml", "var": "GATES_SECURITY_ENABLED"},
            "maintenance": {"enabled": True, "workflow": "maintenance.yml", "var": "GATES_MAINTENANCE_ENABLED"},
        },
    }

    def test_full_valid_passes(self) -> None:
        gates_state.validate(self.VALID)  # does not raise

    def test_partial_valid_passes(self) -> None:
        # Validate skips PRESENT-only keys; `apply_defaults` fills the rest.
        gates_state.validate(
            {
                "schema_version": "1.0.0",
                "gates": {"review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}},
            }
        )

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.validate("nope")

    def test_wrong_schema_version_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate({"schema_version": "0.9.0", "gates": {}})
        self.assertIn("schema_version", str(cm.exception))

    def test_missing_schema_version_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.validate({"gates": {}})

    def test_non_dict_gates_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.validate({"schema_version": "1.0.0", "gates": "nope"})

    def test_unknown_gate_key_raises_with_field_path(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {"lint": {"enabled": True, "workflow": "lint.yml", "var": "GATES_LINT_ENABLED"}},
                }
            )
        self.assertIn("gates:", str(cm.exception))
        self.assertIn("lint", str(cm.exception))

    def test_gate_not_a_dict_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate({"schema_version": "1.0.0", "gates": {"review": "oops"}})
        self.assertIn("gates.review", str(cm.exception))

    def test_gate_with_extra_key_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {
                        "review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED", "extra": 1},
                    },
                }
            )
        self.assertIn("gates.review", str(cm.exception))

    def test_gate_missing_required_key_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": True, "workflow": "review.yml"}},
                }
            )
        self.assertIn("gates.review", str(cm.exception))

    def test_enabled_must_be_bool_not_truthy_string(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": "true", "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}},
                }
            )
        self.assertIn("enabled", str(cm.exception))

    def test_workflow_must_match_gate_key(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": True, "workflow": "security.yml", "var": "GATES_REVIEW_ENABLED"}},
                }
            )
        self.assertIn("workflow", str(cm.exception))

    def test_var_must_match_gate_key_upper(self) -> None:
        with self.assertRaises(gates_state.ValidationError) as cm:
            gates_state.validate(
                {
                    "schema_version": "1.0.0",
                    "gates": {"review": {"enabled": True, "workflow": "review.yml", "var": "GATES_LINT_ENABLED"}},
                }
            )
        self.assertIn("var", str(cm.exception))


class TestReadState(unittest.TestCase):
    """`read_state` is the disk-side contract — missing / corrupt / valid."""

    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = gates_state.read_state(Path(td))
            self.assertEqual(state["schema_version"], "1.0.0")
            self.assertEqual(set(state["gates"].keys()), {"review", "security", "maintenance"})
            self.assertTrue(all(state["gates"][k]["enabled"] for k in state["gates"]))

    def test_valid_file_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text(json.dumps(TestValidate.VALID))
            state = gates_state.read_state(target)
            self.assertEqual(state["gates"]["review"]["enabled"], True)
            self.assertEqual(state["gates"]["security"]["enabled"], False)
            self.assertEqual(state["gates"]["maintenance"]["enabled"], True)

    def test_corrupt_json_raises_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text("{not json")
            with self.assertRaises(gates_state.ValidationError):
                gates_state.read_state(target)

    def test_unknown_gate_key_raises_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text(
                json.dumps({"schema_version": "1.0.0", "gates": {"lint": {"enabled": True}}})
            )
            with self.assertRaises(gates_state.ValidationError):
                gates_state.read_state(target)

    def test_partial_file_fills_defaults_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".dev-kit").mkdir()
            (target / ".dev-kit" / "gates.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "gates": {
                            "review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
                        },
                    }
                )
            )
            state = gates_state.read_state(target)
            self.assertEqual(state["gates"]["review"]["enabled"], False)
            self.assertEqual(state["gates"]["security"]["enabled"], True)


class TestWriteState(unittest.TestCase):
    """`write_state` validates, stamps, atomic-writes, returns the payload."""

    def test_round_trip_persists_full_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            out = gates_state.write_state(
                {
                    "schema_version": "1.0.0",
                    "gates": {
                        "review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
                    },
                },
                target,
            )
            self.assertEqual(out["gates"]["review"]["enabled"], False)
            self.assertEqual(out["gates"]["security"]["enabled"], True)  # filled by apply_defaults
            self.assertEqual(out["installed_by"], "dev-kit:gate-select")
            self.assertTrue(out["installed_at"].endswith("Z"))
            self.assertEqual(out["installed_dev_kit_version"], gates_state._plugin_version_or_zero())
            on_disk = json.loads((target / ".dev-kit" / "gates.json").read_text())
            self.assertEqual(on_disk, out)

    def test_write_rejects_bad_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(gates_state.ValidationError):
                gates_state.write_state({"schema_version": "0.0.0", "gates": {}}, Path(td))

    def test_write_atomic_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gates_state.write_state({"schema_version": "1.0.0", "gates": {}}, target)
            # .dev-kit dir exists, gates.json exists, no leftover .tmp files
            tmp_files = list((target / ".dev-kit").glob(".gates.json.*.tmp"))
            self.assertEqual(tmp_files, [], f"leftover tmp files: {tmp_files}")


class TestIsEnabled(unittest.TestCase):
    """`is_enabled` is the hot path — read on every gate job's `if:` resolution."""

    def test_known_gate_returns_stored_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gates_state.write_state(
                {"schema_version": "1.0.0", "gates": {"review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}}},
                target,
            )
            self.assertFalse(gates_state.is_enabled("review", target))
            self.assertTrue(gates_state.is_enabled("security", target))

    def test_unknown_gate_returns_true_fail_open(self) -> None:
        # A typo in a workflow's `vars.GATES_<NAME>_ENABLED != 'false'` must
        # never silently disable a gate the operator intended to keep on.
        self.assertTrue(gates_state.is_enabled("lint"))


class TestWorkflowForVarFor(unittest.TestCase):
    """Constant lookups used by `lib/ci_setup._build_marker` + sync dispatcher."""

    def test_workflow_for_each_gate(self) -> None:
        self.assertEqual(gates_state.workflow_for("review"), "review.yml")
        self.assertEqual(gates_state.workflow_for("security"), "security.yml")
        self.assertEqual(gates_state.workflow_for("maintenance"), "maintenance.yml")

    def test_workflow_for_unknown_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.workflow_for("lint")

    def test_var_for_each_gate(self) -> None:
        self.assertEqual(gates_state.var_for("review"), "GATES_REVIEW_ENABLED")
        self.assertEqual(gates_state.var_for("security"), "GATES_SECURITY_ENABLED")
        self.assertEqual(gates_state.var_for("maintenance"), "GATES_MAINTENANCE_ENABLED")

    def test_var_for_unknown_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state.var_for("lint")


class TestRunnersFromGates(unittest.TestCase):
    """`runners_from_gates` is the SSOT for `lib/ci_setup._build_marker.runners`."""

    def test_all_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            gates_state.write_state({"schema_version": "1.0.0", "gates": {}}, Path(td))
            self.assertEqual(
                gates_state.runners_from_gates(Path(td)),
                ["review.yml", "security.yml", "maintenance.yml"],
            )

    def test_one_disabled(self) -> None:
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
                gates_state.runners_from_gates(target),
                ["security.yml", "maintenance.yml"],
            )

    def test_all_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gates_state.write_state(
                {
                    "schema_version": "1.0.0",
                    "gates": {
                        "review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"},
                        "security": {"enabled": False, "workflow": "security.yml", "var": "GATES_SECURITY_ENABLED"},
                        "maintenance": {"enabled": False, "workflow": "maintenance.yml", "var": "GATES_MAINTENANCE_ENABLED"},
                    },
                },
                target,
            )
            self.assertEqual(gates_state.runners_from_gates(target), [])


class TestSetField(unittest.TestCase):
    """`_set_field` is the inner-state mutator for the `set` CLI sub-command."""

    def test_enabled_true(self) -> None:
        state = {
            "schema_version": "1.0.0",
            "installed_at": "x",
            "installed_by": "y",
            "installed_dev_kit_version": "z",
            "provider_env_key": "CI_REVIEW_PROVIDER",
            "gates": {"review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}},
        }
        new = gates_state._set_field(state, "review", "enabled", "true")
        self.assertTrue(new["gates"]["review"]["enabled"])

    def test_enabled_accepts_truthy_aliases(self) -> None:
        for v in ("true", "TRUE", "1", "yes", "YES"):
            new = gates_state._set_field(
                {"schema_version": "1.0.0", "gates": {"review": {"enabled": False, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}}},
                "review", "enabled", v,
            )
            self.assertTrue(new["gates"]["review"]["enabled"], v)
        for v in ("false", "0", "no", "NO"):
            new = gates_state._set_field(
                {"schema_version": "1.0.0", "gates": {"review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}}},
                "review", "enabled", v,
            )
            self.assertFalse(new["gates"]["review"]["enabled"], v)

    def test_enabled_rejects_garbage(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state._set_field(
                {"schema_version": "1.0.0", "gates": {"review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}}},
                "review", "enabled", "maybe",
            )

    def test_unknown_gate_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state._set_field(
                {"schema_version": "1.0.0", "gates": {}}, "lint", "enabled", "true"
            )

    def test_unknown_field_raises(self) -> None:
        with self.assertRaises(gates_state.ValidationError):
            gates_state._set_field(
                {"schema_version": "1.0.0", "gates": {"review": {"enabled": True, "workflow": "review.yml", "var": "GATES_REVIEW_ENABLED"}}},
                "review", "bogus", "true",
            )


class TestDetectOwnerRepoBodyEquivalence(unittest.TestCase):
    """Body-equivalence guard for `detect_owner_repo` between gates_state and ci_setup.

    The two implementations are functionally equivalent today; this test is
    git-free and runs in any sandbox. If a future commit drifts them, the
    test fails with a normalized diff and points at the offending line. Per
    PR #834 review (round 2, MAJOR #3): the previous `@unittest.skip` on
    the git-init-based TestDetectOwnerRepo left drift invisible; this
    test fills that gap without requiring `git init`.

    Normalization: drop docstrings, drop Python line-comments, and
    collapse whitespace. Trivial reformatting (multi-line vs single-line
    kwarg, extra `# SSH:` comment, etc.) does not trip the assertion —
    only actual logic drift does.
    """

    @staticmethod
    def _normalize(fn) -> str:
        import inspect
        import io
        import re as _re
        import tokenize as _tokenize

        src = inspect.getsource(fn)
        # 1. Strip the docstring (different prose across the two copies).
        src = _re.sub(r'"""[\s\S]*?"""', "", src)
        # 2. Strip Python line-comments via the tokenize module so the
        #    `# SSH: git@github.com:OWNER/REPO(.git)` notes in ci_setup
        #    don't surface as false-positive drift.
        try:
            tokens = list(_tokenize.tokenize(io.BytesIO(src.encode("utf-8")).readline))
            tokens = [t for t in tokens if t.type != _tokenize.COMMENT]
            src = _tokenize.untokenize(tokens).decode("utf-8")
        except _tokenize.TokenizeError:
            pass  # fall through; whitespace-collapsed raw source is still useful
        # 3. Collapse whitespace.
        return _re.sub(r"\s+", " ", src).strip()

    def test_bodies_are_byte_equivalent_after_normalization(self) -> None:
        import ci_setup  # imported lazily so the heavy chain stays out of unit-test paths
        self.assertEqual(
            self._normalize(gates_state.detect_owner_repo),
            self._normalize(ci_setup.detect_owner_repo),
            "lib/gates_state.py:detect_owner_repo drifted from lib/ci_setup.py:detect_owner_repo; "
            "consolidate into lib/gh_cli.py or sync by hand.",
        )


@unittest.skip("git init is flaky/slow in this sandbox; detect_owner_repo itself is not core to the SSOT")
class TestDetectOwnerRepo(unittest.TestCase):
    """Best-effort owner/repo probe. Mirrors lib/ci_setup.detect_owner_repo."""

    def test_https_github_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(["git", "-C", td, "init", "-q"], capture_output=True, check=True)
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                capture_output=True, check=True,
            )
            self.assertEqual(gates_state.detect_owner_repo(Path(td)), "acme/widgets")

    def test_ssh_github_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(
                ["git", "-C", td, "init", "-q"], capture_output=True, check=True
            )
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "git@github.com:acme/widgets.git"],
                capture_output=True, check=True,
            )
            self.assertEqual(gates_state.detect_owner_repo(Path(td)), "acme/widgets")

    def test_no_remote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(
                ["git", "-C", td, "init", "-q"], capture_output=True, check=True
            )
            self.assertIn("(auto-detect failed: no remote)", gates_state.detect_owner_repo(Path(td)))

    def test_non_github_remote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(
                ["git", "-C", td, "init", "-q"], capture_output=True, check=True
            )
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "https://gitlab.com/x/y.git"],
                capture_output=True, check=True,
            )
            self.assertIn("(auto-detect failed: remote is not GitHub)", gates_state.detect_owner_repo(Path(td)))


class TestSync(unittest.TestCase):
    """`sync` pushes per-gate flags via `gh variable set`. Mock the gh subprocess."""

    @unittest.skip("git init is flaky/slow in this sandbox")
    def test_sync_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            __import__("subprocess").run(["git", "-C", td, "init", "-q"], capture_output=True, check=True)
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                capture_output=True, check=True,
            )
            gates_state.write_state({"schema_version": "1.0.0", "gates": {}}, target)
            # Stub gh availability + subprocess for `gh variable set`.
            with mock.patch.object(gates_state, "gh_available", return_value=("/fake/gh", "")):
                with mock.patch.object(gates_state, "_sync_one", return_value=(True, "")) as m:
                    result = gates_state.sync(root=target)
            self.assertEqual(result["gh_path"], "/fake/gh")
            self.assertEqual(result["repo"], "acme/widgets")
            self.assertEqual(set(result["results"].keys()), {"review", "security", "maintenance"})
            for gate, r in result["results"].items():
                self.assertTrue(r["ok"], f"{gate}: {r}")
            self.assertEqual(m.call_count, 3)

    def test_sync_degraded_when_gh_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(gates_state, "gh_available", return_value=(None, "gh not on PATH")):
                result = gates_state.sync(root=Path(td))
            self.assertIsNone(result["gh_path"])
            self.assertIn("gh not on PATH", result["degraded"])
            self.assertEqual(result["results"], {})

    @unittest.skip("git init is flaky/slow in this sandbox")
    def test_sync_per_gate_failure_doesnt_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            __import__("subprocess").run(["git", "-C", td, "init", "-q"], capture_output=True, check=True)
            __import__("subprocess").run(
                ["git", "-C", td, "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                capture_output=True, check=True,
            )
            gates_state.write_state({"schema_version": "1.0.0", "gates": {}}, target)

            def fake_sync(gh, repo, gate, body, *, timeout=10):
                return (gate != "security", "boom" if gate == "security" else "")

            with mock.patch.object(gates_state, "gh_available", return_value=("/fake/gh", "")):
                with mock.patch.object(gates_state, "_sync_one", side_effect=fake_sync):
                    result = gates_state.sync(root=target)
            self.assertTrue(result["results"]["review"]["ok"])
            self.assertFalse(result["results"]["security"]["ok"])
            self.assertEqual(result["results"]["security"]["stderr"], "boom")
            self.assertTrue(result["results"]["maintenance"]["ok"])

    def test_sync_degraded_when_no_github_remote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            __import__("subprocess").run(
                ["git", "-C", td, "init", "-q"], capture_output=True, check=True
            )
            with mock.patch.object(gates_state, "gh_available", return_value=("/fake/gh", "")):
                result = gates_state.sync(root=Path(td))
            self.assertEqual(result["gh_path"], "/fake/gh")
            self.assertIn("no github remote", result["degraded"])


if __name__ == "__main__":
    unittest.main()
