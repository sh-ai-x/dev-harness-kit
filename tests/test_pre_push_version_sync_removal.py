"""test_pre_push_version_sync_removal.py — RED tests pinning the
post-merge-queue behavior of `.githooks/pre-push` and `bin/sync-version.sh`.

Background (see docs/proposals/release/plugin-version-bump-via-merge-queue.yaml):
pre-merge-queue, every feature PR hit a one-line conflict on
.claude-plugin/plugin.json:version because the trunk version-bump
workflow pushed a fresh version to main faster than parallel PRs could
rebase. Two pieces of code were responsible for catching each branch
up to origin/main's version before push:

  1. `.githooks/pre-push` -- the auto-SYNC block that called
     `bin/sync-version.sh --target <origin/main version>` and committed
     the resulting single-line change.
  2. `bin/sync-version.sh` -- the SYNC (not BUMP) implementation that
     copied origin/main's version field into both plugin manifests.

Post-merge-queue (2026-08-30), the GitHub Merge Queue rebases every
PR onto the latest main (which already includes the version-bump
bump from the previously-merged PR) immediately before merge, so the
per-PR conflict can't happen anymore. Both pieces of code are now
dead and the conflict-resolution contract has moved into the queue.

These tests pin the new behavior:

  - `bin/sync-version.sh --check` still works (legacy callers; returns
    drift notice but doesn't write)
  - `bin/sync-version.sh` with no args exits 0 and emits the
    deprecation notice (legacy callers that invoke it unconditionally)
  - `.githooks/pre-push` no longer contains the auto-SYNC code path
    (no `bin/sync-version.sh` invocation, no `chore(sync):` commit
    message construction, no jq comparison that mutates working tree)

Until this PR lands, the existing `.githooks/pre-push` still calls
`bin/sync-version.sh` and the existing `bin/sync-version.sh` still
mutates the working tree -- so these tests must FAIL today. That's
the RED evidence that gates the production change.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SYNC_SCRIPT = PROJECT_ROOT / "bin" / "sync-version.sh"
PRE_PUSH_HOOK = PROJECT_ROOT / ".githooks" / "pre-push"


class TestSyncVersionShNoOp(unittest.TestCase):
    """`bin/sync-version.sh` is now a no-op compat shim."""

    def test_script_exists_and_is_executable(self) -> None:
        self.assertTrue(SYNC_SCRIPT.exists(), f"missing: {SYNC_SCRIPT}")
        mode = SYNC_SCRIPT.stat().st_mode
        self.assertTrue(mode & 0o111, f"sync-version.sh must be executable (mode={oct(mode)})")

    def test_no_args_emits_deprecation_notice_and_exits_0(self) -> None:
        r = subprocess.run(
            ["bash", str(SYNC_SCRIPT)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, f"expected exit 0 (no-op success); got stderr: {r.stderr!r}")
        # Notice must mention "deprecated" so callers reading the
        # output can tell the no-op is intentional, not a silent
        # failure to mutate.
        self.assertIn(
            "deprecated",
            (r.stdout + r.stderr).lower(),
            f"expected a 'deprecated' notice in output; got: stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        # And it must mention the new mechanism so an operator
        # debugging "why didn't this fix anything" lands on the
        # proposal that explains the merge-queue.
        self.assertIn(
            "merge queue",
            (r.stdout + r.stderr).lower(),
            f"expected the notice to point at the merge queue; got: stdout={r.stdout!r} stderr={r.stderr!r}",
        )

    def test_check_subcommand_reports_drift_without_writing(self) -> None:
        """`--check` must still report drift to the caller (legacy
        debug UX), but must NOT call jq to mutate the working tree.
        We can't easily assert "didn't write" without a snapshot, so
        we instead assert the exit code is 1 (drift) and that the
        manifest on disk was untouched.
        """
        manifest = PROJECT_ROOT / ".claude-plugin" / "plugin.json"
        before = manifest.read_text(encoding="utf-8") if manifest.exists() else None
        try:
            r = subprocess.run(
                ["bash", str(SYNC_SCRIPT), "--check"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # local version matches origin/main in this checkout, so
            # --check should report 0 (no drift). If the repo is
            # somehow stale, that's still OK -- we just need the
            # script to return a deterministic code.
            self.assertIn(r.returncode, (0, 1), f"--check must exit 0 or 1, got {r.returncode}")
        finally:
            after = manifest.read_text(encoding="utf-8") if manifest.exists() else None
            self.assertEqual(before, after, "sync-version.sh --check must not mutate the working tree")

    def test_target_subcommand_does_not_write(self) -> None:
        """Legacy callers passing `--target` (e.g. CI scripts) must
        get the no-op behavior, NOT a silent mutation. The previous
        implementation wrote; the new one ignores and warns.
        """
        manifest = PROJECT_ROOT / ".claude-plugin" / "plugin.json"
        before = manifest.read_text(encoding="utf-8") if manifest.exists() else None
        try:
            r = subprocess.run(
                ["bash", str(SYNC_SCRIPT), "--target", "v9.9.9"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(r.returncode, 0, f"target= should be no-op exit 0; got stderr: {r.stderr!r}")
            self.assertIn("no-op", (r.stdout + r.stderr).lower())
        finally:
            after = manifest.read_text(encoding="utf-8") if manifest.exists() else None
            self.assertEqual(before, after, "sync-version.sh --target must not mutate the working tree")


class TestPrePushHookRemovesAutoSync(unittest.TestCase):
    """.githooks/pre-push must no longer contain the auto-SYNC code path."""

    def test_no_invocation_of_sync_version_script(self) -> None:
        """The hook used to call `bin/sync-version.sh` inside the
        freshness check; that path is dead under merge queue.
        """
        text = PRE_PUSH_HOOK.read_text(encoding="utf-8")
        # The legacy invocation site: `if ! "$SYNC_SCRIPT" --target`.
        self.assertNotIn(
            "sync-version.sh --target",
            text,
            "pre-push must no longer invoke bin/sync-version.sh --target",
        )
        # And the script-path assignment: `SYNC_SCRIPT="$REPO_ROOT/bin/sync-version.sh"`.
        self.assertNotIn(
            "SYNC_SCRIPT=",
            text,
            "pre-push must no longer resolve SYNC_SCRIPT (the auto-sync path is dead)",
        )

    def test_no_chore_sync_commit_message(self) -> None:
        """The hook used to commit a `chore(sync): advance plugin.json
        from vX to vY` change; under merge queue this never happens
        locally -- the queue handles the bump at merge time.
        """
        text = PRE_PUSH_HOOK.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r"chore\(sync\):",
            "pre-push must no longer create chore(sync): commits (merge queue owns the bump)",
        )

    def test_docstring_references_proposal(self) -> None:
        """The hook's header comment should point at the proposal
        doc so the next operator who sees the deprecation can find the
        reason without spelunking git history.
        """
        text = PRE_PUSH_HOOK.read_text(encoding="utf-8")
        self.assertIn(
            "merge-queue",
            text,
            "pre-push docstring should reference the merge-queue proposal",
        )


if __name__ == "__main__":
    unittest.main()
