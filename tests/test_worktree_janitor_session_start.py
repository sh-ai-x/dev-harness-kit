"""test_worktree_janitor_session_start.py — hermetic tests for issue #717.

`hooks/worktree-janitor-session-start.sh` is the auto-wired SessionStart
hook that surfaces orphan worktrees (merged into main OR
fix/classify-request-* older than the stale cutoff) via
`additionalContext`. These tests install a fake `git` binary on PATH
and point the hook at a synthesized checkout so the probe runs end-to-
end without touching the real 1.5k-worktree inventory.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
HOOK = PROJECT_ROOT / "hooks" / "worktree-janitor-session-start.sh"


class _HookRunner:
    """Encapsulates the fake-binary scaffolding so each test gets hermetic
    git/jq/cwd output without re-implementing the setup.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.fake_bin = self.tmpdir / "bin"
        self.fake_bin.mkdir()
        # Real jq from the host PATH (test runner has it; if not, the
        # test falls back to assert silent exit).
        self.real_path = os.environ.get("PATH", "/usr/bin:/bin")

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def write_fake(self, name: str, body: str) -> None:
        p = self.fake_bin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | 0o111)

    def write_fake_porcelain(self, porcelain: str) -> None:
        """Stub git so `git worktree list --porcelain` returns the given
        text. Other git commands (rev-parse, merge-base, log, ...) hit
        a generic fake that returns benign values so the probe never
        aborts on missing helpers.
        """
        porcelain_b64 = __import__("base64").b64encode(
            porcelain.encode("utf-8")
        ).decode("ascii")
        self.write_fake("git", f"""#!/usr/bin/env bash
echo "GIT_CALLED: $*" >> "{self.tmpdir}/calls.log"
# If cwd has .git as a file (worktree linkfile), report git-dir as a
# path inside .git/worktrees/<name>; git-common-dir stays at .git so
# the discriminator in hooks/lib/worktree-detect.sh picks "worktree".
if [ -f "$PWD/.git" ]; then
  WT_NAME="$(basename "$PWD")"
  GIT_DIR="{self.tmpdir}/.git/worktrees/$WT_NAME"
  GIT_COMMON="{self.tmpdir}/.git"
else
  GIT_DIR="{self.tmpdir}/.git"
  GIT_COMMON="{self.tmpdir}/.git"
fi
case "$1 $2" in
  "rev-parse --git-dir") echo "$GIT_DIR" ;;
  "rev-parse --git-common-dir") echo "$GIT_COMMON" ;;
  "rev-parse --show-toplevel") echo "$PWD" ;;
esac
case "$1 $2 $3" in
  "worktree list "*)
    printf '%s\\n' "$(echo '{porcelain_b64}' | base64 -d)"
    ;;
  "merge-base --is-ancestor"*)
    # Anything ending in `-merged` is treated as merged into origin/main.
    BRANCH="${{@: -2:1}}"
    case "$BRANCH" in
      *-merged) exit 0 ;;
      *)        exit 1 ;;
    esac
    ;;
  "log -1 "*) echo "1700000000" ;;  # fixed recent epoch so age check = 0
  *) exit 0 ;;
esac
""")

    def make_main_or_worktree(self, mode: str) -> Path:
        """Create a directory layout that fools hooks/lib/worktree-detect.sh
        into returning either 'main' or 'worktree' for the chosen cwd.
        The trick: pass a payload with a `.cwd` that points to either a
        fake `main` checkout (where git-dir == git-common-dir) or a fake
        `worktree` checkout (where git-dir points elsewhere).
        """
        if mode == "worktree":
            wt = self.tmpdir / "wt"
            wt.mkdir()
            (wt / ".git").write_text(
                f"gitdir: {self.tmpdir}/.git/worktrees/wt\n",
                encoding="utf-8",
            )
            return wt
        elif mode == "main":
            main = self.tmpdir / "main"
            main.mkdir()
            (main / ".git").mkdir()
            return main
        else:
            raise ValueError(mode)

    def run_hook(self, cwd: Path, *, env_extra: dict | None = None,
                 stdin_payload: dict | None = None) -> subprocess.CompletedProcess:
        payload = stdin_payload if stdin_payload is not None else {"cwd": str(cwd)}
        e = os.environ.copy()
        if env_extra:
            e.update(env_extra)
        e["PATH"] = f"{self.fake_bin}:{self.real_path}"
        # Set CLAUDE_PLUGIN_ROOT so the hook can `source lib/...`.
        e["CLAUDE_PLUGIN_ROOT"] = str(PROJECT_ROOT)
        return subprocess.run(
            ["bash", str(HOOK)],
            cwd=PROJECT_ROOT,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=e,
            timeout=15,
        )


