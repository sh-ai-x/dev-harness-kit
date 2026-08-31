"""test_review_local_preview.py — regression test for the read-only HTML
viewer at ``tools/review-local-preview.html`` (issue #777).

Bug
---
``bin/babysit-pr-local.sh`` mirrors ``bin/review-local.sh`` output via a
``/tail`` SSE route into the browser. When a single ``bin/review-local.sh``
iteration finishes, the server emits an ``iteration_done`` frame with
the wrapper-script's exit_code. The viewer's badge handler previously
read ``data.exit_code`` alone, so a wrapper bug (e.g. rc=1 with all 3
gate judges approving) left the page header stuck on "running"
indefinitely -- operators could not tell that the run was
semantically complete.

Fix
---
``tools/review-local-preview.html`` now derives the badge from the
aggregate gate state first (Approve / Blocked / Changes Requested /
incomplete), falling back to the wrapper rc only when gate state is
incomplete. This test drives the page through 8 synthetic
``iteration_done`` scenarios and asserts the badge text + class.

Harness
-------
The page is plain HTML + a JS IIFE. Playwright isn't installed in the
test env, so we extract the JS into a Node + jsdom harness under
``tests/fixtures/review-local-preview/test-badge.js``. The harness
loads the real HTML into jsdom, stubs ``window.EventSource`` with a
controllable mock, mutates the per-gate CSS classes to simulate
prior stdout frames, and dispatches synthetic ``iteration_done``
frames. This Python test invokes the Node harness via ``subprocess``
and asserts exit code 0.

The 8 scenarios pin the exact badge text + class for the issue #777
repro AND every other aggregate-verdict precedence case. If a future
change regresses any of them, the Node script exits 1 and this pytest
fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
NODE_TEST = PROJECT_ROOT / "tests" / "fixtures" / "review-local-preview" / "test-badge.js"


def _node_executable() -> str:
    """Return a node binary that can find the fixture's jsdom install.

    The fixture dir has its own node_modules/jsdom (installed via
    ``npm install jsdom`` in tests/fixtures/review-local-preview/),
    so we set NODE_PATH to that node_modules before invoking node.
    Falls back to whatever ``node`` is on PATH.
    """
    return os.environ.get("NODE_BIN", "node")


class TestReviewLocalPreview(unittest.TestCase):
    def test_node_jsdom_harness_passes_all_scenarios(self) -> None:
        """Invoke the Node + jsdom harness and assert exit 0.

        The harness covers 8 scenarios:
          1. all 3 approved + rc=1 (the exact issue #777 repro)
          2. all 3 approved + rc=0 (clean run)
          3. one gate still running -> incomplete / watching
          4. one gate blocked -> Blocked / done-err
          5. one gate changes-requested -> Changes Requested / done-err
          6. blocked AND changes present -> blocked precedence wins
          7. one gate parse-failed -> incomplete / watching
          8. all 3 missing (initial state) -> incomplete / watching
        """
        self.assertTrue(
            NODE_TEST.is_file(),
            f"Node test fixture missing: {NODE_TEST}",
        )
        node_modules = NODE_TEST.parent / "node_modules"
        jsdom_dir = node_modules / "jsdom"
        if not jsdom_dir.is_dir():
            # Hermetic CI: install on first run. Fixture is tiny (jsdom +
            # transitive deps, ~5s cold). Subsequent runs use the populated
            # node_modules. If `npm` is missing, fail with a clear hint.
            npm = os.environ.get("NPM_BIN", "npm")
            install = subprocess.run(
                [npm, "install", "--no-audit", "--no-fund", "--prefix", str(NODE_TEST.parent)],
                cwd=str(NODE_TEST.parent),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if install.returncode != 0:
                self.fail(
                    "jsdom not installed in fixture dir and `npm install` "
                    f"failed (rc={install.returncode}).\n"
                    f"stdout:\n{install.stdout}\n"
                    f"stderr:\n{install.stderr}\n"
                    f"Run manually: npm install --prefix {NODE_TEST.parent}"
                )
            self.assertTrue(
                jsdom_dir.is_dir(),
                f"npm install succeeded but {jsdom_dir} still missing",
            )

        env = os.environ.copy()
        env["NODE_PATH"] = str(node_modules)

        proc = subprocess.run(
            [_node_executable(), str(NODE_TEST)],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Echo harness output to the pytest console for diagnosability;
        # on success each scenario prints two "ok" lines.
        if proc.stdout:
            print(proc.stdout, file=sys.stdout, end="")
        if proc.stderr:
            print(proc.stderr, file=sys.stdout, end="")
        self.assertEqual(
            proc.returncode,
            0,
            f"Node harness exited {proc.returncode}; expected 0. "
            f"See captured stdout/stderr above for the failing assertion.",
        )


if __name__ == "__main__":
    unittest.main()
