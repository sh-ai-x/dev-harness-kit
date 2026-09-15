"""test_actor_classifier.py — pin the actor classification contract.

Pins every rule documented in `lib/actor_classifier.classify_actor`:
  1. dev-harness-kit itself, maintainer → maintainer_self
  2. dev-harness-kit fork, maintainer → maintainer_fork
  3. dev-harness-kit fork, non-maintainer → consumer_fork
  4. Consumer repo (ci-setup installed), maintainer → maintainer_self
  5. Consumer repo, fork PR from outside → consumer_fork
  6. No git origin → unknown (fail-closed)

Plus CLI shape, idempotency, and the breadcrumb writer.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import actor_classifier  # noqa: E402  (after sys.path manipulation)


def _make_repo(
    tmp: Path,
    *,
    origin: str | None,
    plugin_owner: str | None,
    team_maintainers: list[str] | None,
    head_branch: str = "feat/x",
) -> Path:
    """Build a minimal repo layout under ``tmp`` with optional
    ``.claude-plugin/plugin.json`` and ``.dev-kit/team.json``.

    No actual git init — tests monkeypatch ``actor_classifier._run`` so
    the git CLI is not invoked. The mock is expected to return the
    ``origin`` URL on the first ``git remote get-url origin`` call and
    empty/zero for everything else.
    """
    (tmp / ".dev-kit").mkdir(parents=True, exist_ok=True)
    if plugin_owner is not None:
        (tmp / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (tmp / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "dev-kit", "owner": plugin_owner}),
            encoding="utf-8",
        )
    if team_maintainers is not None:
        (tmp / ".dev-kit" / "team.json").write_text(
            json.dumps({"maintainers": team_maintainers}),
            encoding="utf-8",
        )
    # Stash origin on the tmp dir's `.origin` file so the per-test
    # ``_run_mock`` can read it without each test hand-rolling the
    # origin URL into its mock.return_value.
    (tmp / ".origin").write_text(origin or "", encoding="utf-8")
    return tmp


def _run_mock(tmp: Path, head_branch: str = "feat/x"):
    """Build a ``_run`` mock that returns ``origin`` for
    ``git remote get-url origin`` and ``head_branch`` for
    ``git rev-parse --abbrev-ref HEAD``; everything else returns
    ``(1, "", "")``.
    """
    origin_text = (tmp / ".origin").read_text(encoding="utf-8") if (tmp / ".origin").exists() else ""

    def _impl(cmd, cwd, timeout=5):
        if cmd[:3] == ["git", "remote", "get-url"]:
            if not origin_text:
                return (1, "", "no remote")
            return (0, origin_text, "")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return (0, head_branch, "")
        return (1, "", "")

    return _impl


class TestDetectOwnerRepo(unittest.TestCase):
    """The git-remote parser is reused across gates_state, ci_setup, and
    actor_classifier. Pin all three shapes — HTTPS, SSH, no remote."""

    def test_https(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            with mock.patch.object(
                actor_classifier, "_run",
                return_value=(0, "https://github.com/sh-ai-x/dev-harness-kit.git", ""),
            ):
                self.assertEqual(
                    actor_classifier._detect_owner_repo(tdir),
                    ("sh-ai-x", "dev-harness-kit"),
                )

    def test_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            with mock.patch.object(
                actor_classifier, "_run",
                return_value=(0, "git@github.com:sanghee/my-app.git", ""),
            ):
                self.assertEqual(
                    actor_classifier._detect_owner_repo(tdir),
                    ("sanghee", "my-app"),
                )

    def test_no_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            with mock.patch.object(
                actor_classifier, "_run", return_value=(1, "", "no remote"),
            ):
                self.assertIsNone(actor_classifier._detect_owner_repo(tdir))

    def test_non_github_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            with mock.patch.object(
                actor_classifier, "_run",
                return_value=(0, "https://gitlab.example.com/foo/bar.git", ""),
            ):
                self.assertIsNone(actor_classifier._detect_owner_repo(tdir))


class TestClassifyFiveScenarios(unittest.TestCase):
    """The five scenario contract — one test per case."""

    def test_dev_harness_kit_self_no_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/sh-ai-x/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "consumer_self")
            self.assertEqual(cls.repo_kind, "dev_harness_kit")
            self.assertEqual(cls.recommended_gate, "standard_gates")
            self.assertEqual(cls.remote_owner_repo, ("sh-ai-x", "dev-harness-kit"))

    def test_dev_harness_kit_self_with_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/sh-ai-x/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(
                    tdir, gh_author_association="MEMBER",
                )
            self.assertEqual(cls.actor_type, "maintainer_self")
            self.assertEqual(cls.recommended_gate, "standard_gates")

    def test_dev_harness_kit_fork_maintainer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/alice/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=["alice"],
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "maintainer_fork")
            self.assertEqual(cls.repo_kind, "dev_harness_kit")
            self.assertEqual(cls.recommended_gate, "standard_gates")
            self.assertEqual(cls.remote_owner_repo, ("alice", "dev-harness-kit"))

    def test_dev_harness_kit_fork_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "consumer_fork")
            self.assertEqual(cls.repo_kind, "dev_harness_kit")
            self.assertEqual(cls.recommended_gate, "fork_pr_review_environment")

    def test_consumer_repo_self(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/someorg/myapp.git",
                plugin_owner=None,
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "consumer_self")
            self.assertEqual(cls.repo_kind, "consumer")
            self.assertEqual(cls.recommended_gate, "standard_gates")

    def test_consumer_repo_self_with_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/someorg/myapp.git",
                plugin_owner=None,
                team_maintainers=["alice"],
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "maintainer_self")
            self.assertEqual(cls.recommended_gate, "standard_gates")

    def test_consumer_repo_installed_on_dev_harness_kit_fork(self) -> None:
        """A contributor who ``ci-setup`` installed on their own fork of
        dev-harness-kit. plugin.json is absent (ci-setup consumer) but
        origin matches the canonical dev-kit repo → fork, fail-closed."""
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner=None,
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "consumer_fork")
            self.assertEqual(cls.repo_kind, "consumer")
            self.assertEqual(cls.recommended_gate, "fork_pr_review_environment")

    def test_no_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            with mock.patch.object(
                actor_classifier, "_run", return_value=(1, "", "no remote"),
            ):
                cls = actor_classifier.classify_actor(tdir)
            self.assertEqual(cls.actor_type, "unknown")
            self.assertEqual(cls.repo_kind, "unknown")
            self.assertEqual(cls.recommended_gate, "manual_review")
            self.assertIsNone(cls.remote_owner_repo)

    def test_gh_author_association_trusted(self) -> None:
        """A ``gh_author_association=COLLABORATOR`` injection implies
        maintainer trust regardless of ``team.json``."""
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(
                    tdir, gh_author_association="COLLABORATOR",
                )
            self.assertEqual(cls.actor_type, "maintainer_fork")
            self.assertEqual(cls.recommended_gate, "standard_gates")


class TestIdempotency(unittest.TestCase):
    """Per feedback-tmux-long-running-safety.md: hooks must be idempotent."""

    def test_classify_twice_same_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                first = actor_classifier.classify_actor(tdir)
                second = actor_classifier.classify_actor(tdir)
            self.assertEqual(first.to_dict(), second.to_dict())


class TestCli(unittest.TestCase):
    """CLI shape — JSON output matches the dataclass."""

    def test_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/sh-ai-x/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(actor_classifier, "_run", return_value=(0, "", "")):
                rc = actor_classifier.main(
                    ["--root", str(tdir), "--json", "--head-branch", "feat/y"]
                )
            self.assertEqual(rc, 0)

    def test_human_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(actor_classifier, "_run", return_value=(0, "", "")):
                rc = actor_classifier.main(["--root", str(tdir)])
            self.assertEqual(rc, 0)


class TestBreadcrumb(unittest.TestCase):
    """The hook writes ``.dev-kit/.pr-route.json`` for CI to consume."""

    def test_write_breadcrumb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(
                actor_classifier, "_run", side_effect=_run_mock(tdir)
            ):
                cls = actor_classifier.classify_actor(tdir)
                path = actor_classifier.write_breadcrumb(cls, tdir)
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["actor_type"], "consumer_fork")
            self.assertEqual(payload["recommended_gate"], "fork_pr_review_environment")

    def test_write_breadcrumb_readonly_filesystem(self) -> None:
        """Best-effort: OSError → None, no raise."""
        with tempfile.TemporaryDirectory() as tmp:
            tdir = _make_repo(
                Path(tmp),
                origin="https://github.com/eve/dev-harness-kit.git",
                plugin_owner="sh-ai-x",
                team_maintainers=None,
            )
            with mock.patch.object(actor_classifier, "_run", return_value=(0, "", "")):
                cls = actor_classifier.classify_actor(tdir)
            with mock.patch("pathlib.Path.write_text", side_effect=OSError("read-only")):
                result = actor_classifier.write_breadcrumb(cls, tdir)
            self.assertIsNone(result)


class TestConstants(unittest.TestCase):
    """Pin the public constants — these are the wire contract for
    ``hooks/pr-create-route.sh`` and any consumer of the breadcrumb."""

    def test_known_dev_harness_kit_origins(self) -> None:
        self.assertIn(
            "sh-ai-x/dev-harness-kit", actor_classifier.KNOWN_DEV_HARNESS_KIT_ORIGINS,
        )

    def test_known_dev_harness_kit_repo_names(self) -> None:
        self.assertIn(
            "dev-harness-kit", actor_classifier.KNOWN_DEV_HARNESS_KIT_REPO_NAMES,
        )

    def test_trusted_associations(self) -> None:
        self.assertEqual(
            actor_classifier.TRUSTED_ASSOCIATIONS,
            frozenset({"OWNER", "MEMBER", "COLLABORATOR"}),
        )


if __name__ == "__main__":
    unittest.main()
