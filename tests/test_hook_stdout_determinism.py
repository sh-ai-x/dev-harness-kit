"""tests/test_hook_stdout_determinism.py — G4 regression for the A1 hook audit.

Per docs/proposals/cache-hit-rate/structural-fix.yaml §Validation gates:

  G4: every hook in hooks/*.sh must produce byte-identical stdout when
      invoked twice with identical stdin. Volatile content (timestamps,
      PIDs, ephemeral ids) silently busts the prompt cache on every
      turn — see rules/session-hygiene.md Iron Law 3.

These tests do NOT scan the actual hooks/ directory — they synthesize
two fake hooks (one stable, one emitting `date`) inside a tmp dir and
invoke ``scripts/audit_hook_stdout.sh`` against it. That way a future
real-hook regression is detected on the actual repo, but the test
itself runs in isolation and never breaks the suite due to a real hook
that happens to use `date`.

Stdlib only. No third-party deps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit_hook_stdout.sh"


def _synth_hooks_dir(tmp: Path) -> tuple[Path, Path]:
    """Create a tmp hooks/ dir with two scripts and return (good, bad).

    The volatile source is ``/dev/urandom`` rather than ``$RANDOM``
    or ``date +%s%N`` because:
      * ``$RANDOM`` can collide between two back-to-back invocations
        (probability ~1/32768, but enough to flake in CI).
      * macOS BSD ``date`` doesn't support ``%N`` (nanoseconds) and
        silently outputs the literal ``%N`` string.
      * ``date +%s`` (seconds) collides on sub-second invocations.

    ``/dev/urandom`` is portable, fast, and produces 16 random hex
    chars per call — collision probability is effectively zero.
    """
    hooks = tmp / "hooks"
    hooks.mkdir()
    good = hooks / "good-hook.sh"
    good.write_text(
        "#!/usr/bin/env bash\n"
        'echo "stable prefix line"\n'
        'echo "another stable line"\n'
    )
    bad = hooks / "bad-hook.sh"
    bad.write_text(
        "#!/usr/bin/env bash\n"
        # 16 hex chars from /dev/urandom = 64 bits of entropy. Two
        # sequential invocations will never produce the same string.
        'echo "rand=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d \' \\n\')"\n'
        'echo "stable suffix"\n'
    )
    os.chmod(good, 0o755)
    os.chmod(bad, 0o755)
    return good, bad


def _run_audit_against(tmp_hooks_dir: Path) -> subprocess.CompletedProcess:
    """Run the audit script with $REPO_ROOT pointed at ``tmp_hooks_dir``.

    The script derives its hooks/ path from its own location, so we
    invoke it via a small shim: a copy in the tmp dir whose parent
    matches tmp_hooks_dir.parent.
    """
    # Symlink the audit script into a tmp layout so its $(dirname)
    # walk lands in tmp_hooks_dir/..
    sandbox = tmp_hooks_dir.parent
    fake_repo = sandbox / "fake-repo"
    fake_repo.mkdir()
    # Move the hooks dir under fake_repo so the audit script's
    #   REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
    #   HOOKS_DIR="${REPO_ROOT}/hooks"
    # resolves to our hooks dir.
    target_hooks = fake_repo / "hooks"
    shutil.move(str(tmp_hooks_dir), str(target_hooks))
    # Copy the audit script into fake_repo/scripts/.
    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir()
    shim = scripts_dir / "audit_hook_stdout.sh"
    shutil.copy(AUDIT_SCRIPT, shim)
    os.chmod(shim, 0o755)
    return subprocess.run(
        ["bash", str(shim)],
        capture_output=True, text=True,
    )


class TestHookStdoutDeterminism(unittest.TestCase):
    """G4 regression — see module docstring."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="hook-audit-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_audit_flags_only_volatile_hook(self):
        """A hook that embeds ``date`` must be flagged; a deterministic
        hook must NOT be flagged. Audit script returns exit 1 when any
        hook is bad, 0 when all are clean."""
        good, bad = _synth_hooks_dir(self.tmpdir)
        proc = _run_audit_against(self.tmpdir / "hooks")
        self.assertEqual(
            proc.returncode, 1,
            f"expected exit 1 (one bad hook), got {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        # bad-hook.sh basename must appear in the report.
        self.assertIn("bad-hook.sh", proc.stdout)
        # good-hook.sh must NOT be in the report.
        self.assertNotIn("good-hook.sh", proc.stdout)

    def test_audit_passes_when_all_hooks_deterministic(self):
        """Sanity: with only stable hooks the audit exits 0."""
        # Replace the bad hook with a deterministic one.
        hooks = self.tmpdir / "hooks"
        hooks.mkdir()
        ok = hooks / "ok-hook.sh"
        ok.write_text("#!/usr/bin/env bash\necho stable\n")
        os.chmod(ok, 0o755)
        proc = _run_audit_against(hooks)
        self.assertEqual(
            proc.returncode, 0,
            f"expected exit 0, got {proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        self.assertIn("byte-deterministic", proc.stdout)

    def test_audit_json_emits_structured_report(self):
        """--json mode emits a parseable JSON report with checked count
        and the list of bad hooks. Useful for CI consumption."""
        hooks = self.tmpdir / "hooks"
        hooks.mkdir()
        (hooks / "good.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        (hooks / "bad.sh").write_text(
            "#!/usr/bin/env bash\n"
            "echo \"rand=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \\n')\"\n"
        )
        os.chmod(hooks / "good.sh", 0o755)
        os.chmod(hooks / "bad.sh", 0o755)
        proc = subprocess.run(
            ["bash", str(_build_shim(self.tmpdir)), "--json"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        import json
        report = json.loads(proc.stdout)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(len(report["bad"]), 1)
        self.assertTrue(report["bad"][0].endswith("bad.sh"))


def _build_shim(tmp_root: Path) -> Path:
    """Re-build the audit shim used by the other tests so we can
    invoke it with --json from a separate test. Idempotent."""
    fake_repo = tmp_root / "fake-repo-json"
    if fake_repo.exists():
        shutil.rmtree(fake_repo)
    fake_repo.mkdir()
    hooks = fake_repo / "hooks"
    hooks.mkdir()
    scripts = fake_repo / "scripts"
    scripts.mkdir()
    shim = scripts / "audit_hook_stdout.sh"
    shutil.copy(AUDIT_SCRIPT, shim)
    os.chmod(shim, 0o755)
    # Caller is responsible for adding the .sh files into hooks/.
    # We return the shim; the test owns the hooks it adds via the
    # path it manipulates. Simpler: copy the test hooks here too.
    src_hooks = tmp_root / "hooks"
    if src_hooks.exists():
        for f in src_hooks.iterdir():
            shutil.copy(f, hooks / f.name)
            os.chmod(hooks / f.name, 0o755)
    return shim


if __name__ == "__main__":
    unittest.main()
