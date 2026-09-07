#!/usr/bin/env python3
"""The stage matrix must match its SSOT, and hook tests must be isolated.

Two defects this pins, both found by running the full suite inside a
bootstrapped worktree rather than a fresh clone:

1. **`l4-todo-scan` was permanently dead.** `hooks/index.md` lists it as
   active in build / review / security, but the hook name was never added
   to `DEFAULT_MATRIX` in `lib/active_hooks_codec.py`. `is_hook_active`
   returns False for a hook absent from the stage dict, so the
   `hook_stage_active l4-todo-scan || exit 0` line at
   `hooks/l4-todo-scan.sh:37` exited 0 in every stage from the moment
   #680 shipped it. The Iron Law L4 marker scan never ran.

2. **Hook tests were not isolated from project state.**
   `hooks/lib/stage-gate.sh` fail-opens when `.dev-kit/.active-hooks.json`
   is absent, so on a fresh clone (and on CI runners) every stage-gated
   hook runs and its tests pass. In a bootstrapped checkout the file
   exists, the gate resolves the stage to `bootstrap`, and hooks that are
   off in that stage exit 0 silently — so the black-box hook tests assert
   against a hook that deliberately did nothing. 15 tests across
   test_slop_detector.py / test_l4_todo_scan.py flipped to failing purely
   because the developer had run bootstrap.

The fix for (2) is `DEV_KIT_STAGE` in each hook test's env, which is why
this file also pins that the env var actually overrides the gate.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import active_hooks_codec  # noqa: E402

HOOKS_INDEX = REPO_ROOT / "hooks" / "index.md"

# Hooks that call `hook_stage_active <name> || exit 0`. Every one of these
# is gated, so every one MUST appear in the matrix or it is dead code.
GATED_HOOKS = (
    "bash-guard",
    "l4-todo-scan",
    "secret-scan",
    "slop-detector",
    "stop-verify",
    "tdd-guard",
)

# hooks/index.md labels bash-guard as "bash-guard (tier 1)" and
# "(tier 2)" because the hook has two sub-routes with different risk
# tiers; the table-comparison test skips tiered labels. Keep the
# GATED_HOOKS vs index-label gap explicit so the test can be fixed in
# one place.
_INDEX_LABEL_ALIASES = {
    "bash-guard": "bash-guard (tier 2)",  # tier 2 is the matrix-driven path
}


class TestGatedHooksArePresentInMatrix(unittest.TestCase):
    """A gated hook missing from DEFAULT_MATRIX is silently disabled."""

    def test_every_gated_hook_appears_in_default_matrix(self):
        matrix = active_hooks_codec.DEFAULT_MATRIX
        missing = [
            hook for hook in GATED_HOOKS
            if not any(hook in stage_map for stage_map in matrix.values())
        ]
        self.assertEqual(
            missing, [],
            f"gated hooks absent from DEFAULT_MATRIX: {missing}. "
            "is_hook_active() returns False for an absent hook, so the "
            "`hook_stage_active <name> || exit 0` line in the hook script "
            "exits 0 in EVERY stage and the hook never runs.",
        )

    def test_gated_hooks_call_sites_match_this_fixture(self):
        # Guard the fixture itself: if someone gates a new hook, this test
        # fails until GATED_HOOKS is updated, so the check above cannot go
        # stale and silently stop covering a hook.
        found = set()
        for script in sorted((REPO_ROOT / "hooks").glob("*.sh")):
            for line in script.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("hook_stage_active "):
                    found.add(stripped.split()[1])
        self.assertEqual(
            found, set(GATED_HOOKS),
            "hooks calling hook_stage_active have drifted from this "
            f"test's GATED_HOOKS fixture. Found: {sorted(found)}",
        )

    def test_l4_todo_scan_is_active_in_its_documented_stages(self):
        # hooks/index.md marks l4-todo-scan as ✅ for build / review /
        # security. The default matrix must agree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for stage in ("build", "review", "security"):
                self.assertTrue(
                    active_hooks_codec.is_hook_active(
                        root, stage, "l4-todo-scan"
                    ),
                    f"l4-todo-scan must be active in the {stage} stage "
                    "per hooks/index.md",
                )
            for stage in ("bootstrap", "plan", "design", "ship"):
                self.assertFalse(
                    active_hooks_codec.is_hook_active(
                        root, stage, "l4-todo-scan"
                    ),
                    f"l4-todo-scan must be inactive in the {stage} stage "
                    "per hooks/index.md",
                )


class TestHooksIndexMatchesMatrix(unittest.TestCase):
    """`hooks/index.md` is the documented SSOT for the stage matrix.

    Parses the markdown table and compares it cell-by-cell to
    DEFAULT_MATRIX, so a future doc/code divergence fails here instead of
    silently disabling a hook.
    """

    def _parse_index_table(self) -> "dict[str, dict[str, object]]":
        """Return {hook: {stage: cell}} from hooks/index.md's table.

        Cell values are kept as the raw legend token rather than a bool
        because the table is not binary: `✅` is on, `-` is off, and `R`
        means read-only — which `is_hook_active` reports as ACTIVE. A
        bool-only parser silently mis-read `secret-scan/bootstrap`.

        Rows whose hook label carries a parenthetical tier (e.g.
        `bash-guard (tier 1)` / `(tier 2)`) are merged under the bare
        hook name via `_INDEX_LABEL_ALIASES`; tier 1 is the catastrophic
        always-deny tier that bypasses the matrix entirely, so only the
        matrix-driven tier (tier 2) is comparable.
        """
        stages: list[str] = []
        parsed: dict[str, dict[str, object]] = {}
        in_matrix_section = False
        for line in HOOKS_INDEX.read_text(encoding="utf-8").splitlines():
            # Bind to the ONE table under the matrix heading. hooks/index.md
            # has a later per-hook description table whose first column is
            # also a hook name; parsing both let its prose cells overwrite
            # real matrix rows.
            if line.startswith("## "):
                in_matrix_section = line.strip() == "## Hook matrix (per stage)"
                continue
            if not in_matrix_section or not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            if cells[0].lower() == "hook" and not stages:
                stages = [c.lower() for c in cells[1:]]
                continue
            if not stages or set(cells[0]) <= {"-", ":", " "}:
                continue
            label = cells[0].strip("`")
            canonical = next(
                (h for h, alias in _INDEX_LABEL_ALIASES.items() if alias == label),
                label,
            )
            if canonical not in GATED_HOOKS:
                continue
            parsed[canonical] = dict(zip(stages, cells[1:]))
        return parsed

    @staticmethod
    def _cell_is_active(cell: str) -> bool:
        """Map a legend token to the activity `is_hook_active` reports."""
        return "✅" in cell or cell.strip() == "R"

    def test_table_parses(self):
        # Fixture guard: a parser that silently finds nothing would make
        # the comparison below vacuously pass.
        parsed = self._parse_index_table()
        self.assertEqual(
            sorted(parsed), sorted(GATED_HOOKS),
            f"hooks/index.md table did not yield every gated hook: "
            f"{sorted(parsed)}",
        )

    def test_index_table_agrees_with_default_matrix(self):
        parsed = self._parse_index_table()
        matrix = active_hooks_codec.DEFAULT_MATRIX
        mismatches = []
        for hook, row in parsed.items():
            for stage, cell in row.items():
                if stage not in matrix:
                    continue
                documented = self._cell_is_active(cell)
                actual = bool(matrix[stage].get(hook, False))
                if actual != documented:
                    mismatches.append(
                        f"{hook}/{stage}: index.md={cell!r} "
                        f"(active={documented}) matrix={actual}"
                    )
        self.assertEqual(
            mismatches, [],
            "hooks/index.md and DEFAULT_MATRIX disagree: "
            f"{mismatches}",
        )


class TestStageGateEnvOverride(unittest.TestCase):
    """`DEV_KIT_STAGE` must override the on-disk stage.

    This is the mechanism the hook tests use to isolate themselves from
    whatever stage the developer's own `.dev-kit/` happens to be in.
    """

    GATE_SNIPPET = (
        'source "$1/hooks/lib/stage-gate.sh"\n'
        'hook_stage_active "$2" && echo ACTIVE || echo INACTIVE\n'
    )

    def _probe(self, stage: str, hook: str, project_root: Path) -> str:
        script = project_root / "probe.sh"
        script.write_text(
            "#!/usr/bin/env bash\n" + self.GATE_SNIPPET, encoding="utf-8"
        )
        result = subprocess.run(
            ["bash", str(script), str(REPO_ROOT), hook],
            capture_output=True, text=True, cwd=str(project_root),
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "DEV_KIT_STAGE": stage,
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
            },
            timeout=30,
        )
        return result.stdout.strip()

    def test_env_stage_overrides_on_disk_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A matrix file exists, so the gate does NOT fail open.
            (root / ".dev-kit").mkdir()
            (root / ".dev-kit" / ".active-hooks.json").write_text(
                json.dumps({"schema_version": 1, "events": {}}),
                encoding="utf-8",
            )
            # slop-detector is off in bootstrap, on in build.
            self.assertEqual(
                self._probe("bootstrap", "slop-detector", root), "INACTIVE"
            )
            self.assertEqual(
                self._probe("build", "slop-detector", root), "ACTIVE"
            )


if __name__ == "__main__":
    unittest.main()
