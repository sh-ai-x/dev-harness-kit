#!/usr/bin/env python3
"""
test_active_hooks_codec.py — RED-first tests for active_hooks_codec.py.

Tests cover:
- init_matrix writes default 7-stage matrix
- read_matrix idempotent
- is_hook_active per stage
- env override DEV_KIT_HOOK_OFF
- override.disabled_hooks
- set_stage individual cell update
- cross-codec coexistence (issue #676): regen then codec must not
  lose stage-gated hook state, and codec then regen must not lose
  the event-wiring slice.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import active_hooks_codec  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGEN_TOOL = REPO_ROOT / "tools" / "regenerate_active_hooks.py"
FIXTURE_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"


class TestActiveHooksCodec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".dev-kit").mkdir(parents=True, exist_ok=True)
        # Clean env override
        self._orig_env = os.environ.get("DEV_KIT_HOOK_OFF")
        os.environ.pop("DEV_KIT_HOOK_OFF", None)

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["DEV_KIT_HOOK_OFF"] = self._orig_env
        self.tmp.cleanup()

    def test_init_matrix_default_7_stages(self):
        data = active_hooks_codec.init_matrix(self.root)
        self.assertIn("bootstrap", data["matrix"])
        self.assertIn("plan", data["matrix"])
        self.assertIn("design", data["matrix"])
        self.assertIn("build", data["matrix"])
        self.assertIn("review", data["matrix"])
        self.assertIn("security", data["matrix"])
        self.assertIn("ship", data["matrix"])
        # 5 hooks per stage
        for stage in data["matrix"].values():
            self.assertEqual(len(stage), 5)

    def test_is_hook_active_default(self):
        active_hooks_codec.init_matrix(self.root)
        # build: all 5 hooks ON (or read-only)
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard"))
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "build", "bash-guard"))
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "build", "secret-scan"))
        # plan: only stop-verify
        self.assertFalse(active_hooks_codec.is_hook_active(self.root, "plan", "tdd-guard"))
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "plan", "stop-verify"))

    def test_is_hook_active_bootstrap_readonly(self):
        active_hooks_codec.init_matrix(self.root)
        # bootstrap: secret-scan = "read-only" (truthy)
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "bootstrap", "secret-scan"))
        # others False
        self.assertFalse(active_hooks_codec.is_hook_active(self.root, "bootstrap", "tdd-guard"))

    def test_env_override_disables_hook(self):
        active_hooks_codec.init_matrix(self.root)
        with patch.dict(os.environ, {"DEV_KIT_HOOK_OFF": "tdd-guard,slop-detector"}):
            self.assertFalse(active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard"))
            self.assertFalse(active_hooks_codec.is_hook_active(self.root, "build", "slop-detector"))
            self.assertTrue(active_hooks_codec.is_hook_active(self.root, "build", "bash-guard"))

    def test_disable_override(self):
        active_hooks_codec.init_matrix(self.root)
        active_hooks_codec.disable_override(self.root, "bash-guard")
        self.assertFalse(active_hooks_codec.is_hook_active(self.root, "build", "bash-guard"))
        # Other hooks unaffected
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard"))

    def test_set_stage_cell(self):
        active_hooks_codec.init_matrix(self.root)
        active_hooks_codec.set_stage(self.root, "plan", "tdd-guard", True)
        self.assertTrue(active_hooks_codec.is_hook_active(self.root, "plan", "tdd-guard"))

    def test_read_matrix_idempotent(self):
        active_hooks_codec.init_matrix(self.root)
        d1 = active_hooks_codec.read_matrix(self.root)
        d2 = active_hooks_codec.read_matrix(self.root)
        self.assertEqual(d1["matrix"], d2["matrix"])

    def test_load_matrix_on_missing_file_does_not_create_it(self):
        """load_matrix is read-only — must not call atomic_write_json on miss."""
        matrix_path = self.root / ".dev-kit" / ".active-hooks.json"
        self.assertFalse(matrix_path.exists())
        result = active_hooks_codec.load_matrix(self.root)
        # In-memory default returned.
        self.assertEqual(result["matrix"], active_hooks_codec.DEFAULT_MATRIX)
        # No side effect: file still absent.
        self.assertFalse(matrix_path.exists())

    def test_set_stage_on_fileless_root_does_not_leak_into_other_fileless_root(self):
        """Regression for issue #480.

        set_stage() on a project root with no .active-hooks.json used to
        mutate the module-level DEFAULT_MATRIX in place (because
        load_matrix()'s fallback path returned a live reference to it).
        That corruption then leaked into any other file-less project root
        queried afterwards in the same process.
        """
        root_a = self.root / "project_a"
        root_b = self.root / "project_b"
        root_a.mkdir()
        root_b.mkdir()

        # Sanity: build/tdd-guard defaults to True, neither root has a file yet.
        self.assertFalse((root_a / ".dev-kit" / ".active-hooks.json").exists())
        self.assertFalse((root_b / ".dev-kit" / ".active-hooks.json").exists())
        self.assertTrue(active_hooks_codec.DEFAULT_MATRIX["build"]["tdd-guard"])

        # Mutate root_a's (file-less) matrix.
        active_hooks_codec.set_stage(root_a, "build", "tdd-guard", False)

        # root_b is a completely unrelated, also file-less project root.
        # It must still see the documented default, not root_a's mutation.
        data_b = active_hooks_codec.load_matrix(root_b)
        self.assertTrue(data_b["matrix"]["build"]["tdd-guard"])
        self.assertTrue(
            active_hooks_codec.is_hook_active(root_b, "build", "tdd-guard")
        )

        # The module-level default itself must remain untouched.
        self.assertTrue(active_hooks_codec.DEFAULT_MATRIX["build"]["tdd-guard"])


class TestCrossCodecCoexistence(unittest.TestCase):
    """Regression for issue #676.

    Two writers touch `.dev-kit/.active-hooks.json`:
      - `lib/active_hooks_codec.py` (stage-keyed `matrix` + `override`)
      - `tools/regenerate_active_hooks.py` (event-keyed `events`)

    These tests pin the contract that BOTH writers namespace their
    own slice and PRESERVE the other writer's slice on re-run. The
    old shape wiped each other out — causing stage-gated hooks
    (`tdd-guard`, `bash-guard`, `secret-scan`, `slop-detector`,
    `stop-verify`, `pre_completion_checklist`) to silently turn off
    after the first SessionStart.
    """

    def setUp(self):
        # Sandbox root with its own copies of hooks/hooks.json so
        # the test cannot pollute the worktree's `.dev-kit/`.
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_HOOKS_JSON, self.root / "hooks" / "hooks.json")
        # Clean any stale `.dev-kit/`.
        dev_kit = self.root / ".dev-kit"
        if dev_kit.exists():
            shutil.rmtree(dev_kit)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_regen(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REGEN_TOOL), "--root", str(self.root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"regen failed: rc={result.returncode} stderr={result.stderr!r}",
        )

    def _target_path(self) -> Path:
        return self.root / ".dev-kit" / ".active-hooks.json"

    def test_ensure_matrix_then_regen_preserves_matrix(self):
        """ensure_matrix() writes the codec slice; regen() must preserve it."""
        # Step 1: codec initializes file with stage-keyed matrix slice.
        active_hooks_codec.ensure_matrix(self.root)
        before_data = json.loads(self._target_path().read_text(encoding="utf-8"))
        self.assertIn("matrix", before_data)
        # Sanity: stage-gated hook is OFF by config before regen.
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard"),
            "precondition: build/tdd-guard must default to True",
        )

        # Step 2: regen runs and overwrites — must keep matrix slice intact.
        self._run_regen()
        after_data = json.loads(self._target_path().read_text(encoding="utf-8"))

        # Regen writer owns `events` / `schema_version` / `generated_at`.
        self.assertIn("events", after_data)
        self.assertIn("schema_version", after_data)
        self.assertIn("generated_at", after_data)
        # Codec-owned slice MUST be byte-equal.
        self.assertEqual(after_data["matrix"], before_data["matrix"])
        self.assertEqual(after_data.get("override"), before_data.get("override"))

        # is_hook_active() still answers correctly — the regression
        # that turned every stage-gated hook off after the first
        # SessionStart is no longer present.
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard")
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "bash-guard")
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "review", "secret-scan")
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "slop-detector")
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "stop-verify")
        )

    def test_regen_then_ensure_matrix_preserves_events(self):
        """regen() writes the events slice; ensure_matrix() must preserve it."""
        # Step 1: regen runs first (mimics SessionStart firing on a
        # fresh checkout before the codec has been called).
        self._run_regen()
        regen_only = json.loads(self._target_path().read_text(encoding="utf-8"))
        self.assertIn("events", regen_only)
        # Sanity: regen alone has no `matrix` (codec hasn't run yet).
        self.assertNotIn("matrix", regen_only)

        # Step 2: codec writes its slice — must keep the events
        # slice intact.
        active_hooks_codec.ensure_matrix(self.root)
        combined = json.loads(self._target_path().read_text(encoding="utf-8"))
        self.assertIn("matrix", combined)
        # Regen-owned slice (events) must be byte-equal.
        self.assertEqual(combined["events"], regen_only["events"])
        self.assertEqual(combined["schema_version"], regen_only["schema_version"])
        self.assertEqual(combined["generated_at"], regen_only["generated_at"])

        # Stage-gated hook answers must reflect the freshly-written
        # default matrix.
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard")
        )

    def test_set_stage_after_regen_does_not_lose_events(self):
        """set_stage() after regen must preserve the events slice."""
        self._run_regen()
        regen_only = json.loads(self._target_path().read_text(encoding="utf-8"))

        active_hooks_codec.set_stage(self.root, "plan", "tdd-guard", True)
        after = json.loads(self._target_path().read_text(encoding="utf-8"))

        # Events slice must be unchanged.
        self.assertEqual(after["events"], regen_only["events"])
        # Codec mutation landed in the matrix slice.
        self.assertTrue(after["matrix"]["plan"]["tdd-guard"])
        # is_hook_active reads the new cell.
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "plan", "tdd-guard")
        )

    def test_regen_round_trip_byte_identical_modulo_timestamp(self):
        """Two back-to-back regens after codec init keep events slice stable.

        The first regen writes the events slice; the codec's
        ensure_matrix adds the matrix slice; the second regen must
        round-trip the events slice byte-identically (modulo
        `generated_at` if it crosses a second boundary).
        """
        active_hooks_codec.ensure_matrix(self.root)
        self._run_regen()
        first = json.loads(self._target_path().read_text(encoding="utf-8"))
        self._run_regen()
        second = json.loads(self._target_path().read_text(encoding="utf-8"))
        # Codec slice still there.
        self.assertIn("matrix", second)
        # Events slice identical.
        self.assertEqual(first["events"], second["events"])
        # Matrix slice identical (regen didn't touch it).
        self.assertEqual(first["matrix"], second["matrix"])


    def test_regen_alone_keeps_stage_gated_hooks_active(self):
        """Regression for issue #676 — fresh-checkout safety.

        After the regen tool creates `.dev-kit/.active-hooks.json`
        with only the event-keyed slice (the default state on every
        clone because `.dev-kit/` is gitignored), the codec's
        `is_hook_active()` must STILL return True for the stage-gated
        hooks. Without this guarantee, `stage-gate.sh` would stop
        fail-opening once the file exists, silently disabling
        `tdd-guard`, `bash-guard`, `secret-scan`, `slop-detector`,
        and `stop-verify` on every fresh clone.

        The regen tool MUST NOT create the codec slice itself —
        `is_hook_active` falls back to `DEFAULT_MATRIX` so the
        documented fail-open behavior of `stage-gate.sh` survives.
        """
        # Sanity: precondition is exactly the regression case — regen
        # created the file, codec has not run yet.
        self._run_regen()
        data = json.loads(self._target_path().read_text(encoding="utf-8"))
        self.assertIn("events", data)
        self.assertNotIn("matrix", data)

        # All five stage-gated hooks must still answer True for their
        # default-on stages. If any of these return False, the
        # fresh-checkout regression has returned.
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "tdd-guard"),
            "regen alone must not disable build/tdd-guard",
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "bash-guard"),
            "regen alone must not disable build/bash-guard",
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "secret-scan"),
            "regen alone must not disable build/secret-scan",
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "slop-detector"),
            "regen alone must not disable build/slop-detector",
        )
        self.assertTrue(
            active_hooks_codec.is_hook_active(self.root, "build", "stop-verify"),
            "regen alone must not disable build/stop-verify",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