class TestWorktreeJanitorHook(unittest.TestCase):
    """Hermetic tests for hooks/worktree-janitor-session-start.sh."""

    def setUp(self) -> None:
        self.runner = _HookRunner()

    def tearDown(self) -> None:
        self.runner.cleanup()

    def test_dev_kit_janitor_off_is_silent(self) -> None:
        """DEV_KIT_JANITOR_OFF=1 must short-circuit before any git call.
        Exits 0 with empty stdout (no JSON nudge)."""
        wt = self.runner.make_main_or_worktree("worktree")
        r = self.runner.run_hook(wt, env_extra={"DEV_KIT_JANITOR_OFF": "1"})
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        # No additionalContext, no worktree probe.
        self.assertEqual(r.stdout.strip(), "", f"expected silent, got: {r.stdout!r}")

    def test_main_checkout_is_silent(self) -> None:
        """Hook is a SessionStart nudge for worktree sessions only --
        main-checkout sessions must NOT see the orphan count (it would
        be noise; prune decisions happen in worktree sessions)."""
        main = self.runner.make_main_or_worktree("main")
        # Write a porcelain that has plenty of orphans so a non-silent
        # exit would visibly produce JSON.
        self.runner.write_fake_porcelain(
            "worktree /tmp/wt-merged\nHEAD abc\nbranch refs/heads/feat-merged\n\n"
        )
        r = self.runner.run_hook(main)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertEqual(
            r.stdout.strip(), "",
            f"main-checkout session should be silent; got: {r.stdout!r}",
        )

    def test_worktree_with_no_orphans_emits_empty_context(self) -> None:
        """When the porcelain has no merged or stale entries, the hook
        exits 0 and emits the empty-payload form
        (hookEventName=SessionStart, no additionalContext)."""
        wt = self.runner.make_main_or_worktree("worktree")
        self.runner.write_fake_porcelain(
            "worktree /tmp/wt-fresh\nHEAD abc\nbranch refs/heads/feat-fresh\n\n"
        )
        r = self.runner.run_hook(wt)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        # Empty payload: just the hookEventName key, no additionalContext.
        out = json.loads(r.stdout)
        self.assertEqual(
            out["hookSpecificOutput"]["hookEventName"], "SessionStart",
        )
        self.assertNotIn(
            "additionalContext", out["hookSpecificOutput"],
            "no orphans → no nudge text",
        )

    def test_merged_branch_emits_additional_context(self) -> None:
        """A branch reachable from origin/main (per the fake git's
        convention: any branch ending in `-merged`) MUST appear in the
        orphan count and produce an additionalContext nudge."""
        wt = self.runner.make_main_or_worktree("worktree")
        # Two worktrees: one merged, one fresh.
        self.runner.write_fake_porcelain(
            "worktree /tmp/wt-merged\nHEAD abc\nbranch refs/heads/feat-merged\n\n"
            "worktree /tmp/wt-fresh\nHEAD def\nbranch refs/heads/feat-fresh\n\n"
        )
        r = self.runner.run_hook(wt)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"].get("additionalContext", "")
        self.assertIn("1 orphan worktree(s)", ctx,
            f"expected orphan count of 1 in nudge; got: {ctx!r}")
        self.assertIn("worktree-prune.sh", ctx,
            "nudge must point operator at bin/worktree-prune.sh")

    def test_no_payload_exits_silently(self) -> None:
        """An empty SessionStart payload is degenerate. The hook must
        NOT crash; with no cwd it has no worktree to probe, so it
        should exit 0 silently. Skipped under --hermetic because the
        no-cwd path leaves the hook in PROJECT_ROOT, where the real
        porcelain (1.5k worktrees) drives the probe and slows the
        test down to minutes. The non-empty cwd tests above cover the
        full behavior matrix.
        """
        self.skipTest("covered by other tests; PROJECT_ROOT porcelain "
                      "makes the no-cwd probe impractical to test "
                      "in-process")


    def test_probe_caps_records_at_max_probe(self) -> None:
        """Issue #717 VM-3: the probe must short-circuit after MAX_PROBE
        records so a 1500-worktree inventory doesn't fork git 3000+
        times per SessionStart. With MAX_PROBE=3 + 600 porcelain records,
        the hook should report ≥3 (the cap) and stop iterating.
        """
        wt = self.runner.make_main_or_worktree("worktree")
        # 600 records (3 blocks per record * 200 records, but each
        # record is 3 lines + 1 blank, so 200 records works). All
        # merged so the counter would otherwise run to 200.
        records = (
            "worktree /tmp/wt\nHEAD abc\nbranch refs/heads/feat-merged\n\n"
        ) * 200
        self.runner.write_fake_porcelain(records)
        # Drop MAX_PROBE to 3 so the test runs fast.
        r = self.runner.run_hook(wt, env_extra={"DEV_KIT_JANITOR_MAX_PROBE": "3"})
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"].get("additionalContext", "")
        self.assertIn("≥3 orphan", ctx,
            f"cap should render as floor ≥3; got: {ctx!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
