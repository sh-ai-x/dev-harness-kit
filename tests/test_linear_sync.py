#!/usr/bin/env python3
"""tests/test_linear_sync.py — Regression tests for tools/linear_sync.py.

Covers #539 acceptance criteria:
  - configured  → create / update issue
  - disabled    → no-op, exit 0
  - unavailable → no-op, exit 0 (no LINEAR_API_KEY)
  - stale handoff → replaces the handoff, creates a new issue
  - duplicate issue → reuses the existing issue (no flood)

The Linear GraphQL endpoint is mocked so the suite is hermetic and
runs offline. urllib.request.urlopen is patched per-test.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import linear_sync  # noqa: E402  (sys.path tweak above)


@contextmanager
def _fake_repo(linear_api_key: str | None = "set-at-runtime",
               enabled_json: dict | None = None,
               handoff: dict | None = None,
               branch: str = "feat/issue-539-linear-autosync",
               repo_dirname: str = "fake-worktree",
               commit_subject: str = "",
               main_checkout: bool = False,
               linear_config: dict | None = None):
    """Run linear_sync against a temp directory with controlled config."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / repo_dirname
        repo.mkdir(parents=True, exist_ok=True)
        (repo / ".dev-kit" / "hand-off" / "linear").mkdir(parents=True)
        env = {
            "HOME": str(repo),
            "PATH": os.environ.get("PATH", ""),
        }
        if linear_api_key:
            env["LINEAR_API_KEY"] = linear_api_key
        if enabled_json is not None:
            (repo / ".dev-kit" / ".enabled.json").write_text(
                json.dumps(enabled_json), encoding="utf-8",
            )
        if linear_config is not None:
            (repo / ".dev-kit" / "linear-config.json").write_text(
                json.dumps(linear_config), encoding="utf-8",
            )
        if handoff is not None:
            slug = "main" if main_checkout else repo_dirname
            (repo / ".dev-kit" / "hand-off" / "linear" / f"{slug}.json").write_text(
                json.dumps(handoff), encoding="utf-8",
            )
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(linear_sync, "_repo_root", return_value=repo), \
             mock.patch.object(linear_sync, "_current_branch", return_value=branch), \
             mock.patch.object(linear_sync, "_is_main_checkout", return_value=main_checkout), \
             mock.patch.object(linear_sync, "_latest_commit_subject", return_value=commit_subject), \
             mock.patch.object(linear_sync, "_resolve_team_id", return_value="team-test"), \
             mock.patch.object(linear_sync, "_last_commit_info",
                               return_value={"sha": "", "short": "", "subject": "",
                                              "author": "", "date": ""}), \
             mock.patch.object(linear_sync, "_changed_files_since", return_value=[]), \
             mock.patch.object(linear_sync, "_commit_body", return_value=""):
            yield repo


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mocked_urlopen(handler):
    """Wrap handler(payload) → dict into a urlopen mock.

    Unknown queries (those the test handler did not explicitly
    handle) get a benign empty `{"data": {}}` response. This keeps
    the test focused on the assertions it cares about while letting
    newly-added queries (e.g. `workflowStates` for state transitions)
    gracefully no-op instead of erroring out the test.
    """
    def _urlopen(req, timeout=5):  # noqa: ARG001
        body = json.loads(req.data.decode("utf-8"))
        try:
            result = handler(body)
        except (AssertionError, KeyError):
            result = {"data": {}}
        return _FakeResponse(json.dumps(result).encode("utf-8"))
    return _urlopen


