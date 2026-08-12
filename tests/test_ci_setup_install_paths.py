"""test_ci_setup_install_paths.py — pin bin/ + lib/ install paths in ci_setup.

Issue #619 regression coverage. Guards the three structural facts that
let `/dev-kit:babysit-pr-local` work from a consumer install:

  1. The three bin/*.sh scripts are in EXPECTED_PATHS (so they ship).
  2. The three bin/*.sh scripts are in EXECUTABLE_PATHS (so +x is set).
  3. The lib/ helpers actually imported by bin/review-local.sh are in
     EXPECTED_PATHS (so the `python3 -m lib.maintenance_gate` shell-out
     resolves).
  4. The marker `.dev-kit/ci-config.json` records the new bin/lib keys.
  5. End-to-end install into a tmpdir consumer repo produces
     byte-identical bin/lib copies and executable bits.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Locate ci_setup.py the same way the rest of the repo does.
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import ci_setup  # noqa: E402  (after sys.path manipulation)

PATH_ENTRIES = (
    "bin/babysit-pr-local.sh",
    "bin/review-local.sh",
    "bin/set-provider.sh",
    "lib/review_local_lib.sh",
    "lib/maintenance_gate.py",
    "lib/atomic.py",
    "lib/__init__.py",
)


class TestInstallPathsContainBinLib(unittest.TestCase):
    """Static membership checks against EXPECTED_PATHS / EXECUTABLE_PATHS."""

    def test_bin_entrypoints_in_expected_paths(self) -> None:
        for rel in ("bin/babysit-pr-local.sh", "bin/review-local.sh", "bin/set-provider.sh"):
            self.assertIn(
                rel, ci_setup.EXPECTED_PATHS,
                f"{rel} missing from EXPECTED_PATHS (ci-setup will not install it)",
            )

    def test_bin_entrypoints_in_executable_paths(self) -> None:
        for rel in ("bin/babysit-pr-local.sh", "bin/review-local.sh", "bin/set-provider.sh"):
            self.assertIn(
                rel, ci_setup.EXECUTABLE_PATHS,
                f"{rel} missing from EXECUTABLE_PATHS (no +x after install)",
            )

    def test_lib_helpers_in_expected_paths(self) -> None:
        for rel in ("lib/maintenance_gate.py", "lib/atomic.py", "lib/__init__.py", "lib/review_local_lib.sh"):
            self.assertIn(
                rel, ci_setup.EXPECTED_PATHS,
                f"{rel} missing from EXPECTED_PATHS (review-local.sh will fail to import it)",
            )


class TestMarkerRecordsBinLib(unittest.TestCase):
    """The marker `.dev-kit/ci-config.json` must list the new bin + lib keys."""

    def test_ci_setup_marker_records_bin_and_lib_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            marker = target / ci_setup.MARKER_REL
            self.assertTrue(marker.is_file(), f"marker missing: {marker}")
            payload = json.loads(marker.read_text(encoding="utf-8"))

            self.assertIn("bin", payload, "marker missing 'bin' key")
            self.assertIn("lib", payload, "marker missing 'lib' key")

            self.assertEqual(
                sorted(payload["bin"]),
                sorted([
                    "bin/babysit-pr-local.sh",
                    "bin/review-local.sh",
                    "bin/set-provider.sh",
                ]),
            )
            self.assertEqual(
                sorted(payload["lib"]),
                sorted([
                    "lib/review_local_lib.sh",
                    "lib/maintenance_gate.py",
                    "lib/atomic.py",
                    "lib/__init__.py",
                ]),
            )


class TestInstallsBinLibIntoConsumer(unittest.TestCase):
    """End-to-end: install into a tmpdir consumer and byte-verify + +x."""

    def test_ci_setup_installs_bin_and_lib_into_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # Consumer must be a git repo so install_ci_config's drift / SHA
            # pass produces a clean marker. ci-setup does not require this,
            # but it mirrors the real install flow.
            subprocess.run(["git", "init", "-q"], cwd=target, check=True)

            report = ci_setup.install_ci_config(target, force=True)
            self.assertEqual(report.errors, [], f"install errors: {report.errors}")

            for rel in PATH_ENTRIES:
                dst = target / rel
                self.assertTrue(dst.is_file(), f"missing after install: {rel}")

                # Byte-identical to the source file in the plugin tree.
                src = PROJECT_ROOT / rel
                self.assertTrue(src.is_file(), f"plugin source missing: {src}")
                self.assertEqual(
                    dst.read_bytes(), src.read_bytes(),
                    f"byte mismatch for {rel}",
                )

            # bin/*.sh must have +x.
            for rel in ("bin/babysit-pr-local.sh", "bin/review-local.sh", "bin/set-provider.sh"):
                mode = (target / rel).stat().st_mode
                self.assertTrue(
                    mode & 0o111,
                    f"{rel} not executable after install (mode={oct(mode)})",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
