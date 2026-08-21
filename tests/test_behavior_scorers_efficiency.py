#!/usr/bin/env python3
"""test_behavior_scorers_efficiency.py — RED-first tests for the
behavior_scorers/efficiency.py write_baseline_metrics path.

Target: the bare-except around json.dump + os.fdopen. Narrowed to
(OSError, ValueError, TypeError) so BaseException cousins (KeyboardInterrupt,
SystemExit) propagate; non-realistic failures (e.g. AttributeError on a
malformed metrics dict shape) must also propagate.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from behavior_scorers import efficiency  # noqa: E402


class TestSaveBaselineExceptionNarrowing(unittest.TestCase):
    """Bare-except narrowing for behavior_scorers.efficiency._save_baseline.

    Inner ops: os.open(O_NOFOLLOW|O_CREAT|O_EXCL), os.fdopen, json.dump.
    Realistic failure modes: OSError (perm/IO/disk), ValueError (bad
    input), TypeError (non-serializable metrics). Anything else (incl.
    KeyboardInterrupt / SystemExit) must propagate.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_serializable_metrics_raises_typeerror_and_cleans_partial(self):
        target = self.root / "baseline.json"
        with self.assertRaises(TypeError):
            efficiency._save_baseline(target, {"oops": set()})  # set() not JSON-serializable
        # Cleanup must have removed the partial file.
        self.assertFalse(target.exists(), msg=f"partial file left behind: {target}")

    def test_oserror_propagates_and_cleans_partial(self):
        target = self.root / "baseline.json"
        # efficiency.py imports `os` inside the function, so we patch
        # the global `os.fdopen` reference instead of a module-level attr.
        with patch("os.fdopen", side_effect=OSError("simulated fdopen failure")):
            with self.assertRaises(OSError):
                efficiency._save_baseline(target, {"k": 1})
        self.assertFalse(target.exists(), msg=f"partial file left behind: {target}")

    def test_keyboard_interrupt_propagates_without_cleanup_attempt(self):
        """KeyboardInterrupt is BaseException, NOT Exception. The narrowed
        `except (OSError, ValueError, TypeError)` MUST NOT swallow it."""
        target = self.root / "baseline.json"
        with patch("json.dump", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                efficiency._save_baseline(target, {"k": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