class TestLinearSync(unittest.TestCase):
    def test_disabled_when_no_env_and_no_enabled_json(self):
        with _fake_repo(linear_api_key=None, enabled_json=None) as repo:
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.sync(), 0)
                urlopen.assert_not_called()
                self.assertEqual(
                    list((repo / ".dev-kit" / "hand-off" / "linear").glob("*.json")),
                    [],
                )

    def test_disabled_when_enabled_json_linear_off(self):
        with _fake_repo(enabled_json={"mcp": {"linear": "off"}}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.sync(), 0)
                urlopen.assert_not_called()

    def test_enabled_when_env_var_present(self):
        with _fake_repo(linear_api_key="set-at-runtime", enabled_json=None,
                        handoff={"prompt": "implement auto-sync"},
                        commit_subject="implement auto-sync") as repo:
            calls = []

            def handler(payload):
                calls.append(payload)
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": []}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {"id": "iss-1", "identifier": "DEMO-1"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "created")
            self.assertIn("DEMO-1", handoff["issue"])
            self.assertEqual(handoff["branch"], "feat/issue-539-linear-autosync")
            self.assertGreaterEqual(len(calls), 2)  # project lookup + issue create

    def test_reuses_existing_issue_in_same_scope(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement auto-sync"},
                        commit_subject="implement auto-sync") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    # Issue already exists with the same scope marker.
                    return {"data": {"issues": {"nodes": [
                        {"id": "iss-existing", "identifier": "DEMO-2", "description": "<!-- scope:feat/issue-539-linear-autosync::implement auto sync -->\nold body"},
                    ]}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"issue": {"id": "iss-existing"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "updated")
            self.assertIn("iss-existing", handoff["issue"])

    def test_stale_handoff_with_different_prompt_creates_new_issue(self):
        """#539: 'A present, old, closed, or unrelated handoff is not
        sufficient evidence.' A different prompt = different scope =
        new issue, even if the handoff still points at one."""
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement old unrelated feature"},
                        commit_subject="implement new unrelated feature") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    # No match — the old handoff's issue is not in the current scope.
                    return {"data": {"issues": {"nodes": [
                        {"id": "iss-stale", "identifier": "DEMO-STALE", "description": "<!-- scope:feat/x::old unrelated task -->\nstale body"},
                    ]}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {"id": "iss-new", "identifier": "DEMO-9"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "created")
            self.assertIn("DEMO-9", handoff["issue"])

    def test_handoff_carries_priority_meta(self):
        """Every hand-off write stamps a `_meta` block declaring
        priority 2 and the Linear API as the source of truth, so a
        reader can tell at a glance that the file is a cache."""
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement auto-sync"},
                        commit_subject="implement auto-sync") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": []}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {"id": "iss-1", "identifier": "DEMO-1"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            payload = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertIn("_meta", payload)
            self.assertEqual(payload["_meta"]["priority"], 2)
            self.assertEqual(payload["_meta"]["source_of_truth"], "linear_api")
            self.assertEqual(payload["_meta"]["kind"], "cache")
            self.assertEqual(payload["_meta"]["written_by"], "tools/linear_sync.py")

    def test_skips_read_only_prompts(self):
        """#539: 'Do not invoke Linear for read-only work such as
        inspect, review, security, or code-viz unless the user
        explicitly requests registration.'"""
        with _fake_repo(linear_api_key="set-at-runtime", handoff={"prompt": "ls -la"}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.sync(), 0)
                urlopen.assert_not_called()

    def test_transport_failure_is_non_blocking(self):
        """#539: 'Linear failures are non-blocking for implicit
        workflow calls.' A urllib failure must not raise."""
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement auto-sync"}):
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=OSError("network down"),
            ):
                self.assertEqual(linear_sync.sync(), 0)  # does not raise

    def test_urlopen_timeout_is_non_blocking(self):
        """Cold-start resilience (#583 followup): a transport failure
        (DNS/TLS handshake) must not block the Edit. Under the typed
        LinearTransportError contract, sync() catches and bails
        non-blocking per the #539 contract — the Edit is never blocked
        by a flaky first request, but no handoff is written either
        (the previous round-flow kept the stale handoff intact)."""
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement x"}) as repo:
            call_count = {"n": 0}

            def flaky(req, timeout=15):  # noqa: ARG001
                call_count["n"] += 1
                raise urllib.error.URLError("simulated cold start")

            with mock.patch.object(linear_sync, "_resolve_prompt",
                                    return_value="implement x"), \
                 mock.patch("urllib.request.urlopen", flaky):
                self.assertEqual(linear_sync.sync(), 0)
            # sync() bails after the first transport failure — no retry,
            # no handoff rewrite. The Edit is unblocked, but stale handoff
            # (if any) is left untouched for the next non-flaky round.
            self.assertEqual(
                call_count["n"], 1,
                "sync() should bail after the first transport failure",
            )
            handoff_path = (
                repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json"
            )
            # The original handoff was created by _fake_repo; sync() must
            # NOT have overwritten it with a 'created'/'updated' entry
            # when transport failed.
            self.assertTrue(handoff_path.exists())
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            self.assertNotIn(handoff.get("action"), ("created", "updated"))

    def test_linear_query_transport_raises_linear_transport_error(self):
        """#transport-catch contract: HTTPError/SSLError -> RuntimeError; other
        transport errors (URLError/TimeoutError) -> LinearTransportError so the
        auto-sync non-blocking flow can catch-and-continue; CLI surface flow
        surfaces a real stderr diagnostic instead of the misleading 'no issues
        match' empty-state message."""
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "set-at-runtime"}):
            # 1) HTTPError -> RuntimeError (regression guard for TLS/auth codes)
            err = urllib.error.HTTPError(
                "https://api.linear.app/graphql", 503, "Service Unavailable",
                {"Content-Type": "application/json"},
                io.BytesIO(b'{"err":"x"}'),
            )
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(RuntimeError) as cm:
                    linear_sync._linear_query("q", {})
                self.assertNotIsInstance(
                    cm.exception, linear_sync.LinearTransportError
                )
            # 2) bare URLError -> LinearTransportError
            with mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.URLError("dns down")):
                with self.assertRaises(linear_sync.LinearTransportError):
                    linear_sync._linear_query("q", {})
            # 3) TimeoutError -> LinearTransportError
            with mock.patch("urllib.request.urlopen",
                            side_effect=TimeoutError("socket.timeout")):
                with self.assertRaises(linear_sync.LinearTransportError):
                    linear_sync._linear_query("q", {})
            # 4) SSLError wrapped in URLError -> RuntimeError (TLS preserved)
            ssl_err = urllib.error.URLError(
                reason=__import__("ssl").SSLError("certificate verify failed")
            )
            with mock.patch("urllib.request.urlopen", side_effect=ssl_err):
                with self.assertRaises(RuntimeError) as cm:
                    linear_sync._linear_query("q", {})
                self.assertIn("TLS", str(cm.exception))

    def test_repo_name_falls_back_to_directory(self):
        """Canonical repo name = directory basename, matching #539's
        'project named exactly after the repository' rule."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "dev-harness-kit"
            nested.mkdir()
            self.assertEqual(linear_sync._repo_name(nested), "dev-harness-kit")

    def test_issue_body_is_structured(self):
        """Linear issues should land with a consistent template
        (Summary / Context / Files / Acceptance / Test plan / Related)
        and a leading scope marker so future syncs reuse the same issue."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            scope = "fix/x::test"
            with mock.patch.object(linear_sync, "_changed_files_since",
                                   return_value=[("a.py", 10, 2), ("b.md", 4, 0)]), \
                 mock.patch.object(linear_sync, "_last_commit_info",
                                   return_value={"sha": "abcdef0", "short": "abcdef0",
                                                  "subject": "implement x",
                                                  "author": "Claude", "date": "1 minute ago"}):
                body = linear_sync._build_issue_body(
                    prompt="implement feature x",
                    branch="fix/x",
                    repo=repo,
                    scope=scope,
                )
            # Scope marker must be the very first line so _find_issue
            # can detect reuse by prefix match.
            self.assertTrue(body.startswith(f"<!-- scope:{scope} -->"))
            for section in (
                "## Summary",
                "## Context",
                "## Files changed",
                "## Test plan",
                "## Related",
            ):
                self.assertIn(section, body, f"missing section: {section}")
            self.assertIn("**Branch:**", body)
            self.assertIn("**Worktree slug:**", body)
            self.assertIn("**Auto-synced at:**", body)
            self.assertIn("`a.py`", body)
            self.assertIn("`abcdef0`", body)

    def test_issue_body_omits_optional_sections_when_unavailable(self):
        """`## Files changed` and the commit line should be absent
        when running outside a git checkout (e.g. from a unit test)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            body = linear_sync._build_issue_body(
                prompt="do thing",
                branch="fix/x",
                repo=repo,
                scope="fix/x::do thing",
            )
            self.assertNotIn("## Files changed", body)
            self.assertNotIn("**Last commit:**", body)
            self.assertNotIn("- PR:", body)
            # Required sections still present.
            self.assertIn("## Summary", body)
            self.assertIn("## Context", body)
            self.assertIn("## Test plan", body)
            self.assertIn("## Related", body)

    def test_issue_body_includes_pr_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            pr = {
                "url": "https://github.com/owner/repo/pull/42",
                "number": "42",
                "title": "fix thing",
                "state": "OPEN",
                "draft": "false",
            }
            with mock.patch.object(linear_sync, "_detect_pr", return_value=pr):
                body = linear_sync._build_issue_body(
                    prompt="x", branch="fix/x", repo=repo, scope="fix/x::x",
                )
            self.assertIn("- PR: [#42 (open)](https://github.com/owner/repo/pull/42)", body)
            self.assertIn("fix thing", body)

    def test_detect_pr_returns_none_when_gh_missing(self):
        with mock.patch("subprocess.check_output",
                        side_effect=FileNotFoundError("gh not found")):
            self.assertIsNone(linear_sync._detect_pr(Path("/tmp")))

    def test_detect_pr_returns_none_on_gh_error(self):
        err = subprocess.CalledProcessError(1, "gh", b"")
        with mock.patch("subprocess.check_output", side_effect=err):
            self.assertIsNone(linear_sync._detect_pr(Path("/tmp")))

    def test_issue_body_appends_notes_section(self):
        """Operator-written `notes` in linear-config.json land as a
        '## Notes' section so Korean narrative (or any free-form
        context) survives into the Linear issue."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / "linear-config.json").write_text(
                json.dumps({"enabled": True, "notes": "## 작업 메모\n- 한글 컨텍스트"}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                body = linear_sync._build_issue_body(
                    prompt="x", branch="fix/x", repo=repo, scope="fix/x::x",
                )
            self.assertIn("## Notes", body)
            self.assertIn("## 작업 메모", body)
            self.assertIn("한글 컨텍스트", body)
            # Notes must come BEFORE Related so the auto-link block
            # stays at the end.
            self.assertLess(body.index("## Notes"), body.index("## Related"))

    def test_issue_body_extracts_acceptance_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            prompt = (
                "do the thing\n"
                "- [ ] first criterion\n"
                "- [x] already done item\n"
                "  - [ ] indented criterion\n"
            )
            criteria = linear_sync._extract_acceptance_criteria(prompt, "")
            self.assertIn("first criterion", criteria)
            self.assertIn("already done item", criteria)
            self.assertIn("indented criterion", criteria)

    def test_enabled_json_auto_state(self):
        with _fake_repo(linear_api_key=None,
                        enabled_json={"mcp": {"linear": "auto"}}):
            with mock.patch("urllib.request.urlopen") as urlopen:
                # No handoff, no prompt → no-op without API call.
                self.assertEqual(linear_sync.sync(), 0)
                urlopen.assert_not_called()

    def test_prompt_falls_back_to_commit_subject(self):
        """#539 follow-up: when the handoff has no `prompt`, derive
        the task description from the latest commit subject instead
        of bailing out."""
        with _fake_repo(linear_api_key="set-at-runtime", handoff=None,
                        commit_subject="implement linear auto-sync") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": []}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {"id": "iss-c", "identifier": "DEMO-7"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["prompt"], "implement linear auto-sync")
            self.assertIn("DEMO-7", handoff["issue"])

    def test_stale_handoff_prompt_does_not_shadow_new_commit(self):
        """Adversarial review [high]: `_resolve_prompt` must NOT prefer
        a stale `handoff.prompt` from a previous task. When the
        operator moves to a new task in the same worktree (new
        commit, same branch), the scope must follow the new commit,
        not the cached prompt — otherwise the API lookup updates
        the previous task's issue instead of creating/selecting a
        new one.
        """
        with _fake_repo(
            linear_api_key="set-at-runtime",
            # Old task's prompt is still in the handoff.
            handoff={"prompt": "implement OLD task", "issue": "OLD-1"},
            # New task has a fresh commit subject.
            commit_subject="implement NEW task",
        ) as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    # No existing issue with the NEW scope.
                    return {"data": {"issues": {"nodes": []}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {"id": "iss-new", "identifier": "NEW-1"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            # The new commit subject wins, the old prompt is shadowed.
            self.assertEqual(handoff["prompt"], "implement NEW task")
            self.assertEqual(handoff["action"], "created")
            self.assertIn("NEW-1", handoff["issue"])
            self.assertNotIn("OLD-1", handoff["issue"])

    def test_worktree_config_explicit_off_blocks_sync(self):
        """A worktree that has run `linear off` must not sync even
        if `LINEAR_API_KEY` is set."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / "linear-config.json").write_text(
                json.dumps({"enabled": False, "project_name": "x", "team_id": ""}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "PATH": os.environ.get("PATH", "")}, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    self.assertEqual(linear_sync.sync(), 0)
                    urlopen.assert_not_called()

    def test_env_file_loads_linear_api_key(self):
        """`.dev-kit/.env.linear` (untracked) is a fallback for
        users who don't want the key in their shell rc-file."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / ".env.linear").write_text(
                "# comment\n"
                "LINEAR_API_KEY=file-token-xyz\n"
                "OTHER_VAR=kept\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(repo)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "file-token-xyz")
                self.assertEqual(os.environ.get("OTHER_VAR"), "kept")

    def test_env_file_does_not_overwrite_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / ".env.linear").write_text(
                "LINEAR_API_KEY=file-token\n", encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(repo),
                   "LINEAR_API_KEY": "shell-token"}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                # Shell env wins.
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "shell-token")

    def test_env_file_strips_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / ".env.linear").write_text(
                "LINEAR_API_KEY=\"abc123\"  # trailing comment\n"
                "PLAIN=value # also a comment\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(repo)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "abc123")
                self.assertEqual(os.environ.get("PLAIN"), "value")


    # ---- user-scope env loader --------------------------------------------

    def test_user_scope_env_loads_linear_api_key(self):
        """`~/.config/dev-kit/.env` feeds LINEAR_API_KEY when no shell value."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            (fake_home / ".config" / "dev-kit").mkdir(parents=True)
            (fake_home / ".config" / "dev-kit" / ".env").write_text(
                "# comment\n"
                "LINEAR_API_KEY=user-scope-token\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "user-scope-token")

    def test_user_scope_env_filters_non_linear_keys(self):
        """Generic `~/.config/dev-kit/.env` only injects LINEAR_* keys."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            (fake_home / ".config" / "dev-kit").mkdir(parents=True)
            (fake_home / ".config" / "dev-kit" / ".env").write_text(
                "LINEAR_API_KEY=set-at-runtime\n"
                "LINEAR_TEAM_ID=team-7\n"
                "SOME_OTHER_KEY=should-not-leak\n"
                "GH_TOKEN=set-at-runtime\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(
                    os.environ.get("LINEAR_API_KEY"), "set-at-runtime"
                )
                self.assertEqual(os.environ.get("LINEAR_TEAM_ID"), "team-7")
                self.assertIsNone(os.environ.get("SOME_OTHER_KEY"))
                self.assertIsNone(os.environ.get("GH_TOKEN"))

    def test_user_scope_env_overrides_worktree_file(self):
        """User-scope `.env` is loaded first, so per-worktree file is ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / ".env.linear").write_text(
                "LINEAR_API_KEY=worktree-token\n", encoding="utf-8",
            )
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            (fake_home / ".config" / "dev-kit").mkdir(parents=True)
            (fake_home / ".config" / "dev-kit" / ".env").write_text(
                "LINEAR_API_KEY=user-scope-token\n", encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "user-scope-token")

    def test_user_scope_env_respects_xdg_config_home(self):
        """`$XDG_CONFIG_HOME/dev-kit/.env` wins over the default home path."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            xdg = Path(tmp) / "xdg"
            xdg.mkdir()
            (xdg / "dev-kit").mkdir()
            (xdg / "dev-kit" / ".env").write_text(
                "LINEAR_API_KEY=xdg-token\n", encoding="utf-8",
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(fake_home),
                "XDG_CONFIG_HOME": str(xdg),
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "xdg-token")

    def test_user_scope_env_quotes_and_comments_strip(self):
        """User-scope file honors the same quote/comment rules as per-worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            (fake_home / ".config" / "dev-kit").mkdir(parents=True)
            (fake_home / ".config" / "dev-kit" / ".env").write_text(
                "LINEAR_API_KEY=\"abc123\"  # trailing\n"
                "LINEAR_TEAM_ID=team  # team comment\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertEqual(os.environ.get("LINEAR_API_KEY"), "abc123")
                self.assertEqual(os.environ.get("LINEAR_TEAM_ID"), "team")

    def test_user_scope_env_no_keys_is_noop(self):
        """Empty user-scope file is a silent no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            fake_home = Path(tmp) / "fakehome"
            fake_home.mkdir()
            (fake_home / ".config" / "dev-kit").mkdir(parents=True)
            (fake_home / ".config" / "dev-kit" / ".env").write_text(
                "OTHER_KEY=ignored\n", encoding="utf-8",
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo):
                linear_sync._load_env_file(repo)
                self.assertIsNone(os.environ.get("LINEAR_API_KEY"))
                self.assertIsNone(os.environ.get("OTHER_KEY"))

    # ---- `list` subcommand helpers ----------------------------------------

    def test_parse_list_args_defaults(self):
        from linear_sync import _parse_list_args
        out = _parse_list_args([])
        self.assertEqual(out["state"], None)
        self.assertEqual(out["team"], None)
        self.assertIsNone(out["project"])
        self.assertFalse(out["all_projects"])
        self.assertEqual(out["assignee"], None)
        self.assertEqual(out["limit"], 25)

    def test_parse_list_args_overrides(self):
        from linear_sync import _parse_list_args
        out = _parse_list_args([
            "--state=Backlog",
            "--team=SHO",
            "--project=dev-harness-kit",
            "--all-projects",
            "--assignee=me",
            "--limit=10",
        ])
        self.assertEqual(out["state"], "Backlog")
        self.assertEqual(out["team"], "SHO")
        self.assertEqual(out["project"], "dev-harness-kit")
        self.assertTrue(out["all_projects"])
        self.assertEqual(out["assignee"], "me")
        self.assertEqual(out["limit"], 10)

    def test_parse_list_args_all_projects_alone(self):
        from linear_sync import _parse_list_args
        out = _parse_list_args(["--all-projects"])
        self.assertTrue(out["all_projects"])
        self.assertIsNone(out["project"])

    def test_parse_list_args_clamps_limit(self):
        from linear_sync import _parse_list_args
        self.assertEqual(_parse_list_args(["--limit=0"])["limit"], 1)
        self.assertEqual(_parse_list_args(["--limit=999"])["limit"], 100)
        self.assertEqual(_parse_list_args(["--limit=notanumber"])["limit"], 25)

    def test_list_query_no_filters(self):
        from linear_sync import _list_query
        q, v = _list_query({"state": None, "team": None, "assignee": None, "limit": 25})
        self.assertIn("issues(first: $first", q)
        self.assertNotIn("filter:", q)
        self.assertNotIn("$state", q)
        self.assertEqual(v["first"], 25)

    def test_list_query_with_state_filter(self):
        from linear_sync import _list_query
        q, v = _list_query({"state": "Backlog", "team": None, "assignee": None, "limit": 5})
        self.assertIn("state: { name: { eq: $state } }", q)
        self.assertIn("$state: String", q)
        self.assertEqual(v["state"], "Backlog")
        self.assertNotIn("teamKey", v)

    def test_list_query_with_team_and_assignee(self):
        from linear_sync import _list_query
        q, v = _list_query({
            "state": None, "team": "SHO", "assignee": "u-123", "limit": 7,
        })
        self.assertIn("team: { key: { eq: $teamKey } }", q)
        self.assertIn("assignee: { id: { eq: $assigneeId } }", q)
        self.assertEqual(v["teamKey"], "SHO")
        self.assertEqual(v["assigneeId"], "u-123")
        self.assertNotIn("state", v)

    def test_list_query_with_project_filter(self):
        from linear_sync import _list_query
        q, v = _list_query({
            "state": None, "team": None, "project": "dev-harness-kit", "all_projects": False,
            "assignee": None, "limit": 5,
        })
        self.assertIn("project: { name: { eq: $projectName } }", q)
        self.assertIn("$projectName: String", q)
        self.assertEqual(v["projectName"], "dev-harness-kit")
        self.assertIn("project { name }", q)

    def test_list_query_all_projects_drops_project_filter(self):
        from linear_sync import _list_query
        q, v = _list_query({
            "state": None, "team": "SHO", "project": "dev-harness-kit", "all_projects": True,
            "assignee": None, "limit": 5,
        })
        self.assertNotIn("project: {", q)
        self.assertNotIn("$projectName", q)
        self.assertNotIn("projectName", v)

    def test_list_query_with_project_and_all_projects_ignored(self):
        from linear_sync import _list_query
        # --all-projects suppresses the project filter even when --project is
        # also set: this is the documented opt-out. cmd_list never injects the
        # project default when --all-projects is set, so passing both is a
        # user-driven combination; the result is "no project filter".
        q, v = _list_query({
            "state": None, "team": None, "project": "hermes", "all_projects": True,
            "assignee": None, "limit": 5,
        })
        self.assertNotIn("project: {", q)
        self.assertNotIn("projectName", v)

    def test_format_issue_row_columns(self):
        from linear_sync import _format_issue_row
        row = _format_issue_row({
            "identifier": "SHO-153",
            "title": "Unify babysit-pr and GitHub auto-fix behind one repair coordinator",
            "state": {"name": "In Progress"},
            "priority": 2,
            "updatedAt": "2026-08-03T12:34:56Z",
            "url": "https://linear.app/x",
        })
        self.assertIn("SHO-153", row)
        self.assertIn("In Progress", row)
        self.assertIn("2026-08-03", row)
        self.assertIn("Unify babysit-pr", row)
        self.assertIn("pri=2", row)

    def test_format_issue_row_renders_project_when_present(self):
        from linear_sync import _format_issue_row
        row = _format_issue_row({
            "identifier": "SHO-1",
            "title": "x",
            "state": {"name": "Backlog"},
            "priority": 1,
            "updatedAt": "2026-08-03T00:00:00Z",
            "url": "u",
            "project": {"name": "hermes"},
        })
        self.assertIn("[hermes]", row)

    def test_format_issue_row_omits_project_when_missing(self):
        from linear_sync import _format_issue_row
        row = _format_issue_row({
            "identifier": "SHO-1",
            "title": "x",
            "state": {"name": "Backlog"},
            "priority": 1,
            "updatedAt": "2026-08-03T00:00:00Z",
            "url": "u",
        })
        self.assertNotIn("[", row.split("2026-08-03  ")[1].split(" x")[0])

    def test_format_issue_row_handles_missing_fields(self):
        from linear_sync import _format_issue_row
        row = _format_issue_row({})
        # No crash, returns a string with "-" for missing priority.
        self.assertIn("pri=-", row)

    def test_worktree_config_project_name_overrides_repo_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wt"
            repo.mkdir()
            (repo / ".dev-kit").mkdir()
            (repo / ".dev-kit" / "linear-config.json").write_text(
                json.dumps({"enabled": True, "project_name": "My Project", "team_id": "team-1"}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "PATH": os.environ.get("PATH", "")}, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo), \
                 mock.patch.object(linear_sync, "_is_main_checkout", return_value=False):
                self.assertEqual(linear_sync._project_name_override(repo), "My Project")
                self.assertEqual(linear_sync._team_id_override(repo), "team-1")


class TestLinearCLI(unittest.TestCase):
    def _run_cli(self, *args, repo_dirname: str = "wt", env_extra: dict | None = None):
        """Run the CLI in a fresh temp worktree.

        Returns ``(repo, code, stdout, stderr)`` after the temp dir is
        torn down. Tests that need to inspect files written by the CLI
        must do so via ``_read_config(repo)`` (which preserves the
        snapshot) before the helper exits.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / repo_dirname
            repo.mkdir(parents=True)
            (repo / ".dev-kit").mkdir()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(repo)}
            if env_extra:
                env.update(env_extra)
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(linear_sync, "_repo_root", return_value=repo), \
                 mock.patch.object(linear_sync, "_is_main_checkout", return_value=False):
                import contextlib
                from io import StringIO
                buf_out, buf_err = StringIO(), StringIO()
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    code = linear_sync.main(list(args))
                stdout = buf_out.getvalue()
                stderr = buf_err.getvalue()
                # Snapshot any config file before the temp dir is torn down.
                config_snapshot = None
                config_path = repo / ".dev-kit" / "linear-config.json"
                if config_path.is_file():
                    config_snapshot = config_path.read_text(encoding="utf-8")
            return repo, code, stdout, stderr, config_snapshot

    @staticmethod
    def _parse_config(snapshot: str | None) -> dict:
        if snapshot is None:
            return {}
        return json.loads(snapshot)

    def test_on_creates_worktree_config(self):
        _, code, out, _, snapshot = self._run_cli("on")
        self.assertEqual(code, 0)
        self.assertIn("linear: on", out)
        cfg = self._parse_config(snapshot)
        self.assertTrue(cfg["enabled"])

    def test_off_disables(self):
        _, code, _, _, snapshot = self._run_cli("off")
        self.assertEqual(code, 0)
        cfg = self._parse_config(snapshot)
        self.assertFalse(cfg["enabled"])

    def test_project_name_persists(self):
        _, code, out, _, snapshot = self._run_cli("project-name", "My Linear Project")
        self.assertEqual(code, 0)
        cfg = self._parse_config(snapshot)
        self.assertEqual(cfg["project_name"], "My Linear Project")
        self.assertIn("linear: project-name=My Linear Project", out)

    def test_status_prints_resolved_state(self):
        _, code, out, _, _ = self._run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("resolved_project", out)
        self.assertIn("linear_api_key_set", out)

    def test_setup_prints_checklist(self):
        _, code, out, _, _ = self._run_cli("setup")
        self.assertEqual(code, 0)
        self.assertIn("LINEAR_API_KEY", out)
        self.assertIn("linear_sync.py on", out)
        self.assertIn("project-name", out)

    def test_unknown_command_exits_2(self):
        _, code, out, _, _ = self._run_cli("bogus")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", out)

    def test_list_default_scopes_to_active_repo(self):
        """`list` (no --project) auto-resolves the active repo's project
        and surfaces a stderr hint. Verifies the default-scope branch
        in `_cmd_list`."""
        captured = {}

        def fake_query(query, variables):
            captured["query"] = query
            captured["variables"] = variables
            return {
                "issues": {
                    "nodes": [{
                        "identifier": "SHO-1",
                        "title": "x",
                        "state": {"name": "Backlog"},
                        "priority": 1,
                        "updatedAt": "2026-08-03T00:00:00Z",
                        "url": "u",
                        "project": {"name": "dev-harness-kit"},
                    }],
                },
            }

        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "PATH": os.environ.get("PATH", "")}, clear=True), \
             mock.patch.object(linear_sync, "_is_main_checkout", return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "wt"
                repo.mkdir()
                (repo / ".dev-kit").mkdir()
                with mock.patch.object(linear_sync, "_repo_root", return_value=repo), \
                     mock.patch.object(linear_sync, "_repo_name", return_value="wt"), \
                     mock.patch.object(linear_sync, "_linear_query", side_effect=fake_query):
                    import contextlib
                    from io import StringIO
                    buf_out, buf_err = StringIO(), StringIO()
                    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                        code = linear_sync.main(["list", "--limit=5"])
        self.assertEqual(code, 0)
        # The auto-resolved project name flows into the GraphQL filter
        self.assertIn("projectName", captured["variables"])
        self.assertEqual(captured["variables"]["projectName"], "wt")
        # A stderr hint tells the user we scoped the list
        self.assertIn("scoped to project", buf_err.getvalue())
        # The row includes the project name from the node (the fake_query
        # returns project.name="dev-harness-kit", so the row's [project]
        # column reflects what Linear returned, while the GraphQL filter
        # was scoped to the auto-resolved "wt").
        self.assertIn("[dev-harness-kit]", buf_out.getvalue())

    def test_list_all_projects_skips_default_scope(self):
        captured = {}
        def fake_query(query, variables):
            captured["variables"] = variables
            return {"issues": {"nodes": []}}

        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "PATH": os.environ.get("PATH", "")}, clear=True),              mock.patch.object(linear_sync, "_is_main_checkout", return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "wt"
                repo.mkdir()
                (repo / ".dev-kit").mkdir()
                with mock.patch.object(linear_sync, "_repo_root", return_value=repo),                      mock.patch.object(linear_sync, "_linear_query", side_effect=fake_query):
                    import contextlib
                    from io import StringIO
                    buf_out, buf_err = StringIO(), StringIO()
                    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                        code = linear_sync.main(["list", "--all-projects", "--limit=5"])
        self.assertEqual(code, 0)
        # --all-projects: no projectName variable is sent
        self.assertNotIn("projectName", captured["variables"])
        self.assertNotIn("scoped to project", buf_err.getvalue())




class TestCompletionSignal(unittest.TestCase):
    """`_is_completion_signal` gates the auto-Done transition."""

    def test_bare_done_returns_true(self):
        self.assertTrue(linear_sync._is_completion_signal("done with the auth refactor"))

    def test_shipped_returns_true(self):
        self.assertTrue(linear_sync._is_completion_signal("shipped"))

    def test_completed_returns_true(self):
        self.assertTrue(linear_sync._is_completion_signal("all tests completed"))

    def test_completion_verb_alone_signals_done(self):
        # Completion verb is the signal; work-verb-wins was dropped because
        # false-negatives (e.g. "done with the auth refactor") were common.
        self.assertTrue(linear_sync._is_completion_signal("shipped X and now implement Y"))

    def test_empty_returns_false(self):
        self.assertFalse(linear_sync._is_completion_signal(""))

    def test_read_only_returns_false(self):
        self.assertFalse(linear_sync._is_completion_signal("review the diff"))


class TestWorkSignal(unittest.TestCase):
    """`_is_work_signal` gates the auto-In-progress transition."""

    def test_implement_returns_true(self):
        self.assertTrue(linear_sync._is_work_signal("implement the new sync"))

    def test_build_returns_true(self):
        self.assertTrue(linear_sync._is_work_signal("build the second part"))

    def test_fix_returns_false(self):
        # `fix` is excluded — it usually applies to cleanup, not new starting work.
        self.assertFalse(linear_sync._is_work_signal("fix the leftover test"))

    def test_update_returns_false(self):
        # `update` is excluded for the same reason.
        self.assertFalse(linear_sync._is_work_signal("update the docs"))

    def test_empty_returns_false(self):
        self.assertFalse(linear_sync._is_work_signal(""))


class TestAutoOpen(unittest.TestCase):
    """Step 6 of sync(): create new issue in Todo state."""

    def test_first_edit_creates_issue_in_todo(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement auto-sync"},
                        commit_subject="implement auto-sync") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": []}}}
                if "workflowStates" in q:
                    return {"data": {"workflowStates": {"nodes": [
                        {"id": "state-todo", "name": "Todo"},
                        {"id": "state-done", "name": "Done"},
                    ]}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {
                        "id": "iss-1", "identifier": "DEMO-1", "state": {"name": "Todo"},
                    }}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "created")
            self.assertEqual(handoff["state"], "Todo")
            self.assertIn("DEMO-1", handoff["issue"])

    def test_falls_back_to_backlog_when_no_todo_column(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo"},
                        commit_subject="implement foo again") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": []}}}
                if "workflowStates" in q:
                    # Only Backlog — no Todo column.
                    return {"data": {"workflowStates": {"nodes": [
                        {"id": "state-back", "name": "Backlog"},
                        {"id": "state-done", "name": "Done"},
                    ]}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {
                        "id": "iss-1", "identifier": "DEMO-1", "state": {"name": "Backlog"},
                    }}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["state"], "Backlog")  # actual returned state from issueCreate


class TestAutoInProgress(unittest.TestCase):
    """Step 7 of sync(): transition existing issue to In Progress on work signal."""

    def test_subsequent_edit_transitions_to_in_progress(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo", "issue": "DEMO-1 (iss-1)",
                                 "state": "Todo", "branch": "feat/x",
                                 "scope": "feat/x::implement foo", "action": "created"},
                        commit_subject="implement foo continued") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo continued -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "Todo"},
                    }]}}}
                if "workflowStates" in q:
                    return {"data": {"workflowStates": {"nodes": [
                        {"id": "state-prog", "name": "In Progress"},
                    ]}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"success": True,"issue": {"id": "iss-1", "identifier": "DEMO-1", "state": {"name": "In Progress"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["state"], "In Progress")

    def test_already_in_progress_is_idempotent(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo", "issue": "DEMO-1 (iss-1)",
                                 "state": "In Progress", "branch": "feat/x",
                                 "scope": "feat/x::implement foo", "action": "created"},
                        commit_subject="implement foo continued"):
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo continued -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "In Progress"},
                    }]}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"success": True,"issue": {"id": "iss-1", "identifier": "DEMO-1", "state": {"name": "In Progress"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)


class TestAutoDone(unittest.TestCase):
    """Step 5 of sync(): transition to Done on completion verb."""

    def test_done_prompt_transitions_issue(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo", "issue": "DEMO-1 (iss-1)",
                                 "state": "In Progress", "branch": "feat/x",
                                 "scope": "feat/x::implement foo", "action": "updated"},
                        commit_subject="implement foo done") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo done -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "In Progress"},
                    }]}}}
                if "workflowStates" in q:
                    return {"data": {"workflowStates": {"nodes": [
                        {"id": "state-done", "name": "Done"},
                    ]}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"success": True,"issue": {"id": "iss-1", "identifier": "DEMO-1", "state": {"name": "Done"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "completed")
            self.assertEqual(handoff["state"], "Done")
            self.assertIn("completed_at", handoff)

    def test_done_with_noun_work_verb_still_transitions(self):
        # "done with the auth refactor" — "refactor" describes the
        # completed task (noun), not new work. The completion verb
        # is the signal; auto-Done fires.
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo", "issue": "DEMO-1 (iss-1)",
                                 "state": "In Progress", "branch": "feat/x",
                                 "scope": "feat/x::implement foo", "action": "updated"},
                        commit_subject="implement foo done with the refactor") as repo:
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo done with the refactor -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "In Progress"},
                    }]}}}
                if "workflowStates" in q:
                    return {"data": {"workflowStates": {"nodes": [{"id": "state-done", "name": "Done"}]}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"success": True,"issue": {"id": "iss-1", "identifier": "DEMO-1", "state": {"name": "Done"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertEqual(handoff["action"], "completed")
            self.assertEqual(handoff["state"], "Done")


class TestAutoArchiveDuplicates(unittest.TestCase):
    """Step 4 of sync(): archive older duplicates, keep newest."""

    def test_archives_older_keeps_newest(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo again",
                                 "scope": "feat/x::implement foo again",
                                 "branch": "feat/x"},
                        commit_subject="implement foo again") as repo:
            archived: list[str] = []

            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    # Two open issues with the same scope-marker (race).
                    return {"data": {"issues": {"nodes": [
                        {"id": "iss-new", "identifier": "DEMO-2",
                         "description": "<!-- scope:feat/x::implement foo again -->newer",
                         "updatedAt": "2026-08-06T12:00:00Z",
                         "state": {"name": "Todo"}},
                        {"id": "iss-old", "identifier": "DEMO-1",
                         "description": "<!-- scope:feat/x::implement foo again -->older",
                         "updatedAt": "2026-08-06T00:00:00Z",
                         "state": {"name": "Todo"}},
                    ]}}}
                if "issueArchive" in q:
                    archived.append(payload["variables"]["id"])
                    return {"data": {"issueArchive": {"success": True, "entity": {"id": payload["variables"]["id"]}}}}
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"success": True,"issue": {"id": "iss-new", "identifier": "DEMO-2", "state": {"name": "Todo"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            self.assertEqual(archived, ["iss-old"])
            handoff = json.loads(
                (repo / ".dev-kit" / "hand-off" / "linear" / "fake-worktree.json").read_text(encoding="utf-8")
            )
            self.assertIn("DEMO-2", handoff["issue"])

    def test_no_archive_when_single_match(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        handoff={"prompt": "implement foo"},
                        commit_subject="implement foo"):
            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo continued -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "Todo"},
                    }]}}}
                if "issueArchive" in q:
                    raise AssertionError("should not archive when only one match")
                if "issueUpdate" in q:
                    return {"data": {"issueUpdate": {"issue": {"id": "iss-1", "identifier": "DEMO-1", "state": {"name": "Todo"}}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)


class TestEnabledGate(unittest.TestCase):
    """Step 1: hardened `_enabled()` with LINEAR_DEBUG logging."""

    def test_no_sources_returns_false_without_calling_env_loader(self):
        with _fake_repo(linear_api_key=None, enabled_json=None) as repo:
            # Path.home() ignores mocked HOME on macOS (uses pwd db),
            # so the real ~/.config/dev-kit/.env would leak a key.
            # Patch _user_env_path + the per-worktree env to no-op paths.
            bogus_env = repo / "no-such-env"
            bogus_env.mkdir(exist_ok=True)
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(linear_sync, "_user_env_path", return_value=bogus_env / ".env"), \
                 mock.patch.object(linear_sync, "_ENV_FILE_REL", Path("no-such") / ".env.linear"):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    self.assertFalse(linear_sync._enabled())
                    urlopen.assert_not_called()

    def test_env_var_alone_returns_true(self):
        with _fake_repo(linear_api_key="k", enabled_json=None):
            self.assertTrue(linear_sync._enabled())

    def test_linear_config_enabled_false_returns_false(self):
        with _fake_repo(linear_api_key="k",
                        linear_config={"enabled": False}):
            self.assertFalse(linear_sync._enabled())

    def test_linear_config_enabled_true_with_key_returns_true(self):
        with _fake_repo(linear_api_key="k",
                        linear_config={"enabled": True}):
            self.assertTrue(linear_sync._enabled())

    def test_linear_config_enabled_true_without_key_returns_false(self):
        with _fake_repo(linear_api_key=None,
                        linear_config={"enabled": True}):
            self.assertFalse(linear_sync._enabled())

    def test_linear_config_without_enabled_key_falls_through_to_env(self):
        # Defensive: a partial config (no `enabled` key) does NOT enable sync.
        with _fake_repo(linear_api_key="k",
                        linear_config={"project_name": "demo"}):
            self.assertTrue(linear_sync._enabled())

    def test_linear_debug_logs_decision(self):
        with _fake_repo(linear_api_key="k"):
            with mock.patch.dict(os.environ, {"LINEAR_DEBUG": "1"}, clear=False):
                import contextlib
                from io import StringIO
                buf = StringIO()
                with contextlib.redirect_stderr(buf):
                    linear_sync._enabled()
                self.assertIn("[linear-sync] _enabled()=", buf.getvalue())




class TestAutoDoneStateGuard(unittest.TestCase):
    """MAJOR 4: auto-Done must NOT resurrect Canceled/Done issues."""

    def test_done_verb_skips_when_issue_already_canceled(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo", "issue": "DEMO-1 (iss-1)",
                                 "state": "In Progress", "action": "updated",
                                 "scope": "feat/x::implement foo done"},
                        commit_subject="implement foo done"):
            issueUpdate_calls: list[str] = []

            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-1", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo done -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "Canceled"},
                    }]}}}
                if "issueUpdate" in q:
                    issueUpdate_calls.append(q[:50])
                    return {"data": {"issueUpdate": {"success": True, "issue": {"id": "iss-1"}}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            # Canceled is filtered out by dedupe (MINOR 1), so matches is
            # empty and sync() creates a new issue — issueUpdate never
            # transitions the canceled one. The important guarantee:
            # NO stateId mutation fires on the canceled issue.
            state_updates = [c for c in issueUpdate_calls if "stateId" in c]
            self.assertEqual(state_updates, [])


class TestDedupeSkipsTerminal(unittest.TestCase):
    """MINOR 1: _find_all_issues excludes Done/Canceled issues."""

    def test_dedupe_skips_done_issue(self):
        with _fake_repo(linear_api_key="set-at-runtime",
                        branch="feat/x",
                        handoff={"prompt": "implement foo"},
                        commit_subject="implement foo again"):
            archived: list[str] = []

            def handler(payload):
                q = payload["query"]
                if "projects(filter:" in q and "projectCreate" not in q:
                    return {"data": {"projects": {"nodes": [{"id": "proj-1", "name": "demo"}]}}}
                if "issues(filter:" in q:
                    return {"data": {"issues": {"nodes": [{
                        "id": "iss-old", "identifier": "DEMO-1",
                        "description": "<!-- scope:feat/x::implement foo again -->body",
                        "updatedAt": "2026-08-06T00:00:00Z",
                        "state": {"name": "Done"},
                    }]}}}
                if "issueArchive" in q:
                    archived.append(payload["variables"]["id"])
                    return {"data": {"issueArchive": {"success": True}}}
                if "issueCreate" in q:
                    return {"data": {"issueCreate": {"issue": {
                        "id": "iss-new", "identifier": "DEMO-2", "state": {"name": "Todo"},
                    }}}}
                raise AssertionError(f"unexpected query: {q}")

            with mock.patch("urllib.request.urlopen", _mocked_urlopen(handler)):
                self.assertEqual(linear_sync.sync(), 0)
            self.assertEqual(archived, [])


class TestIsRepoOwner(unittest.TestCase):
    """Unit tests for `is_repo_owner` — the gate that decides whether
    the auto-sync hooks (linear-autosync, linear-session-start,
    linear-worktree-create, linear-task-change) fire for the current
    user. The manual CLI path (`/dev-kit:linear`) intentionally does
    not consult this gate.
    """

    def setUp(self):
        # Reset the module-level cache so each test sees a clean state.
        # The cache is process-local; a leftover True/False from a prior
        # test would silently poison the next assertion.
        linear_sync._OWNER_CACHE = {}

    def tearDown(self):
        linear_sync._OWNER_CACHE = {}

    def test_env_var_true_bypasses_detection(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            with mock.patch.dict(os.environ, {"LINEAR_REPO_OWNER_AUTO_SYNC": "1"}, clear=False):
                with mock.patch.object(linear_sync, "_resolve_gh_login") as gh:
                    self.assertTrue(linear_sync.is_repo_owner(repo))
                    gh.assert_not_called()

    def test_env_var_false_bypasses_detection(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            with mock.patch.dict(os.environ, {"LINEAR_REPO_OWNER_AUTO_SYNC": "0"}, clear=False):
                with mock.patch.object(linear_sync, "_resolve_gh_login") as gh:
                    self.assertFalse(linear_sync.is_repo_owner(repo))
                    gh.assert_not_called()

    def test_detection_match_returns_true(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            with mock.patch.dict(os.environ, {}, clear=False):
                # Remove any LINEAR_REPO_OWNER_AUTO_SYNC leftover from the host env.
                os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
                with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="sh-ai-x"), \
                     mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x"):
                    self.assertTrue(linear_sync.is_repo_owner(repo))

    def test_detection_match_is_case_insensitive(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="Sh-AI-X"), \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x"):
                self.assertTrue(linear_sync.is_repo_owner(repo))

    def test_detection_mismatch_returns_false(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="contributor"), \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x"):
                self.assertFalse(linear_sync.is_repo_owner(repo))

    def test_detection_no_gh_returns_false(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value=None), \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x"):
                self.assertFalse(linear_sync.is_repo_owner(repo))

    def test_detection_no_origin_returns_false(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="sh-ai-x"), \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value=None):
                self.assertFalse(linear_sync.is_repo_owner(repo))

    def test_result_is_cached_within_a_process(self):
        with _fake_repo(linear_api_key="set-at-runtime") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="sh-ai-x") as gh, \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x") as origin:
                self.assertTrue(linear_sync.is_repo_owner(repo))
                self.assertTrue(linear_sync.is_repo_owner(repo))
                # Both resolves are called once thanks to the cache.
                self.assertEqual(gh.call_count, 1)
                self.assertEqual(origin.call_count, 1)

    def test_cache_is_keyed_per_repo(self):
        # Two different `repo` paths in the same process must NOT share
        # a cached answer. Regression for the previous module-global
        # cache that was correct by accident (one repo per process)
        # but unsound in principle.
        with _fake_repo(linear_api_key="set-at-runtime", repo_dirname="repo-a") as repo_a, \
             _fake_repo(linear_api_key="set-at-runtime", repo_dirname="repo-b") as repo_b:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "_resolve_gh_login", return_value="sh-ai-x") as gh, \
                 mock.patch.object(linear_sync, "_resolve_origin_owner", return_value="sh-ai-x") as origin:
                # First call: cache miss → resolve fires.
                self.assertTrue(linear_sync.is_repo_owner(repo_a))
                # Second call, different repo: cache miss (different
                # cache key) → resolve fires AGAIN.
                self.assertTrue(linear_sync.is_repo_owner(repo_b))
                # Third call: same as second → cache hit, no resolve.
                self.assertTrue(linear_sync.is_repo_owner(repo_b))
                # Two resolves, one per repo.
                self.assertEqual(gh.call_count, 2)
                self.assertEqual(origin.call_count, 2)


class TestAutoSync(unittest.TestCase):
    """Tests for the owner-gated auto-sync entry point used by
    every Linear auto-trigger hook (linear-autosync,
    linear-session-start, linear-worktree-create, linear-task-change).

    The contract under test:
      - When `is_repo_owner` is False, `auto_sync` bails without
        forking a Linear round-trip (and without writing a handoff).
      - When `is_repo_owner` is True, `auto_sync` delegates to
        `sync()` exactly as before.
      - The manual CLI path (`sync()`) is never gated.
    """

    def setUp(self):
        linear_sync._OWNER_CACHE = {}

    def tearDown(self):
        linear_sync._OWNER_CACHE = {}

    def test_non_owner_bails_silently_without_network(self):
        with _fake_repo(linear_api_key="set-at-runtime", commit_subject="implement foo") as repo:
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=False), \
                 mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.auto_sync(), 0)
                urlopen.assert_not_called()
                # No handoff is written on a gated bail.
                self.assertEqual(
                    list((repo / ".dev-kit" / "hand-off" / "linear").glob("*.json")),
                    [],
                )

    def test_owner_delegates_to_sync(self):
        with _fake_repo(linear_api_key="set-at-runtime", commit_subject="implement foo"):
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=True), \
                 mock.patch.object(linear_sync, "sync", return_value=0) as sync:
                self.assertEqual(linear_sync.auto_sync(), 0)
                sync.assert_called_once()


class TestTaskChangeSync(unittest.TestCase):
    """Tests for the scope-change short-circuit used by
    linear-task-change. The hook is meant to fire ONLY when the
    current scope (branch + latest commit subject) differs from
    the handoff's last-recorded scope. A same-scope prompt is a
    continuation, not a change.
    """

    def setUp(self):
        linear_sync._OWNER_CACHE = {}

    def tearDown(self):
        linear_sync._OWNER_CACHE = {}

    def test_non_owner_bails_silently(self):
        with _fake_repo(linear_api_key="set-at-runtime", commit_subject="implement foo",
                        handoff={"scope": "feat/x::implement foo"}):
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=False), \
                 mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.task_change_sync(), 0)
                urlopen.assert_not_called()

    def test_same_scope_bails_without_network(self):
        with _fake_repo(linear_api_key="set-at-runtime", branch="feat/x",
                        commit_subject="implement foo",
                        handoff={"scope": "feat/x::implement foo"}):
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=True), \
                 mock.patch("urllib.request.urlopen") as urlopen:
                self.assertEqual(linear_sync.task_change_sync(), 0)
                urlopen.assert_not_called()

    def test_changed_scope_triggers_auto_sync(self):
        # Commit subject moved to a new task; handoff still has the old scope.
        with _fake_repo(linear_api_key="set-at-runtime", commit_subject="implement bar",
                        handoff={"scope": "feat/x::implement foo"}):
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=True), \
                 mock.patch.object(linear_sync, "auto_sync", return_value=0) as auto:
                self.assertEqual(linear_sync.task_change_sync(), 0)
                auto.assert_called_once()

    def test_missing_handoff_triggers_auto_sync(self):
        # No prior handoff means the scope is "unknown" → always sync.
        with _fake_repo(linear_api_key="set-at-runtime", commit_subject="implement foo",
                        handoff=None):
            os.environ.pop("LINEAR_REPO_OWNER_AUTO_SYNC", None)
            with mock.patch.object(linear_sync, "is_repo_owner", return_value=True), \
                 mock.patch.object(linear_sync, "auto_sync", return_value=0) as auto:
                self.assertEqual(linear_sync.task_change_sync(), 0)
                auto.assert_called_once()


class TestLinearAutosyncHookCallsAutoSync(unittest.TestCase):
    """Regression: the bash hook now calls `auto-sync` (owner-gated),
    not bare `sync` (ungated). A non-owner must never see a Linear
    round-trip from the Edit|Write path.
    """

    def test_hook_invokes_auto_sync_subcommand(self):
        path = ROOT / "hooks" / "linear-autosync.sh"
        text = path.read_text(encoding="utf-8")
        self.assertIn("auto-sync", text,
                      "linear-autosync.sh must call the owner-gated auto-sync entry point")
        # Defense in depth: the bare subcommand (no owner gate) is
        # still in the file via `if argv[0] == "sync"` in main(), but
        # the hook must invoke `auto-sync` (not bare `sync`).
        self.assertIn("linear_sync.py\" auto-sync", text,
                      "the hook must pass auto-sync as the subcommand argument")

    def test_session_start_hook_invokes_auto_sync(self):
        path = ROOT / "hooks" / "linear-session-start.sh"
        text = path.read_text(encoding="utf-8")
        self.assertIn("linear_sync.py\" auto-sync", text,
                      "the hook must pass auto-sync as the subcommand argument")

    def test_worktree_create_hook_invokes_auto_sync(self):
        path = ROOT / "hooks" / "linear-worktree-create.sh"
        text = path.read_text(encoding="utf-8")
        self.assertIn("linear_sync.py\" auto-sync", text,
                      "the hook must pass auto-sync as the subcommand argument")
        # The hook must parse the bash command for `git worktree add`.
        self.assertIn("git worktree add", text)

    def test_task_change_hook_invokes_task_change_sync(self):
        path = ROOT / "hooks" / "linear-task-change.sh"
        text = path.read_text(encoding="utf-8")
        # task-change-sync is the scope-diff entry point; auto-sync
        # would always fire and defeat the diff.
        self.assertIn("linear_sync.py\" task-change-sync", text,
                      "the hook must pass task-change-sync as the subcommand argument")


if __name__ == "__main__":
    unittest.main(verbosity=2)
