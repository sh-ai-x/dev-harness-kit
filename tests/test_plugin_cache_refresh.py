"""test_plugin_cache_refresh.py — regression tests for the
SessionStart auto-sync hook `hooks/plugin-cache-refresh.sh` and the
shared helper `lib/plugin_cache_refresh.sh`.

Behaviors locked by these tests:

  1. Drift detection: new marketplace commit -> hook rsyncs the new
     file into the cache + writes the marker. Mirrors the ralph-at-
     0.3.350 bug the fix exists to solve.
  2. Idempotency: no new commit -> no rsync, no marker rewrite.
  3. Soft-fail: marketplace absent, marketplace not a git clone,
     rsync missing, jq missing -> rc=0, no crash.
  4. Marker location + contents: <cache-dir>/.devkit-refresh-head,
     value == short HEAD.
  5. NO git pull: the hook must never run `git pull` (which would be
     hostile from a SessionStart hook). Adding a commit locally and
     not pushing must still produce a sync.

Reuses the bare-remote + marketplace fixture from test_devkit_refresh.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Reuse the marketplace + remote factory from the existing devkit-refresh
# tests so we do not duplicate the git plumbing setup.
sys.path.insert(0, str(Path(__file__).parent))
from test_devkit_refresh import _cache_root, _git, _init_marketplace_with_remote  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "plugin-cache-refresh.sh"
CODEX_HOOK = REPO_ROOT / ".codex-plugin" / "hooks" / "plugin-cache-refresh.sh"
HELPER = REPO_ROOT / "lib" / "plugin_cache_refresh.sh"
DEV_KIT_REFRESH = REPO_ROOT / "bin" / "devkit-refresh.sh"


def _run_hook(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, timeout=30, env=env,
    )


class TestPluginCacheRefresh(unittest.TestCase):
    """hooks/plugin-cache-refresh.sh — SessionStart auto-sync."""

    @classmethod
    def setUpClass(cls):
        if not HOOK.exists():
            raise unittest.SkipTest(f"hook not found: {HOOK}")
        if not HELPER.exists():
            raise unittest.SkipTest(f"helper not found: {HELPER}")
        if not DEV_KIT_REFRESH.exists():
            raise unittest.SkipTest(f"devkit-refresh.sh not found: {DEV_KIT_REFRESH}")
        if not CODEX_HOOK.exists():
            raise unittest.SkipTest(f"codex hook not found: {CODEX_HOOK}")

    # ---- 1. drift detection -------------------------------------------------

    def test_drift_triggers_rsync_and_writes_marker(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)

            # First invocation (no marker yet) populates the cache.
            r1 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r1.returncode, 0, f"stderr={r1.stderr}")

            cache_dir = cache_root / "0.1.0"
            self.assertTrue((cache_dir / "README.md").exists())
            marker = cache_dir / ".devkit-refresh-head"
            self.assertTrue(marker.exists())
            first_sha = marker.read_text().strip()

            # Add a new commit to the marketplace.
            (mp / "skills").mkdir(exist_ok=True)
            (mp / "skills" / "new.md").write_text("new\n")
            _git(mp, "add", "-A")
            _git(mp, "commit", "-q", "-m", "add skills/new.md")
            second_sha = _git(mp, "rev-parse", "--short", "HEAD").stdout.decode().strip()

            self.assertNotEqual(first_sha, second_sha)

            # Second invocation must rsync + update marker.
            r2 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r2.returncode, 0, f"stderr={r2.stderr}")
            self.assertTrue(
                (cache_dir / "skills" / "new.md").exists(),
                "new marketplace file did not propagate to cache",
            )
            self.assertEqual(marker.read_text().strip(), second_sha)

    # ---- 2. idempotency -----------------------------------------------------

    def test_no_drift_silent_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)

            r1 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r1.returncode, 0, f"stderr={r1.stderr}")
            marker = cache_root / "0.1.0" / ".devkit-refresh-head"
            self.assertTrue(marker.exists())
            sentinel = cache_root / "0.1.0" / "README.md"
            mtime_before = sentinel.stat().st_mtime_ns

            # Second invocation with no new commit -> no-op.
            r2 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r2.returncode, 0, f"stderr={r2.stderr}")
            self.assertEqual(sentinel.stat().st_mtime_ns, mtime_before,
                             "no-drift run must not re-write cache files")

    # ---- 3. soft-fail edge cases -------------------------------------------

    def test_marketplace_absent_silent_exit(self):
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "cache"
            cache_root.mkdir()
            r = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": "/no/such/path",
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")

    def test_marketplace_not_git_clone_silent_exit(self):
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "mp"
            mp.mkdir()
            (mp / ".claude-plugin").mkdir()
            (mp / ".claude-plugin" / "plugin.json").write_text(
                '''{"name": "dev-kit", "version": "0.1.0"}\n'''
            )
            cache_root = Path(td) / "cache"
            cache_root.mkdir()
            r = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")

    def test_cache_dir_absent_gets_created(self):
        """If a cache root exists but no version subdir, the hook creates it."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)
            self.assertFalse((cache_root / "0.1.0").exists())
            r = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            self.assertTrue((cache_root / "0.1.0").exists())

    def test_rsync_missing_fail_open(self):
        """PATH without rsync -> hook must exit 0, not crash."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)
            # Inherit a full PATH but prepend a dir that contains a stub
            # `rsync` which exits 127 -- mirrors "rsync not installed" so
            # the hook must bail gracefully rather than abort.
            fake_bin = tmp / "fake_bin"
            fake_bin.mkdir()
            (fake_bin / "rsync").write_text("#!/bin/sh\nexit 127\n")
            (fake_bin / "rsync").chmod(0o755)
            r = _run_hook({
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0,
                             f"hook must fail open without rsync, got {r.returncode}: {r.stderr}")

    def test_jq_missing_in_cc_hook_is_ignored(self):
        """The CC hook (hooks/plugin-cache-refresh.sh) does not call jq
        directly — it only sources lib/plugin_cache_refresh.sh which is
        pure bash + rsync + git. So a missing jq must NOT affect the
        hook (the preamble's `::warning::` fires, hook still exits 0).
        Contrast with the Codex manual script
        skills/codex-cache-update/scripts/update.sh which DOES die
        hard on missing jq (covered by the existing codex_cache_update
        test)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)
            fake_bin = tmp / "fake_bin"
            fake_bin.mkdir()
            # Stub jq as missing, but keep bash/git/rsync available.
            (fake_bin / "jq").write_text("#!/bin/sh\nexit 127\n")
            (fake_bin / "jq").chmod(0o755)
            r = _run_hook({
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            # The hook ran the rsync even without jq available.
            self.assertTrue((cache_root / "0.1.0" / "README.md").exists())

    # ---- 4. marker location + contents -------------------------------------

    def test_marker_location_and_contents(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)
            r = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")

            cache_dir = cache_root / "0.1.0"
            marker = cache_dir / ".devkit-refresh-head"
            self.assertTrue(marker.exists())
            expected = _git(mp, "rev-parse", "--short", "HEAD").stdout.decode().strip()
            self.assertEqual(marker.read_text().strip(), expected)

    # ---- 5. NO git pull -----------------------------------------------------

    def test_helper_no_git_pull(self):
        """A commit added to the marketplace WITHOUT being pushed to the
        bare remote must still propagate to the cache. This locks the
        invariant that the hook never runs `git pull` (which would fail
        in this scenario because nothing has been pushed)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {"README.md": "v1\n"})
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)

            # First invocation establishes the baseline.
            r1 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r1.returncode, 0, f"stderr={r1.stderr}")

            # Add a commit locally without pushing.
            (mp / "unpushed.md").write_text("not pushed\n")
            _git(mp, "add", "-A")
            _git(mp, "commit", "-q", "-m", "local-only commit")

            # Second invocation must still sync the new file.
            r2 = _run_hook({
                **os.environ,
                "DEV_KIT_MARKETPLACE_DIR": str(mp),
                "DEV_KIT_CACHE_ROOT": str(cache_root),
            })
            self.assertEqual(r2.returncode, 0, f"stderr={r2.stderr}")
            cache_dir = cache_root / "0.1.0"
            self.assertTrue(
                (cache_dir / "unpushed.md").exists(),
                "unpushed commit must still sync (proves hook does NOT git pull)",
            )

    # ---- 6. bin/devkit-refresh.sh refactor still works ----------------------

    def test_helper_called_from_bin_devkit_refresh(self):
        """bin/devkit-refresh.sh must continue to work end-to-end after
        the refactor that delegates its rsync to plugin_cache_sync."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mp = _init_marketplace_with_remote(tmp, {
                "README.md": "v1\n",
                "hooks/test.sh": "#!/bin/sh\necho ok\n",
            })
            cache_root = _cache_root(tmp)
            cache_root.mkdir(parents=True)
            r = subprocess.run(
                ["bash", str(DEV_KIT_REFRESH),
                 "--marketplace", str(mp), "--cache", str(cache_root)],
                capture_output=True, text=True, timeout=60,
                env={**os.environ},
            )
            self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
            cache_dir = cache_root / "0.1.0"
            self.assertTrue((cache_dir / "README.md").exists())
            self.assertTrue((cache_dir / "hooks" / "test.sh").exists())
            # chmod +x must be preserved.
            mode = (cache_dir / "hooks" / "test.sh").stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, "hook .sh file lost +x bit")
            # Marker file written.
            self.assertTrue((cache_dir / ".devkit-refresh-head").exists())

    # ---- 7. Codex twin shape parity ----------------------------------------

    def test_codex_twin_uses_codex_paths(self):
        """The Codex twin at .codex-plugin/hooks/ must use CODEX_*
        env-var defaults, not DEV_KIT_*."""
        text = CODEX_HOOK.read_text()
        self.assertIn("CODEX_MARKETPLACE_DIR", text)
        self.assertIn("CODEX_CACHE_ROOT", text)
        self.assertIn(".codex-plugin", text)
        # Must NOT carry the CC defaults.
        self.assertNotIn("DEV_KIT_MARKETPLACE_DIR", text)
        self.assertNotIn("DEV_KIT_CACHE_ROOT", text)


if __name__ == "__main__":
    unittest.main()
