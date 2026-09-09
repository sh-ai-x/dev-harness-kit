"""test_log_retention — bin/log-retention.sh dry-run + APPROVE behaviors.

Uses a tmp_path as the LOG_RETENTION_ROOT (via env override) so the test
doesn't touch the real ~/.claude/projects directory. Backdates files via
os.utime to trigger the gzip / delete thresholds without sleeping.
"""
import gzip
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "log-retention.sh"


def _seed(root: Path, *, jsonl_old: int, jsonl_fresh: int, gz_old: int) -> dict:
    """Seed the fake transcript tree. Returns mtimes in seconds (epoch)."""
    proj = root / "proj-a"
    proj.mkdir(parents=True)
    proj_b = root / "proj-b"
    proj_b.mkdir(parents=True)

    now = int(time.time())
    files = {}

    # Old jsonl (>= LOG_RETENTION_DAYS) → should gzip.
    f_old = proj / "session-old.jsonl"
    f_old.write_text('{"role":"user","content":"old transcript"}\n')
    os.utime(f_old, (now - jsonl_old, now - jsonl_old))
    files["old_jsonl"] = f_old

    # Fresh jsonl (< LOG_RETENTION_DAYS) → should be retained.
    f_fresh = proj / "session-fresh.jsonl"
    f_fresh.write_text('{"role":"user","content":"fresh"}\n')
    os.utime(f_fresh, (now - jsonl_fresh, now - jsonl_fresh))
    files["fresh_jsonl"] = f_fresh

    # Old gz (>= LOG_RETENTION_ARCHIVE_DAYS) → should delete.
    f_gz = proj_b / "session-ancient.jsonl.gz"
    with gzip.open(f_gz, "wt") as fh:
        fh.write('{"role":"user","content":"ancient"}\n')
    os.utime(f_gz, (now - gz_old, now - gz_old))
    files["old_gz"] = f_gz

    return files


def _run(root: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["LOG_RETENTION_ROOT"] = str(root)
    env["LOG_RETENTION_DAYS"] = "30"
    env["LOG_RETENTION_ARCHIVE_DAYS"] = "180"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, check=False,
    )


def test_dry_run_no_changes(tmp_path):
    files = _seed(tmp_path, jsonl_old=40 * 86400, jsonl_fresh=5 * 86400, gz_old=200 * 86400)
    cp = _run(tmp_path, "--dry-run")
    assert cp.returncode == 0, cp.stderr
    # Dry-run must NOT touch files.
    assert files["old_jsonl"].exists()
    assert files["fresh_jsonl"].exists()
    assert files["old_gz"].exists()
    # And the output should mention both would-gzip and would-delete.
    assert "would gzip" in cp.stdout
    assert "would delete" in cp.stdout


def test_dry_run_is_default(tmp_path):
    files = _seed(tmp_path, jsonl_old=40 * 86400, jsonl_fresh=5 * 86400, gz_old=200 * 86400)
    cp = _run(tmp_path)  # no -y / no APPROVE
    assert cp.returncode == 0, cp.stderr
    assert "dry-run" in cp.stdout
    # Files untouched.
    assert files["old_jsonl"].exists()
    assert files["old_gz"].exists()


def test_approve_gzips_old_jsonl(tmp_path):
    files = _seed(tmp_path, jsonl_old=40 * 86400, jsonl_fresh=5 * 86400, gz_old=400 * 86400)
    cp = _run(tmp_path, "-y")
    assert cp.returncode == 0, cp.stderr
    gz_path = files["old_jsonl"].with_suffix(".jsonl.gz")
    # Old .jsonl replaced by .jsonl.gz.
    assert not files["old_jsonl"].exists()
    assert gz_path.exists()
    # Fresh .jsonl stays.
    assert files["fresh_jsonl"].exists()
    # The 400d-old .gz is well past the 180d archive cutoff → must be deleted.
    assert not files["old_gz"].exists()
    # And the summary line should mention both gzip + delete counts.
    assert "gzipped=" in cp.stdout
    assert "deleted=" in cp.stdout


def test_approve_via_env_var(tmp_path):
    files = _seed(tmp_path, jsonl_old=40 * 86400, jsonl_fresh=5 * 86400, gz_old=400 * 86400)
    cp = _run(tmp_path, env_extra={"APPROVE": "1"})
    assert cp.returncode == 0, cp.stderr
    gz_path = files["old_jsonl"].with_suffix(".jsonl.gz")
    assert gz_path.exists()
    assert files["fresh_jsonl"].exists()


def test_race_guard_skips_recently_modified(tmp_path):
    """Files modified within the last 60 s must be skipped, even if the age
    in seconds would otherwise qualify for gzip/delete."""
    files = _seed(tmp_path, jsonl_old=40 * 86400, jsonl_fresh=5 * 86400, gz_old=400 * 86400)
    # Re-touch the 'old' jsonl to now (still qualifies as 40d-old in principle,
    # but the mtime is current → race guard should skip).
    now = time.time()
    os.utime(files["old_jsonl"], (now, now))
    cp = _run(tmp_path, "-y")
    assert cp.returncode == 0, cp.stderr
    # Should NOT have been gzipped.
    assert files["old_jsonl"].exists()
    assert not files["old_jsonl"].with_suffix(".jsonl.gz").exists()


def test_empty_root_existing_no_files(tmp_path):
    cp = _run(tmp_path, "--dry-run")
    assert cp.returncode == 0, cp.stderr
    # Existing-but-empty root → dry-run summary, retain_count=0.
    assert "retain_count=0" in cp.stdout


def test_missing_root(tmp_path):
    """A nonexistent LOG_RETENTION_ROOT path is a no-op with a friendly line."""
    cp = _run(tmp_path / "does-not-exist", "--dry-run")
    assert cp.returncode == 0, cp.stderr
    assert "nothing to do" in cp.stdout


def test_help(tmp_path):
    cp = _run(tmp_path, "--help")
    assert cp.returncode == 0
    assert "Usage" in cp.stdout or "log-retention" in cp.stdout.lower()


def test_invalid_flag(tmp_path):
    cp = _run(tmp_path, "--bogus")
    assert cp.returncode == 2
    assert "unknown flag" in cp.stderr
