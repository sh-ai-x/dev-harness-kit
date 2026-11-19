"""test_ci_branch_policy.py — regression tests for `lib/ci_branch_policy.py`.

`fetch_required_status_checks()` backs the "branch policy" check of
`/dev-kit:ci-doctor`. When the `gh api` probe fails, the function must
return a *human-readable* degradation reason so the doctor can print
"SKIP: <why>" instead of a nonsense one-character string.

The original implementation smuggled the failure reason through the
returned `set[str]` as `set(f"gh api failed: {err}")` and read it back
with `.pop()`. `set(<str>)` splits a string into its individual
characters, so the caller received one arbitrary character (e.g. `'a'`)
instead of the real reason — the doctor then printed
`branch policy | SKIP | a`, which is undiagnosable for the operator.

These tests pin:
  - the degradation message carries the real reason (regression),
  - the exception path names the exception type (previously it dropped
    the exception entirely and fell back to the bare string
    "gh api failed"),
  - the happy path still returns the context names,
  - the legacy `.checks[].context` fallback still works,
  - "no contexts found" is still reported when both probes are empty.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib.ci_branch_policy import fetch_required_status_checks  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _gh_ok(timeout: int = 10):
    """Stand-in for `lib.gh_cli._gh_available` — gh present + authenticated."""
    return "/usr/bin/gh", ""


def _gh_missing(timeout: int = 10):
    return None, "gh not on PATH"


def _patch_run(monkeypatch, fn):
    monkeypatch.setattr(subprocess, "run", fn)


class TestFetchRequiredStatusChecks:
    def test_api_failure_reports_full_reason(self, monkeypatch):
        """REGRESSION: a failing `gh api` must yield the real reason,
        not a single character from `set(<str>)`."""
        _patch_run(monkeypatch, lambda *a, **k: _FakeCompleted(
            stdout="", stderr="gh: Not Found (HTTP 404)\n", returncode=1,
        ))
        names, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert names == set()
        assert "gh api failed" in degraded
        assert "Not Found" in degraded, (
            f"degradation reason must carry the gh stderr text, got {degraded!r}"
        )
        assert len(degraded) > 5, (
            f"degradation reason must not be a truncated fragment, got {degraded!r}"
        )

    def test_api_failure_reason_is_not_a_character_set_fragment(self, monkeypatch):
        """The old bug returned `set(f"gh api failed: {err}")` — the
        degraded string was one arbitrary character of that message."""
        _patch_run(monkeypatch, lambda *a, **k: _FakeCompleted(
            stdout="", stderr="HTTP 403 Forbidden\n", returncode=1,
        ))
        _, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert len(degraded) > 1, (
            f"degradation reason must be a full message, not one character: "
            f"{degraded!r}"
        )
        assert "403" in degraded

    def test_api_failure_without_stderr_reports_returncode(self, monkeypatch):
        """No stderr on failure -> the exit code is the only signal."""
        _patch_run(monkeypatch, lambda *a, **k: _FakeCompleted(
            stdout="", stderr="", returncode=3,
        ))
        _, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert "gh api failed" in degraded
        assert "3" in degraded

    def test_subprocess_exception_names_exception_type(self, monkeypatch):
        """The exception path must name the exception class. Previously
        the exception was swallowed and the reason degraded to the bare
        string "gh api failed"."""
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

        _patch_run(monkeypatch, _boom)
        names, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert names == set()
        assert "gh api error" in degraded
        assert "TimeoutExpired" in degraded

    def test_success_returns_context_names(self, monkeypatch):
        _patch_run(monkeypatch, lambda *a, **k: _FakeCompleted(
            stdout="lint\ntest (python 3.12)\n", returncode=0,
        ))
        names, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert names == {"lint", "test (python 3.12)"}
        assert degraded == ""

    def test_legacy_checks_context_fallback(self, monkeypatch):
        """Empty `.contexts[]?` -> fall back to `.checks[].context`."""
        calls: list[str] = []

        def _run(cmd, **k):
            jq = cmd[cmd.index("--jq") + 1]
            calls.append(jq)
            if jq == ".contexts[]?":
                return _FakeCompleted(stdout="", returncode=0)
            return _FakeCompleted(stdout="legacy-check\n", returncode=0)

        _patch_run(monkeypatch, _run)
        names, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert names == {"legacy-check"}
        assert degraded == ""
        assert calls == [".contexts[]?", ".checks[].context"]

    def test_no_contexts_found(self, monkeypatch):
        """Both probes succeed but are empty -> 'no contexts found'."""
        _patch_run(monkeypatch, lambda *a, **k: _FakeCompleted(
            stdout="", returncode=0,
        ))
        names, degraded = fetch_required_status_checks(_gh_ok, "owner/repo")
        assert names == set()
        assert degraded == "no contexts found"

    def test_gh_missing_is_degraded_without_probing(self, monkeypatch):
        """No gh on PATH -> SKIP with the probe reason, no subprocess call."""
        def _never(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("gh api must not run when gh is absent")

        _patch_run(monkeypatch, _never)
        names, degraded = fetch_required_status_checks(_gh_missing, "owner/repo")
        assert names == set()
        assert degraded == "gh not on PATH"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
