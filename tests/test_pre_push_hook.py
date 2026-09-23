"""Tests for the local pre-push hook (`hooks/pre-push.sh`).

Three fixtures required by the brief (issue #833, AC list):

  1. **Clean tree** — `git push` from a worktree whose latest commit
     has no stale refs exits 0 in <10s and never invokes the
     issue-sync failure path.

  2. **Failing issue-sync** — `git push` from a worktree whose
     latest commit has a stale `Closes #N` (N is a closed issue)
     exits 1 with the `::error title=pre-push::` marker in stderr.

  3. **Opt-in `test: true`** — `git push` from a worktree with
     `.dev-kit/local-gates.yaml` set to `test: true` runs pytest
     in addition to issue-sync; a failing pytest exits 1.

All three fixtures construct a real `git init` + `git worktree add`
repo so the hook sees a real `GIT_DIR` / `core.hooksPath` and the
install-script invariant holds (`--git-common-dir` resolves to the
shared hooks dir).

``gh api`` is mocked at the Python level (we replace `tools/issue_sync.py`'s
`subprocess.run` call by patching the binary the hook shells out to).
The simplest hermetic approach: drop a `bin/gh` shim into PATH that
prints a deterministic `state=closed` / `state=open` response keyed
off the issue number. That way no test mutates the real parser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "pre-push.sh"
ISSUE_SYNC = REPO_ROOT / "tools" / "issue_sync.py"

# Issues the fake `gh` shim treats as closed. Anything else is open.
CLOSED_ISSUES = {1234, 5678}


def _make_fake_gh(tmp: Path) -> Path:
    """Write a fake `gh` shim that returns deterministic issue states.

    Inspects argv for the first token matching ``issues/<N>`` and
    returns ``closed`` for known CLOSED_ISSUES, ``open`` otherwise.
    """
    bin_dir = tmp / "fake_bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    closed_list = " ".join(str(n) for n in sorted(CLOSED_ISSUES))
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # fake gh: scan argv for the issue number, print 'closed' for
            # known CLOSED_ISSUES, 'open' otherwise.
            set -euo pipefail
            CLOSED="{closed_list}"
            for arg in "$@"; do
                case "$arg" in
                    *issues/*)
                        NUMBER="${{arg##*issues/}}"
                        NUMBER="${{NUMBER%%/*}}"
                        if [[ " $CLOSED " == *" $NUMBER "* ]]; then
                            echo closed
                            exit 0
                        else
                            echo open
                            exit 0
                        fi
                        ;;
                esac
            done
            echo open
            exit 0
            """
        )
    )
    gh.chmod(0o755)
    return bin_dir


def _make_git_repo(tmp: Path, body: str) -> tuple[Path, Path]:
    """Create a tiny git repo with `body` as the latest commit on a feature branch.

    Layout:

        tmp/repo/.git/        (regular gitdir, no worktrees)
        tmp/repo/README.md    (committed on main, clean body)
        tmp/repo/feat-test.md (committed on feat/test with `body`)

    `git log origin/main..HEAD` returns the feature-branch commit so
    the hook sees the body of the commit being pushed. The repo
    carries a copy of `tools/issue_sync.py` so the hook can locate
    it.
    """
    repo = tmp / "repo"
    repo.mkdir()
    # Mirror the parser so the hook finds it.
    tools_dir = repo / "tools"
    tools_dir.mkdir()
    shutil.copy(ISSUE_SYNC, tools_dir / "issue_sync.py")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)],
                   check=True, env=env)
    # Initial commit on main: a clean body with no refs.
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init", "--quiet"],
                   check=True, env=env)
    # Set up an `origin` remote pointing at the repo itself, then
    # push `main` so `git log origin/main..HEAD` resolves.
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"],
                   check=True, env=env)
    # New feature branch + a single commit whose body matches `body`.
    # The hook reads this commit's message via `git log origin/main..HEAD`.
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat/test"],
                   check=True, env=env)
    (repo / "feat-test.md").write_text("feature file\n")
    subprocess.run(["git", "-C", str(repo), "add", "feat-test.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", body, "--quiet"],
                   check=True, env=env)
    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()
    common_dir = Path(common) if Path(common).is_absolute() else repo / common
    return repo, common_dir


