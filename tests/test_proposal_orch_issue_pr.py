"""test_proposal_orch_issue_pr.py — RED evidence for the orchestrator triage lib.

Pins the deterministic engine in lib/proposal_orch_issue_pr.py:

- `classify()` rule-table coverage (label / title / default)
- `score()` baseline + risk/bottleneck modifiers
- `recommend_disposition()` rule precedence
- `bucket_for()` boundary → critical-path mapping
- `compose_yaml()` shape (parses cleanly via parse_proposal_yaml)
- `render_html()` writes YAML + HTML atomically under docs/proposals/
- `_fetch_pr_checks_for()` rollup collapse + failure handling
- Snapshot parsing helpers (`_normalize_pr_safe`, `_normalize_issue`)
- CLI dispatch + gh-availability degradation

The fixture for `gh_runner` is a fake that returns canned JSON per
command prefix; this exercises the pipeline end-to-end without ever
calling the real `gh` CLI.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import yaml

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR.parent))

from lib import proposal_orch_issue_pr as poip  # noqa: E402
from lib import render_proposal_html as rph  # noqa: E402

# ----- Test fixtures ---------------------------------------------------------


def _pr_raw(
    number: int,
    title: str = "",
    body: str = "",
    labels: List[str] | None = None,
    head: str = "feat/example",
    base: str = "main",
    draft: bool = False,
    created: str = "2026-08-01T00:00:00Z",
    updated: str = "2026-08-01T00:00:00Z",
) -> Dict:
    return {
        "number": number,
        "title": title or f"PR #{number}",
        "body": body,
        "state": "OPEN",
        "labels": [{"name": label} for label in (labels or [])],
        "createdAt": created,
        "updatedAt": updated,
        "author": {"login": "octocat"},
        "url": f"https://github.com/sh-ai-x/dev-harness-kit/pull/{number}",
        "isDraft": draft,
        "headRefName": head,
        "baseRefName": base,
    }


def _issue_raw(
    number: int,
    title: str = "",
    body: str = "",
    labels: List[str] | None = None,
    created: str = "2026-08-01T00:00:00Z",
    updated: str = "2026-08-01T00:00:00Z",
) -> Dict:
    return {
        "number": number,
        "title": title or f"Issue #{number}",
        "body": body,
        "state": "OPEN",
        "labels": [{"name": label} for label in (labels or [])],
        "createdAt": created,
        "updatedAt": updated,
        "author": {"login": "octocat"},
        "url": f"https://github.com/sh-ai-x/dev-harness-kit/issues/{number}",
    }


def _gh_fake(prs: List[Dict], issues: List[Dict], pr_checks: Dict[int, Dict] | None = None):
    """Build a fake `_run_gh` that returns canned JSON per command prefix.

    `pr_checks` maps PR number → `{state, files}` response payload; if
    a PR number is missing, the fake returns `[]` rollup (i.e. "none").
    Recognized command shapes:
      - `["pr", "list", ...]`     → prs
      - `["issue", "list", ...]`  → issues
      - `["pr", "view", <N>, ...]` → rollup for that PR
    """
    pr_checks = pr_checks or {}

    def fake(args: List[str]) -> str:
        if args and args[0] == "pr" and len(args) >= 2 and args[1] == "list":
            return json.dumps(prs)
        if args and args[0] == "issue" and len(args) >= 2 and args[1] == "list":
            return json.dumps(issues)
        if args and args[0] == "pr" and len(args) >= 2 and args[1] == "view":
            n = int(args[2])
            payload = pr_checks.get(n, {"statusCheckRollup": [], "changedFiles": 0})
            return json.dumps(payload)
        raise AssertionError(f"unexpected gh args: {args!r}")

    return fake


# ----- classify --------------------------------------------------------------


class ClassifyTests(unittest.TestCase):
    def test_label_takes_precedence_over_title(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="any title", body="any body",
            state="OPEN", labels=("area:hook",), created_at="",
            updated_at="", author="", url="",
        )
        # Label says hook → edit-admission, NOT the title fallback.
        self.assertEqual(poip.classify(item), "edit-admission")

    def test_title_pattern_for_state(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="ralph state machine bug",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "state")

    def test_title_pattern_for_artifact(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="promote-phases missing",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "artifact")

    def test_title_pattern_for_side_effect(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="force-push confirmation broken",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "side-effect")

    def test_title_pattern_for_throughput(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="hook latency regression",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "throughput")

    def test_title_pattern_for_measurement(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="token-efficiency dashboard",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "measurement")

    def test_title_pattern_for_verification(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="regression test missing",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "verification")

    def test_title_pattern_for_recovery(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="janitor retention bug",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "recovery")

    def test_default_boundary_is_state(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="unrelated stuff",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertEqual(poip.classify(item), "state")

    def test_classify_returns_string_from_boundaries(self):
        item = poip.new_open_item(
            kind="issue", number=1, title="x", body="",
            state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        self.assertIn(poip.classify(item), poip.BOUNDARIES)


# ----- score -----------------------------------------------------------------


class ScoreTests(unittest.TestCase):
    def test_default_scores_match_boundary_table(self):
        # The boundary → (B, R, C) table is the public contract.
        item = poip.new_open_item(
            kind="issue", number=1, title="worktree-guard false deny",
            body="", state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        # edit-admission boundary → (5, 4, 4)
        self.assertEqual(poip.score(item), (5, 4, 4))

    def test_wide_pr_increases_risk(self):
        item = poip.new_open_item(
            kind="pr", number=1, title="x", body="",
            state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
            checks_state="success", files_count=poip.WIDE_PR_FILE_THRESHOLD + 1,
        )
        _, risk, _ = poip.score(item)
        # baseline risk +1 (clamped at 5).
        self.assertEqual(risk, min(5, poip.DEFAULT_SCORES["state"][1] + 1))

    def test_red_checks_increase_bottleneck(self):
        item = poip.new_open_item(
            kind="pr", number=1, title="x", body="",
            state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
            checks_state="failure",
        )
        bottleneck, _, _ = poip.score(item)
        # baseline bottleneck +1 for red checks.
        self.assertGreaterEqual(bottleneck, 4)

    def test_scores_clamped_to_5(self):
        # A wide PR on red checks should NOT exceed 5 on either axis.
        item = poip.new_open_item(
            kind="pr", number=1, title="x", body="",
            state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
            checks_state="failure",
            files_count=poip.WIDE_PR_FILE_THRESHOLD * 10,
        )
        b, r, _ = poip.score(item)
        self.assertLessEqual(b, 5)
        self.assertLessEqual(r, 5)


# ----- recommend_disposition ------------------------------------------------


class RecommendDispositionTests(unittest.TestCase):
    def _item(self, **kw) -> dict:
        defaults = dict(
            kind="pr", number=1, title="x", body="",
            state="OPEN", labels=(), created_at="",
            updated_at="", author="", url="",
        )
        defaults.update(kw)
        return poip.new_open_item(**defaults)

    def test_reject_for_superseded_title(self):
        item = self._item(title="duplicate of #123", kind="issue")
        self.assertEqual(poip.recommend_disposition(item), "reject")

    def test_reject_for_wontfix_title(self):
        item = self._item(title="wontfix: stale", kind="issue")
        self.assertEqual(poip.recommend_disposition(item), "reject")

    def test_replace_for_red_checks(self):
        item = self._item(
            kind="pr", title="worktree-guard",
            checks_state="failure",
        )
        self.assertEqual(poip.recommend_disposition(item), "replace")

    def test_replace_for_wide_pr(self):
        item = self._item(
            kind="pr", title="worktree-guard",
            checks_state="success",
            files_count=poip.WIDE_PR_FILE_THRESHOLD + 1,
        )
        self.assertEqual(poip.recommend_disposition(item), "replace")

    def test_keep_for_green_narrow_pr_on_critical_path(self):
        # edit-admission has containment=4 baseline.
        item = self._item(
            kind="pr", title="worktree-guard hook fix",
            checks_state="success",
            files_count=2,
        )
        self.assertEqual(poip.recommend_disposition(item), "keep")

    def test_defer_for_throughput_boundary(self):
        item = self._item(
            kind="pr", title="hook latency tuning",
            checks_state="success",
            files_count=2,
        )
        self.assertEqual(poip.recommend_disposition(item), "defer")

    def test_defer_for_measurement_boundary(self):
        item = self._item(
            kind="issue", title="token-efficiency dashboard",
        )
        self.assertEqual(poip.recommend_disposition(item), "defer")

    def test_defer_for_documentation_boundary(self):
        item = self._item(
            kind="issue", title="docs typo cleanup",
        )
        self.assertEqual(poip.recommend_disposition(item), "defer")

    def test_replace_is_default_for_issue_on_critical_path(self):
        item = self._item(
            kind="issue", title="worktree-guard hook fix",
        )
        # No PR exists → conservative `replace` (don't presume the issue
        # has an accepted PR shape).
        self.assertEqual(poip.recommend_disposition(item), "replace")


# ----- bucket_for ------------------------------------------------------------


class BucketForTests(unittest.TestCase):
    def test_hard_stop_holds_edit_admission(self):
        self.assertEqual(poip.bucket_for("edit-admission"), "hard-stop")

    def test_hard_stop_holds_state(self):
        self.assertEqual(poip.bucket_for("state"), "hard-stop")

    def test_resume_audit_holds_artifact(self):
        self.assertEqual(poip.bucket_for("artifact"), "resume-audit")

    def test_resume_audit_holds_verification(self):
        self.assertEqual(poip.bucket_for("verification"), "resume-audit")

    def test_side_effect_integrity(self):
        self.assertEqual(poip.bucket_for("side-effect"), "side-effect-integrity")

    def test_safe_cleanup_holds_recovery(self):
        self.assertEqual(poip.bucket_for("recovery"), "safe-cleanup")

    def test_measured_optimization_holds_throughput(self):
        self.assertEqual(poip.bucket_for("throughput"), "measured-optimization")

    def test_measured_optimization_holds_measurement(self):
        self.assertEqual(poip.bucket_for("measurement"), "measured-optimization")

    def test_learning_documentation(self):
        self.assertEqual(poip.bucket_for("documentation"), "learning-documentation")

    def test_unknown_boundary_falls_back_to_resume_audit(self):
        self.assertEqual(poip.bucket_for("not-a-boundary"), "resume-audit")


# ----- compose_yaml shape ----------------------------------------------------


class ComposeYamlTests(unittest.TestCase):
    def _snapshot(self, items):
        return poip.new_backlog_snapshot(
            snapshot_date="2026-09-15",
            main_head_sha="abcdef1234567890",
            items=tuple(items),
        )

    def test_yaml_parses_cleanly(self):
        items = [
            poip.new_open_item(
                kind="issue", number=842, title="worktree-guard false deny",
                body="Body line 1\nBody line 2", state="OPEN",
                labels=("area:hook",), created_at="2026-09-10T00:00:00Z",
                updated_at="2026-09-10T00:00:00Z", author="octocat",
                url="https://example/842",
            ),
            poip.new_open_item(
                kind="pr", number=838, title="hygiene bundle",
                body="", state="OPEN", labels=(),
                created_at="2026-08-01T00:00:00Z",
                updated_at="2026-08-01T00:00:00Z", author="octocat",
                url="https://example/838",
                checks_state="failure", files_count=25,
            ),
            poip.new_open_item(
                kind="issue", number=820, title="dashboard redesign",
                body="", state="OPEN", labels=(),
                created_at="2026-07-01T00:00:00Z",
                updated_at="2026-07-01T00:00:00Z", author="octocat",
                url="https://example/820",
            ),
        ]
        snap = self._snapshot(items)
        text, _counts = poip.compose_yaml(snap)
        parsed = yaml.safe_load(text)
        # Top-level fields exist.
        self.assertEqual(parsed["title"], "Open work priority — orchestrator-first triage")
        self.assertEqual(parsed["status"], "ready-for-review")
        self.assertEqual(parsed["issue"], 843)
        self.assertEqual(parsed["date"], "2026-09-15")
        self.assertIn("long-running", parsed["tags"])
        # Before/after blocks exist.
        self.assertIn("summary", parsed["before"])
        self.assertIn("evidence", parsed["before"])
        self.assertIn("summary", parsed["after"])
        self.assertIn("files", parsed["after"])
        # Pros / cons / limitations lists.
        self.assertTrue(len(parsed["pros"]) >= 1)
        self.assertTrue(len(parsed["cons"]) >= 1)
        self.assertTrue(len(parsed["limitations"]) >= 1)
        # Sections present.
        titles = [s["title"] for s in parsed["sections"]]
        for required in (
            "Snapshot summary",
            "Decision rule: accept issues, not PRs",
            "Bottleneck map — orchestrator critical path",
            "Open work by orchestrator bucket",
            "Risk-based disposition table",
            "Cons, limitations, accepted trade-offs",
            "Acceptance gates and exit criteria",
            "Current backlog snapshot",
        ):
            self.assertIn(required, titles)

    def test_compose_yaml_renders_via_proposal_parser(self):
        items = [
            poip.new_open_item(
                kind="pr", number=842, title="worktree-guard hook",
                body="body", state="OPEN", labels=(),
                created_at="2026-09-10T00:00:00Z",
                updated_at="2026-09-10T00:00:00Z", author="x",
                url="https://example/842",
                checks_state="success", files_count=1,
            ),
        ]
        snap = self._snapshot(items)
        text, _counts = poip.compose_yaml(snap)
        # Strip the header comments; the renderer doesn't parse them,
        # and yaml.safe_load leaves them in (so we keep them in compose
        # output for source readability — but parse_proposal_yaml
        # itself uses safe_load which accepts comments).
        parsed = rph.parse_proposal_yaml(text)
        self.assertEqual(parsed.title, "Open work priority — orchestrator-first triage")
        self.assertEqual(parsed.status, "ready-for-review")

    def test_render_html_produces_html_file(self):
        items = [
            poip.new_open_item(
                kind="issue", number=842, title="worktree-guard",
                body="body", state="OPEN", labels=(),
                created_at="2026-09-10T00:00:00Z",
                updated_at="2026-09-10T00:00:00Z", author="x",
                url="https://example/842",
            ),
        ]
        snap = self._snapshot(items)
        text, _counts = poip.compose_yaml(snap)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            html = poip.render_html(
                text, repo_root=repo,
                bucket="reviewing", main="long-running-priorities",
                sub="open-work-priority",
            )
            self.assertTrue(html.exists())
            self.assertTrue(html.read_text(encoding="utf-8").startswith("<!"))
            yaml_path = html.with_suffix(".yaml")
            self.assertTrue(yaml_path.exists())
            self.assertIn("Open work priority", yaml_path.read_text(encoding="utf-8"))

    def test_render_html_rejects_unknown_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                poip.render_html(
                    "title: x\nstatus: draft\nsections: []\n",
                    repo_root=Path(td),
                    bucket="not-a-bucket",
                    main="m", sub="s",
                )

    def test_render_html_rejects_unsafe_slug(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                poip.render_html(
                    "title: x\nstatus: draft\nsections: []\n",
                    repo_root=Path(td),
                    bucket="reviewing",
                    main="../escape",
                    sub="s",
                )


# ----- snapshot helpers ------------------------------------------------------


class SnapshotHelpersTests(unittest.TestCase):
    def test_normalize_pr_safe(self):
        raw = _pr_raw(842, title="t", body="b", labels=["area:hook"])
        item = poip._normalize_pr_safe(raw)
        self.assertEqual(item["kind"], "pr")
        self.assertEqual(item["number"], 842)
        self.assertEqual(item["title"], "t")
        self.assertEqual(item["body"], "b")
        self.assertEqual(item["labels"], ("area:hook",))
        self.assertEqual(item["base_ref_name"], "main")
        self.assertEqual(item["head_ref_name"], "feat/example")
        self.assertEqual(item["author"], "octocat")
        self.assertFalse(item["is_draft"])

    def test_normalize_issue(self):
        raw = _issue_raw(842, title="t", body="b", labels=["area:hook"])
        item = poip._normalize_issue(raw)
        self.assertEqual(item["kind"], "issue")
        self.assertEqual(item["labels"], ("area:hook",))

    def test_fetch_pr_checks_for_success(self):
        def fake(_args):
            return json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "SUCCESS"},
                ],
                "changedFiles": 3,
            })
        state, files = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "success")
        self.assertEqual(files, 3)

    def test_fetch_pr_checks_for_failure(self):
        def fake(_args):
            return json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "FAILURE"},
                ],
                "changedFiles": 1,
            })
        state, _ = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "failure")

    def test_fetch_pr_checks_for_pending(self):
        def fake(_args):
            return json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"status": "IN_PROGRESS"},
                ],
                "changedFiles": 1,
            })
        state, _ = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "pending")

    def test_fetch_pr_checks_for_empty(self):
        def fake(_args):
            return json.dumps({"statusCheckRollup": [], "changedFiles": 0})
        state, files = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "none")
        self.assertEqual(files, 0)

    def test_fetch_pr_checks_handles_malformed_json(self):
        def fake(_args):
            return "not-json"
        state, files = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "none")
        self.assertEqual(files, 0)

    def test_fetch_pr_checks_handles_runner_error(self):
        def fake(_args):
            raise poip.SnapshotError("network blip")
        state, files = poip._fetch_pr_checks_for(842, fake)
        self.assertEqual(state, "none")
        self.assertEqual(files, 0)


# ----- end-to-end pipeline --------------------------------------------------


class EndToEndTests(unittest.TestCase):
    def test_snapshot_returns_items(self):
        prs = [_pr_raw(842, title="worktree-guard")]
        issues = [_issue_raw(841, title="phase artifacts")]
        fake = _gh_fake(prs, issues)
        with tempfile.TemporaryDirectory() as td:
            snap = poip.snapshot_open_backlog(
                repo_root=Path(td), gh_runner=fake,
            )
            self.assertEqual(len(poip.prs_in_snapshot(snap)), 1)
            self.assertEqual(len(poip.issues_in_snapshot(snap)), 1)
            self.assertEqual(poip.prs_in_snapshot(snap)[0]["number"], 842)
            self.assertEqual(poip.issues_in_snapshot(snap)[0]["number"], 841)

    def test_snapshot_populates_pr_checks(self):
        prs = [_pr_raw(842, title="worktree-guard")]
        issues = []
        pr_checks = {842: {"statusCheckRollup": [{"conclusion": "SUCCESS"}],
                           "changedFiles": 4}}
        fake = _gh_fake(prs, issues, pr_checks)
        with tempfile.TemporaryDirectory() as td:
            snap = poip.snapshot_open_backlog(
                repo_root=Path(td), gh_runner=fake,
            )
            self.assertEqual(poip.prs_in_snapshot(snap)[0]["checks_state"], "success")
            self.assertEqual(poip.prs_in_snapshot(snap)[0]["files_count"], 4)

    def test_snapshot_raises_on_malformed_json(self):
        def fake(_args):
            return "{ not json"
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(poip.SnapshotError):
                poip.snapshot_open_backlog(repo_root=Path(td), gh_runner=fake)

    def test_snapshot_raises_on_non_list_payload(self):
        def fake(_args):
            return json.dumps({"not": "a list"})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(poip.SnapshotError):
                poip.snapshot_open_backlog(repo_root=Path(td), gh_runner=fake)

    def test_new_work_filters_old_items(self):
        old = _issue_raw(820, title="dashboard", created="2024-01-01T00:00:00Z",
                         updated="2024-01-01T00:00:00Z")
        new = _issue_raw(842, title="worktree", created="2026-09-10T00:00:00Z",
                         updated="2026-09-10T00:00:00Z")
        # Override snapshot_date to 2026-09-15 for the test.
        fake = _gh_fake([], [old, new])
        with tempfile.TemporaryDirectory() as td:
            with patch.object(poip, "_snapshot_date_today", return_value="2026-09-15"):
                snap = poip.snapshot_open_backlog(repo_root=Path(td), gh_runner=fake)
            self.assertEqual(len(poip.new_work_in_snapshot(snap)), 1)
            self.assertEqual(poip.new_work_in_snapshot(snap)[0]["number"], 842)


# ----- CLI dispatch ---------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_main_returns_2_when_gh_unavailable(self):
        with patch.object(poip, "_run_gh", side_effect=poip.GhUnavailable("missing")):
            with tempfile.TemporaryDirectory() as td:
                rc = poip.main(["--project-root", td])
                self.assertEqual(rc, 2)

    def test_main_returns_3_on_snapshot_error(self):
        with patch.object(poip, "snapshot_open_backlog",
                          side_effect=poip.SnapshotError("bad")):
            with tempfile.TemporaryDirectory() as td:
                rc = poip.main(["--project-root", td])
                self.assertEqual(rc, 3)

    def test_main_print_yaml_skips_render(self):
        # Build a snapshot directly and feed it through main with --print-yaml.
        snap = poip.new_backlog_snapshot(
            snapshot_date="2026-09-15",
            main_head_sha="abc123",
            items=(
                poip.new_open_item(
                    kind="issue", number=842, title="worktree-guard",
                    body="body", state="OPEN", labels=(),
                    created_at="2026-09-10T00:00:00Z",
                    updated_at="2026-09-10T00:00:00Z", author="x",
                    url="https://example/842",
                ),
            ),
        )
        with patch.object(poip, "snapshot_open_backlog", return_value=snap):
            with tempfile.TemporaryDirectory() as td:
                rc = poip.main(["--project-root", td, "--print-yaml"])
                self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