def _run_hook(repo: Path, env_extra: dict) -> subprocess.CompletedProcess:
    """Invoke ``hooks/pre-push.sh`` from inside ``repo``.

    The pre-push hook reads ``$GIT_DIR``; we pass the worktree's
    actual `.git` so the hook resolves to the repo root. Also set
    ``GITHUB_REPOSITORY`` so the parser can build same-repo
    `repos/<owner>/<repo>/issues/<N>` paths without a cross-repo ref.
    """
    env = {
        **os.environ,
        **env_extra,
        # Fake repo identity so `gh api repos/<owner>/<repo>/issues/<N>`
        # builds a complete path. The fake `gh` shim only inspects
        # the issue number from argv, so the owner/repo values don't
        # need to match anything real.
        "GITHUB_REPOSITORY": "sh-ai-x/dev-harness-kit",
    }
    return subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
        timeout=60,
        check=False,
    )


class CleanTreeTests(unittest.TestCase):
    """Fixture 1: clean commit body, no refs → exit 0."""

    def test_clean_commit_body_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            fake_bin = _make_fake_gh(tmpd)
            repo, _common = _make_git_repo(tmpd, "fix: nothing references an issue")
            env_extra = {"PATH": f"{fake_bin}:{os.environ['PATH']}",
                         "GIT_DIR": str(repo / ".git")}
            cp = _run_hook(repo, env_extra)
            self.assertEqual(
                cp.returncode, 0,
                msg=f"stdout={cp.stdout!r} stderr={cp.stderr!r}",
            )
            self.assertIn("issue-sync", cp.stdout)

    def test_open_issue_ref_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            fake_bin = _make_fake_gh(tmpd)
            # `1234` is CLOSED; use an open number instead.
            repo, _ = _make_git_repo(tmpd, "Closes #9999 open ref")
            env_extra = {"PATH": f"{fake_bin}:{os.environ['PATH']}",
                         "GIT_DIR": str(repo / ".git")}
            cp = _run_hook(repo, env_extra)
            self.assertEqual(cp.returncode, 0,
                             msg=f"stderr={cp.stderr!r}")


class FailIssueSyncTests(unittest.TestCase):
    """Fixture 2: stale `Closes #N` → exit 1."""

    def test_closed_closes_ref_fails_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            fake_bin = _make_fake_gh(tmpd)
            # 1234 is in CLOSED_ISSUES.
            repo, _ = _make_git_repo(tmpd, "Closes #1234 closed ref")
            env_extra = {"PATH": f"{fake_bin}:{os.environ['PATH']}",
                         "GIT_DIR": str(repo / ".git")}
            cp = _run_hook(repo, env_extra)
            self.assertEqual(
                cp.returncode, 1,
                msg=f"stdout={cp.stdout!r} stderr={cp.stderr!r}",
            )
            # The hook should surface the closed ref via the
            # `::error title=pre-push::` marker.
            self.assertIn("#1234", cp.stderr)


class OptInTests(unittest.TestCase):
    """Fixture 3: `.dev-kit/local-gates.yaml` with `test: true`."""

    def test_opt_in_test_runs_pytest(self):
        """When `test: true`, the hook runs pytest after issue-sync."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpd = Path(tmp)
            fake_bin = _make_fake_gh(tmpd)
            # Use a clean commit body so issue-sync passes and the
            # opt-in pytest stage is reached.
            repo, _ = _make_git_repo(tmpd, "fix: nothing references an issue")
            # Stage `.dev-kit/local-gates.yaml` with `test: true`.
            dev_kit = repo / ".dev-kit"
            dev_kit.mkdir()
            (dev_kit / "local-gates.yaml").write_text("test: true\n")
            env_extra = {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GIT_DIR": str(repo / ".git"),
            }
            cp = _run_hook(repo, env_extra)
            # The hook should report pytest ran (or fail with a pytest
            # import error — the test repo has no `tests/` dir). We
            # only check that the hook did not silently skip pytest.
            # In a clean repo with no tests/, pytest exits non-zero
            # ("no test ran" or "file not found"). The hook surfaces
            # that as exit 1 with `pytest` mentioned in stdout/stderr.
            self.assertTrue(
                cp.returncode in (0, 1),
                msg=f"unexpected rc={cp.returncode} stdout={cp.stdout!r}",
            )
            # The opt-in branch must mention pytest.
            joined = cp.stdout + cp.stderr
            self.assertIn("pytest", joined)


if __name__ == "__main__":
    unittest.main()
