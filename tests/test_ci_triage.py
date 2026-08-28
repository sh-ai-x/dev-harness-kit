#!/usr/bin/env python3
"""test_ci_triage.py — regression tests for lib/ci_triage.py.

Black-box coverage:

  1. Dedup signature — stable across identical failures, distinct across
     different workflow/job/marker combinations.
  2. Case store round-trip (load/save) and the unjudged -> open lifecycle.
  3. `runs_for_commit` refuses short SHAs (the `gh run list --commit`
     silent-empty-list gotcha this module exists to avoid).
  4. `scan()` end-to-end with subprocess mocked: two commits sharing one
     failure signature collapse into a single case with two occurrences;
     a second scan against a third commit bumps occurrences without
     re-flagging the case as unjudged.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
LIB = REPO_ROOT / "lib"


class TestSignature(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import signature
        self.signature = signature

    def test_same_workflow_and_marker_is_stable(self):
        sig_a = self.signature("cost-flag.yml", {"job_name": None, "marker": "a workflow file issue"})
        sig_b = self.signature("cost-flag.yml", {"job_name": None, "marker": "a workflow file issue"})
        self.assertEqual(sig_a, sig_b)

    def test_different_marker_changes_signature(self):
        sig_a = self.signature("cost-flag.yml", {"job_name": None, "marker": "a workflow file issue"})
        sig_b = self.signature("cost-flag.yml", {"job_name": None, "marker": "a timeout"})
        self.assertNotEqual(sig_a, sig_b)

    def test_different_workflow_changes_signature(self):
        sig_a = self.signature("cost-flag.yml", {"job_name": "aggregate", "marker": "step X"})
        sig_b = self.signature("ci.yml", {"job_name": "aggregate", "marker": "step X"})
        self.assertNotEqual(sig_a, sig_b)


class TestStoreRoundtrip(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import SCHEMA_VERSION, load_store, save_store
        self.load_store = load_store
        self.save_store = save_store
        self.schema_version = SCHEMA_VERSION

    def test_missing_store_returns_empty_schema(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.load_store(Path(d) / "nope.json")
            self.assertEqual(store, {"schema_version": self.schema_version, "cases": []})

    def test_save_then_load_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "store.json"
            store = {"schema_version": 1, "cases": [{"id": "abc", "occurrences": []}]}
            self.save_store(path, store)
            self.assertTrue(path.exists())
            self.assertEqual(self.load_store(path), store)


class TestRecordOccurrenceAndJudgment(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import find_case, record_judgment, record_occurrence
        self.record_occurrence = record_occurrence
        self.record_judgment = record_judgment
        self.find_case = find_case

    def test_first_occurrence_creates_unjudged_stub(self):
        store = {"schema_version": 1, "cases": []}
        case = self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        self.assertEqual(case["status"], "unjudged")
        self.assertEqual(len(case["occurrences"]), 1)
        self.assertEqual(len(store["cases"]), 1)

    def test_second_occurrence_appends_not_duplicates(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "b", "run_id": 2})
        self.assertEqual(len(store["cases"]), 1)
        self.assertEqual(len(store["cases"][0]["occurrences"]), 2)

    def _judge_kwargs(self, **overrides):
        base = dict(
            primary_cause="harness", secondary_cause="state-contamination",
            evidence="workflow updated_at (2026-07-14) predates file's last commit (2026-07-16)",
            repro="gh api repos/:owner/:repo/actions/workflows/312869658 | jq .updated_at",
            regression_test="tests/test_ci_doctor.py::test_workflow_registration_freshness",
            proposal="gh api -X PUT .../workflows/312869658/disable then enable",
        )
        base.update(overrides)
        return base

    def test_record_judgment_transitions_to_open(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        self.record_judgment(store, "sig1", **self._judge_kwargs(hook_proposal="post-edit re-registration check"))
        case = self.find_case(store, "sig1")
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["primary_cause"], "harness")
        self.assertEqual(case["secondary_cause"], "state-contamination")
        self.assertEqual(case["hook_proposal"], "post-edit re-registration check")

    def test_record_judgment_unknown_id_raises(self):
        store = {"schema_version": 1, "cases": []}
        with self.assertRaises(KeyError):
            self.record_judgment(store, "missing", **self._judge_kwargs())

    def test_record_judgment_rejects_unknown_primary_cause(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        with self.assertRaises(ValueError):
            self.record_judgment(store, "sig1", **self._judge_kwargs(primary_cause="infra"))

    def test_record_judgment_rejects_secondary_not_under_primary(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        with self.assertRaises(ValueError):
            self.record_judgment(
                store, "sig1", **self._judge_kwargs(primary_cause="model", secondary_cause="state-contamination"),
            )

    def test_record_judgment_requires_regression_test(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        with self.assertRaises(ValueError):
            self.record_judgment(store, "sig1", **self._judge_kwargs(regression_test=""))

    def test_record_judgment_requires_repro(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        with self.assertRaises(ValueError):
            self.record_judgment(store, "sig1", **self._judge_kwargs(repro=""))

    def test_record_judgment_allows_na_regression_test_with_reason(self):
        store = {"schema_version": 1, "cases": []}
        self.record_occurrence(store, "sig1", "cost-flag.yml", {"commit": "a", "run_id": 1})
        self.record_judgment(
            store, "sig1", **self._judge_kwargs(regression_test="N/A: third-party outage, no repo-side guard possible"),
        )
        case = self.find_case(store, "sig1")
        self.assertTrue(case["regression_test"].startswith("N/A:"))


class TestRunsForCommitValidation(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import runs_for_commit
        self.runs_for_commit = runs_for_commit

    def test_short_sha_raises_before_any_subprocess_call(self):
        with self.assertRaises(ValueError):
            self.runs_for_commit("060d53b")


class TestScanIntegration(unittest.TestCase):
    """End-to-end scan() with subprocess mocked at the module's `_run` seam."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage
        self.commit_a = "a" * 40
        self.commit_b = "b" * 40
        self.commit_c = "c" * 40

    def _fake_run(self, cmd: list[str]) -> str:
        if cmd[:2] == ["git", "rev-parse"]:
            ref = cmd[2]
            return {"A": self.commit_a, "B": self.commit_b, "C": self.commit_c}[ref] + "\n"
        if cmd[:2] == ["git", "log"] and "-1" in cmd:
            sha = cmd[-1]
            return f"docs: fix thing {sha[0]}\x1fbot@users.noreply.github.com\x1f\n"
        if cmd[:3] == ["gh", "run", "list"]:
            sha = cmd[cmd.index("--commit") + 1]
            run_id = {self.commit_a: 101, self.commit_b: 102, self.commit_c: 103}[sha]
            return json.dumps([{
                "databaseId": run_id, "name": "cost-flag.yml", "status": "completed",
                "conclusion": "failure", "event": "push", "headBranch": "main",
                "createdAt": "2026-07-30T08:00:00Z", "url": f"https://example/{run_id}",
            }])
        if cmd[:3] == ["gh", "api", "repos/:owner/:repo/actions/runs/101/jobs"] or \
           cmd[2].endswith("/jobs"):
            return json.dumps({"total_count": 0, "jobs": []})
        if cmd[:3] == ["gh", "run", "view"] and "--log-failed" not in cmd:
            return "X This run likely failed because of a workflow file issue.\n"
        raise AssertionError(f"unexpected command: {cmd}")

    def test_two_commits_same_failure_collapse_to_one_case(self):
        with tempfile.TemporaryDirectory() as d:
            store_path = Path(d) / "store.json"
            with patch.object(self.mod, "_run", side_effect=self._fake_run):
                result = self.mod.scan(commits=["A", "B"], count=None, store_path=store_path)

            self.assertEqual(len(result["unjudged"]), 1)
            case = result["unjudged"][0]["case"]
            self.assertEqual(len(case["occurrences"]), 2)

            store = self.mod.load_store(store_path)
            self.assertEqual(len(store["cases"]), 1)
            self.assertEqual(store["cases"][0]["status"], "unjudged")

    def test_rescan_after_judging_bumps_occurrence_not_unjudged(self):
        with tempfile.TemporaryDirectory() as d:
            store_path = Path(d) / "store.json"
            with patch.object(self.mod, "_run", side_effect=self._fake_run):
                first = self.mod.scan(commits=["A"], count=None, store_path=store_path)
            case_id = first["unjudged"][0]["case"]["id"]

            store = self.mod.load_store(store_path)
            self.mod.record_judgment(
                store, case_id, primary_cause="harness", secondary_cause="state-contamination",
                evidence="ev", repro="repro", regression_test="tests/test_x.py::test_y", proposal="prop",
            )
            self.mod.save_store(store_path, store)

            with patch.object(self.mod, "_run", side_effect=self._fake_run):
                second = self.mod.scan(commits=["C"], count=None, store_path=store_path)

            self.assertEqual(len(second["unjudged"]), 0)
            self.assertEqual(len(second["already_known"]), 1)
            self.assertEqual(len(second["already_known"][0]["occurrences"]), 2)


class TestFailureSignalsMultiJob(unittest.TestCase):
    """Regression test for a real dogfooding failure: a single run can fail
    more than one job at the same step name (e.g. `review` and `security`
    both failing at "Resolve PR + provider"). `failure_signals` must return
    one entry per failing job, not just the first, and must not cross-
    attribute one job's log lines to another job's entry."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def _fake_run(self, cmd: list[str]) -> str:
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/jobs"):
            return json.dumps({"jobs": [
                {"name": "/dev-kit:review (3-dim)", "conclusion": "failure",
                 "steps": [{"name": "Resolve PR + provider", "conclusion": "failure"}]},
                {"name": "/dev-kit:security (10-dim OWASP)", "conclusion": "failure",
                 "steps": [{"name": "Resolve PR + provider", "conclusion": "failure"}]},
                {"name": "severity gate", "conclusion": "success", "steps": []},
            ]})
        if cmd[:3] == ["gh", "run", "view"] and "--log-failed" in cmd:
            return (
                "/dev-kit:review (3-dim)\tResolve PR + provider\tts review-specific error line\n"
                "/dev-kit:security (10-dim OWASP)\tResolve PR + provider\tts security-specific error line\n"
            )
        raise AssertionError(f"unexpected command: {cmd}")

    def test_returns_one_signal_per_failing_job(self):
        with patch.object(self.mod, "_run", side_effect=self._fake_run):
            signals = self.mod.failure_signals(999)
        self.assertEqual(len(signals), 2)
        job_names = {s["job_name"] for s in signals}
        self.assertEqual(job_names, {"/dev-kit:review (3-dim)", "/dev-kit:security (10-dim OWASP)"})

    def test_detail_is_not_cross_attributed_between_jobs(self):
        with patch.object(self.mod, "_run", side_effect=self._fake_run):
            signals = self.mod.failure_signals(999)
        by_job = {s["job_name"]: s["detail"] for s in signals}
        self.assertIn("review-specific error line", by_job["/dev-kit:review (3-dim)"])
        self.assertNotIn("security-specific error line", by_job["/dev-kit:review (3-dim)"])
        self.assertIn("security-specific error line", by_job["/dev-kit:security (10-dim OWASP)"])
        self.assertNotIn("review-specific error line", by_job["/dev-kit:security (10-dim OWASP)"])

    def test_scan_creates_two_distinct_cases_for_one_run_with_two_failed_jobs(self):
        def fake_run(cmd: list[str]) -> str:
            if cmd[:2] == ["git", "rev-parse"]:
                return "d" * 40 + "\n"
            if cmd[:2] == ["git", "log"] and "-1" in cmd:
                return "subject\x1fauthor@x\x1f\n"
            if cmd[:3] == ["gh", "run", "list"]:
                return json.dumps([{
                    "databaseId": 999, "name": "PR Review", "status": "completed",
                    "conclusion": "failure", "event": "pull_request", "headBranch": "x",
                    "createdAt": "2026-07-30T09:00:00Z", "url": "https://example/999",
                }])
            return self._fake_run(cmd)

        with tempfile.TemporaryDirectory() as d:
            store_path = Path(d) / "store.json"
            with patch.object(self.mod, "_run", side_effect=fake_run):
                result = self.mod.scan(commits=["D"], count=None, store_path=store_path)
            self.assertEqual(len(result["unjudged"]), 2)


class TestFailureSignalsErrorAnnotationPreferred(unittest.TestCase):
    """Regression test for a second dogfooding failure: the real error is a
    `##[error]` annotation mid-log, but a large "Post job cleanup" section
    (git config teardown boilerplate that's present on every job,
    pass or fail) follows it. Blindly tailing the last 4000 chars of a
    job's lines landed in that boilerplate and lost the actual error."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def _fake_run(self, cmd: list[str]) -> str:
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/jobs"):
            return json.dumps({"jobs": [
                {"name": "review", "conclusion": "failure",
                 "steps": [{"name": "Resolve PR + provider", "conclusion": "failure"}]},
            ]})
        if cmd[:3] == ["gh", "run", "view"] and "--log-failed" in cmd:
            cleanup = "".join(f"review\tUNKNOWN STEP\tts cleanup line {i}\n" for i in range(200))
            return (
                "review\tResolve PR + provider\tts ##[error]No provider resolved. Set the GitHub repo variable:\n"
                "review\tResolve PR + provider\tts ##[error]Process completed with exit code 1.\n"
                + cleanup
            )
        raise AssertionError(f"unexpected command: {cmd}")

    def test_error_annotation_survives_despite_trailing_cleanup_boilerplate(self):
        with patch.object(self.mod, "_run", side_effect=self._fake_run):
            signals = self.mod.failure_signals(999)
        self.assertEqual(len(signals), 1)
        self.assertIn("No provider resolved", signals[0]["detail"])
        self.assertNotIn("cleanup line", signals[0]["detail"])


class TestRenderReport(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import render_report
        self.render_report = render_report

    def test_empty_store(self):
        out = self.render_report({"schema_version": 1, "cases": []})
        self.assertIn("No failure cases recorded yet.", out)

    def test_judged_case_includes_cause_repro_and_regression_test(self):
        store = {"schema_version": 1, "cases": [{
            "id": "sig1", "workflow": "cost-flag.yml", "status": "open",
            "primary_cause": "harness", "secondary_cause": "state-contamination",
            "evidence": "ev", "repro": "gh api ...", "proposal": "prop",
            "regression_test": "tests/test_ci_doctor.py::test_x", "hook_proposal": "hook",
            "occurrences": [{"commit": "a"}],
        }]}
        out = self.render_report(store)
        self.assertIn("harness / state-contamination", out)
        self.assertIn("tests/test_ci_doctor.py::test_x", out)


class TestWorkflowIdFromProposal(unittest.TestCase):
    """The auto-fix path matches a proposal against the canonical
    api-toggle pattern (`actions/workflows/<id>/disable` + `enable`).
    One workflow id only — a proposal with two distinct IDs is treated
    as ambiguous and returns None so we don't accidentally toggle the
    wrong workflow."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import _workflow_id_from_proposal
        self.extract = _workflow_id_from_proposal

    def test_canonical_disable_enable_pattern_returns_id(self):
        self.assertEqual(
            self.extract("gh api -X PUT repos/x/y/actions/workflows/312869658/disable && "
                         "gh api -X PUT repos/x/y/actions/workflows/312869658/enable"),
            312869658,
        )

    def test_proposal_without_toggle_pattern_returns_none(self):
        self.assertIsNone(self.extract("manually re-create the workflow file"))

    def test_two_distinct_workflow_ids_in_proposal_is_ambiguous(self):
        self.assertIsNone(self.extract(
            "gh api .../actions/workflows/111/disable && gh api .../actions/workflows/222/enable"
        ))


class TestApplyApiToggle(unittest.TestCase):
    """`_apply_api_toggle` runs disable -> enable and records both
    commands + the pre/post {state, updated_at} pair so the case log
    carries an audit trail of *exactly* what was executed."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def test_records_commands_and_pre_post_state(self):
        # Pre-GET happens BEFORE disable, so verify_pre.state is whatever
        # the workflow is right now ("active"). The toggle disable→enable
        # runs, then post-GET verifies it's "active" again with a fresh
        # `updated_at` (the cache-refresh signal). The audit value is
        # `updated_at` changing, not state.
        def fake_run(cmd):
            if cmd[:2] == ["gh", "api"] and "/actions/workflows/312869658" in cmd[-1]:
                if "/disable" in cmd[-1] or "/enable" in cmd[-1]:
                    return ""
                # Pre vs post distinguished by the toggled updated_at —
                # the audit value of the whole exercise is "did the
                # server-side updated_at change after disable→enable".
                if not hasattr(fake_run, "_calls"):
                    fake_run._calls = 0  # type: ignore[attr-defined]
                fake_run._calls += 1  # type: ignore[attr-defined]
                if fake_run._calls == 1:  # type: ignore[attr-defined]
                    return json.dumps({"state": "active", "updated_at": "2026-07-14T18:11:40.000+09:00"})
                return json.dumps({"state": "active", "updated_at": "2026-07-31T20:10:31.000+09:00"})
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch.object(self.mod, "_run", side_effect=fake_run):
            result = self.mod._apply_api_toggle(312869658)

        self.assertEqual(result["method"], "api-toggle")
        self.assertEqual(len(result["commands_run"]), 2)
        self.assertIn("/disable", result["commands_run"][0])
        self.assertIn("/enable", result["commands_run"][1])
        self.assertEqual(result["verify_pre"]["state"], "active")
        self.assertEqual(result["verify_post"]["state"], "active")
        self.assertNotEqual(result["verify_pre"]["updated_at"], result["verify_post"]["updated_at"])


class TestCommitForFix(unittest.TestCase):
    """For code-fix resolutions, find the most recent commit on the
    workflow file after first_seen.date and look up its PR. When no
    commit exists, return None — the case is treated as manual."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def test_finds_commit_and_linked_pr(self):
        def fake_run(cmd):
            if cmd[:2] == ["git", "log"]:
                return "abc123def456\x1ffix: register pull_request trigger only\n"
            if cmd[:2] == ["gh", "api"] and any("/commits/" in a and a.endswith("/pulls") for a in cmd):
                # Production-shaped response: `gh api .../commits/{sha}/pulls`
                # returns a list of {number, html_url, title}; we then jq
                # the first element. The mocked return is what `jq` would
                # produce AFTER the filter — a single object.
                return json.dumps({
                    "number": 203, "html_url": "https://github.com/x/y/pull/203",
                    "title": "fix: register pull_request trigger only",
                })
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch.object(self.mod, "_run", side_effect=fake_run):
            result = self.mod._commit_for_fix(".github/workflows/cost-flag.yml", "2026-07-14T00:00:00Z")

        self.assertEqual(result["sha"], "abc123def456")
        self.assertEqual(result["subject"], "fix: register pull_request trigger only")
        self.assertEqual(result["pr_number"], 203)
        self.assertEqual(result["pr_url"], "https://github.com/x/y/pull/203")

    def test_no_commit_since_first_seen_returns_none(self):
        def fake_run(cmd):
            if cmd[:2] == ["git", "log"]:
                return ""
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch.object(self.mod, "_run", side_effect=fake_run):
            result = self.mod._commit_for_fix(".github/workflows/x.yml", "2026-07-14T00:00:00Z")
        self.assertIsNone(result)


class TestProcess(unittest.TestCase):
    """End-to-end `process()` lifecycle:
    - open + api-toggle proposal + auto_fix + clean verify -> processed
      with full resolution record (commands_run, verify_pre/post).
    - open + api-toggle proposal + auto_fix + fresh failures -> stays
      open with `last_process_attempt` note.
    - open + clean verify but auto_fix disabled -> processed with
      method=manual (resolution still recorded for audit).
    - already-processed -> skipped, never re-toggled.
    """

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage
        self.case_id = "abc123def456"
        self.case = {
            "id": self.case_id, "signature": self.case_id,
            "workflow": ".github/workflows/cost-flag.yml", "status": "open",
            "primary_cause": "harness", "secondary_cause": "state-contamination",
            "evidence": "registration predates file",
            "repro": "gh api .../actions/workflows | jq .updated_at",
            "regression_test": "N/A: server-side cache not observable in repo",
            "proposal": "gh api -X PUT repos/x/y/actions/workflows/312869658/disable && "
                        "gh api -X PUT repos/x/y/actions/workflows/312869658/enable",
            "first_seen": {"date": "2026-07-14T00:00:00Z", "run_id": 1, "commit": "a" * 40, "url": ""},
            "occurrences": [{"commit": "a" * 40, "run_id": 1, "date": "2026-07-14T00:00:00Z", "url": ""}],
        }

    def _gh_run_list(self):
        # Default: no recent failures -> clean verify
        return json.dumps([])

    def _workflow_get(self):
        return json.dumps({"state": "active", "updated_at": "2026-07-31T20:10:31.000+09:00"})

    def _fake_run_clean(self, cmd):
        if cmd[:2] == ["gh", "api"] and "/actions/workflows/312869658" in cmd[-1]:
            if "/disable" in cmd[-1] or "/enable" in cmd[-1]:
                return ""
            return self._workflow_get()
        if cmd[:3] == ["gh", "run", "list"]:
            return self._gh_run_list()
        if cmd[:2] == ["git", "log"]:
            return ""
        raise AssertionError(f"unexpected cmd: {cmd}")

    def _seeded_store(self, tmp: Path) -> Path:
        store_path = tmp / "store.json"
        store = {"schema_version": 3, "cases": [dict(self.case)]}
        self.mod.save_store(store_path, store)
        return store_path

    def test_auto_fix_plus_clean_verify_marks_processed_with_full_resolution_record(self):
        with tempfile.TemporaryDirectory() as d:
            store_path = self._seeded_store(Path(d))
            with patch.object(self.mod, "_run", side_effect=self._fake_run_clean):
                summary = self.mod.process(
                    auto_fix=True, verify_window=10, store_path=store_path,
                )

            self.assertEqual(len(summary["processed"]), 1)
            self.assertEqual(summary["processed"][0]["id"], self.case_id)
            self.assertEqual(summary["processed"][0]["method"], "api-toggle")

            case = self.mod.find_case(self.mod.load_store(store_path), self.case_id)
            self.assertEqual(case["status"], "processed")
            self.assertIn("processed_at", case)
            res = case["resolution"]
            self.assertEqual(res["method"], "api-toggle")
            self.assertEqual(len(res["commands_run"]), 2)
            self.assertIn("/disable", res["commands_run"][0])
            self.assertEqual(res["verify_post"]["state"], "active")
            self.assertEqual(case["post_fix_scan"]["result"], "clean")
            self.assertEqual(case["post_fix_scan"]["fresh_failures"], 0)

    def test_fresh_failure_after_fix_keeps_case_open_with_attempt_note(self):
        def fake_run_with_fresh_failures(cmd):
            if cmd[:3] == ["gh", "run", "list"]:
                return json.dumps([{
                    "databaseId": 999, "conclusion": "failure",
                    "createdAt": "2026-07-31T20:30:00Z",
                    "url": "https://example/999",
                }])
            return self._fake_run_clean(cmd)

        # Simulate the scenario the test is meant to exercise: a previous
        # `process()` run already applied a fix (toggle) and recorded its
        # `fix_applied_at`. The current run should NOT re-apply the fix
        # (the code preserves an existing resolution); the verify scan
        # uses the recorded `fix_applied_at` as the cutoff so the failure
        # at 2026-07-31T20:30:00Z (after the recorded fix) is detected
        # as a fresh failure and the case stays open.
        prior_resolution = {
            "method": "api-toggle",
            "commands_run": [
                "gh api -X PUT repos/:owner/:repo/actions/workflows/312869658/disable",
                "gh api -X PUT repos/:owner/:repo/actions/workflows/312869658/enable",
            ],
            "verify_pre": {"state": "active", "updated_at": "2026-07-31T20:10:31.000+09:00"},
            "verify_post": {"state": "active", "updated_at": "2026-07-31T20:25:00.000+09:00"},
            "fix_applied_at": "2026-07-31T20:25:00Z",
            "notes": "auto-applied stale workflow registration toggle",
        }

        with tempfile.TemporaryDirectory() as d:
            store_path = self._seeded_store(Path(d))
            # Pre-populate the prior resolution so the verify scan uses
            # the recorded `fix_applied_at` rather than `_now()` from a
            # fresh `_resolution_record()` call (which would set the
            # cutoff to wall-clock time and filter the mock failure as
            # historical).
            store = self.mod.load_store(store_path)
            store["cases"][0]["resolution"] = prior_resolution
            self.mod.save_store(store_path, store)
            with patch.object(self.mod, "_run", side_effect=fake_run_with_fresh_failures):
                summary = self.mod.process(
                    auto_fix=True, verify_window=10, store_path=store_path,
                )

            self.assertEqual(summary["processed"], [])
            self.assertEqual(len(summary["still_open"]), 1)
            self.assertEqual(summary["still_open"][0]["id"], self.case_id)

            case = self.mod.find_case(self.mod.load_store(store_path), self.case_id)
            self.assertEqual(case["status"], "open")
            self.assertEqual(case["post_fix_scan"]["result"], "still-failing")
            self.assertEqual(case["post_fix_scan"]["fresh_failures"], 1)
            self.assertEqual(case["last_process_attempt"]["fresh_failures"], 1)
            self.assertEqual(case["last_process_attempt"]["last_run_url"], "https://example/999")

    def test_auto_fix_disabled_leaves_case_open_with_manual_method(self):
        # A manual case awaiting real resolution must NOT auto-close
        # even when the workflow is quiet — an empty `gh run list`
        # would otherwise yield fresh_failures == 0 and flip the
        # case to processed despite no human having flagged it
        # resolved. The case stays at `open` with a `last_process_attempt`
        # note tagged "awaiting manual resolution".
        with tempfile.TemporaryDirectory() as d:
            store_path = self._seeded_store(Path(d))
            with patch.object(self.mod, "_run", side_effect=self._fake_run_clean):
                summary = self.mod.process(
                    auto_fix=False, verify_window=10, store_path=store_path,
                )

            self.assertEqual(summary["processed"], [])
            self.assertEqual(len(summary["still_open"]), 1)
            self.assertEqual(summary["still_open"][0]["id"], self.case_id)
            self.assertEqual(summary["still_open"][0]["reason"], "awaiting manual resolution")
            case = self.mod.find_case(self.mod.load_store(store_path), self.case_id)
            self.assertEqual(case["status"], "open")
            self.assertEqual(case["resolution"]["method"], "manual")
            self.assertEqual(case["resolution"]["commands_run"], [])
            self.assertEqual(case["last_process_attempt"]["note"], "awaiting manual resolution")
            # Manual cases don't carry a fix_applied_at or post_fix_scan —
            # those are reserved for cases whose fix was actually applied.
            self.assertNotIn("fix_applied_at", case["resolution"])
            self.assertNotIn("post_fix_scan", case)

    def test_already_processed_case_is_skipped_and_not_re_toggled(self):
        with tempfile.TemporaryDirectory() as d:
            store_path = self._seeded_store(Path(d))
            # Mark as already processed with a fake resolution record.
            store = self.mod.load_store(store_path)
            store["cases"][0]["status"] = "processed"
            store["cases"][0]["processed_at"] = "2026-07-30T00:00:00Z"
            store["cases"][0]["resolution"] = {"method": "api-toggle", "commands_run": ["old"]}
            self.mod.save_store(store_path, store)

            call_count = {"count": 0}
            def counting_run(cmd):
                call_count["count"] += 1
                return self._fake_run_clean(cmd)

            with patch.object(self.mod, "_run", side_effect=counting_run):
                summary = self.mod.process(
                    auto_fix=True, verify_window=10, store_path=store_path,
                )

            self.assertEqual(summary["processed"], [])
            self.assertEqual(summary["skipped_already_processed"], [self.case_id])
            case = self.mod.find_case(self.mod.load_store(store_path), self.case_id)
            self.assertEqual(case["processed_at"], "2026-07-30T00:00:00Z")  # unchanged
            self.assertEqual(case["resolution"]["commands_run"], ["old"])

    def test_render_report_includes_processed_resolution_record(self):
        store = {"schema_version": 3, "cases": [{
            "id": "x", "workflow": ".github/workflows/cost-flag.yml", "status": "processed",
            "primary_cause": "harness", "secondary_cause": "state-contamination",
            "processed_at": "2026-07-31T20:11:00Z",
            "resolution": {
                "method": "api-toggle",
                "commands_run": [
                    "gh api -X PUT .../312869658/disable",
                    "gh api -X PUT .../312869658/enable",
                ],
                "verify_pre": {"state": "active", "updated_at": "2026-07-14T18:11:40+09:00"},
                "verify_post": {"state": "active", "updated_at": "2026-07-31T20:10:31+09:00"},
            },
            "post_fix_scan": {"result": "clean", "fresh_failures": 0,
                              "last_run_url": "https://example/1"},
            "occurrences": [{"commit": "a"}],
        }]}
        out = self.mod.render_report(store)
        self.assertIn("[processed]", out)
        self.assertIn("api-toggle", out)
        self.assertIn("/disable", out)
        self.assertIn("/enable", out)
        self.assertIn("result=clean", out)

    def test_historical_failures_before_fix_do_not_block_processed_transition(self):
        """Regression for the live case 1b71f09a5926: a workflow with
        a long history of failure runs (all 0-job 'phantom trigger'
        entries from before the toggle) must transition to `processed`
        once a fresh post-fix scan shows zero NEW failures. The verify
        scan's `since_iso` is the fix_applied_at timestamp recorded in
        the resolution block."""
        def fake_run_with_history(cmd):
            if cmd[:2] == ["gh", "api"] and "/actions/workflows/312869658" in cmd[-1]:
                if "/disable" in cmd[-1] or "/enable" in cmd[-1]:
                    return ""
                return self._workflow_get()
            if cmd[:3] == ["gh", "run", "list"]:
                # Five historical failures (createdAt way before now),
                # zero failures after the fix.
                return json.dumps([
                    {"databaseId": 1, "conclusion": "failure",
                     "createdAt": "2026-07-31T08:00:00Z", "url": "https://example/1"},
                    {"databaseId": 2, "conclusion": "failure",
                     "createdAt": "2026-07-31T08:01:00Z", "url": "https://example/2"},
                    {"databaseId": 3, "conclusion": "failure",
                     "createdAt": "2026-07-31T08:02:00Z", "url": "https://example/3"},
                    {"databaseId": 4, "conclusion": "failure",
                     "createdAt": "2026-07-31T08:03:00Z", "url": "https://example/4"},
                    {"databaseId": 5, "conclusion": "failure",
                     "createdAt": "2026-07-31T08:04:00Z", "url": "https://example/5"},
                ])
            if cmd[:2] == ["git", "log"]:
                return ""
            raise AssertionError(f"unexpected cmd: {cmd}")

        with tempfile.TemporaryDirectory() as d:
            store_path = self._seeded_store(Path(d))
            with patch.object(self.mod, "_run", side_effect=fake_run_with_history):
                summary = self.mod.process(
                    auto_fix=True, verify_window=10, store_path=store_path,
                )

            self.assertEqual(len(summary["processed"]), 1)
            self.assertEqual(summary["still_open"], [])
            case = self.mod.find_case(self.mod.load_store(store_path), self.case_id)
            self.assertEqual(case["status"], "processed")
            # fresh_failures is 0 (all 5 historical failures predate fix_applied_at).
            self.assertEqual(case["post_fix_scan"]["fresh_failures"], 0)
            self.assertEqual(case["post_fix_scan"]["result"], "clean")


class TestFractionalSecondCutoff(unittest.TestCase):
    """`_signature_present_in_recent_runs` normalizes both sides of
    the cutoff to second precision. GitHub's `createdAt` carries
    fractional seconds (`...38.500Z`), but `_now()` returns
    `...38Z`. Lexicographically `.` < `Z`, so a fractional-second
    post-fix run would be filtered OUT of `fresh_failures` if
    compared raw."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def test_fractional_second_created_at_is_not_filtered_out(self):
        def fake_run(cmd):
            if cmd[:3] == ["gh", "run", "list"]:
                return json.dumps([{
                    "databaseId": 1, "conclusion": "failure",
                    # Same second as fix_applied_at, but with .500
                    "createdAt": "2026-07-31T11:16:38.500Z",
                    "url": "https://example/1",
                }])
            raise AssertionError(f"unexpected cmd: {cmd}")

        with patch.object(self.mod, "_run", side_effect=fake_run):
            result = self.mod._signature_present_in_recent_runs(
                "x.yml", since_iso="2026-07-31T11:16:38Z", verify_window=10,
            )

        self.assertEqual(result["fresh_failures"], 1,
                         "fractional-second createdAt must not be filtered out by the second-precision cutoff")


class TestVerifyWindowArgparseClamp(unittest.TestCase):
    """`--verify-window` is clamped to [1, 1000]. `0` would silently
    produce an empty `gh run list` (making every case look clean),
    and negatives would produce a negative --limit that errors."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def _clamp(self, raw: str) -> int:
        # _clamped_verify_window is defined inside _cli() to close over
        # argparse; the clamp logic itself is what we test here.
        # Use the script's CLI via subprocess to exercise the real
        # argparse parse path.
        import subprocess
        proc = subprocess.run(
            ["python3", str(LIB / "ci_triage.py"),
             "--store", "/tmp/__nonexistent_ci_triage_store__.json",
             "process", "--verify-window", raw],
            capture_output=True, text=True,
        )
        # argparse error exits 2 with a usage line on stderr.
        self.assertEqual(proc.returncode, 2,
                         f"verify-window={raw!r} should be rejected at argparse, "
                         f"got exit {proc.returncode}, stderr: {proc.stderr}")
        self.assertIn("--verify-window", proc.stderr)

    def test_zero_rejected(self):
        self._clamp("0")

    def test_negative_rejected(self):
        self._clamp("-1")

    def test_above_max_rejected(self):
        self._clamp("1001")

    def test_within_range_accepted(self):
        # 1 and 1000 should parse cleanly. The subprocess will then
        # proceed to process() — which fails on the missing store —
        # but the exit code is non-2 (specifically: 1 from process()
        # raising on the missing store, OR 0 if we mocked the run).
        # We only care that argparse accepted the value, so we test
        # the subprocess exit is NOT 2.
        import subprocess
        for raw in ("1", "1000"):
            proc = subprocess.run(
                ["python3", str(LIB / "ci_triage.py"),
                 "--store", "/tmp/__nonexistent_ci_triage_store__.json",
                 "process", "--verify-window", raw],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 2,
                                f"verify-window={raw!r} should be accepted, got stderr: {proc.stderr}")


class TestDeadCodeRemoved(unittest.TestCase):
    """`_verify_scan` (dead) and `_workflow_path` (no-op) were
    removed after review. Their absence is part of the contract —
    a future refactor that re-adds them is going off-script."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def test_no_verify_scan_helper(self):
        self.assertFalse(hasattr(self.mod, "_verify_scan"),
                         "_verify_scan was dead code; process() never called it")

    def test_no_workflow_path_helper(self):
        self.assertFalse(hasattr(self.mod, "_workflow_path"),
                         "_workflow_path was a no-op pass-through; inlined at call site")


class TestResolutionRecordMethodHint(unittest.TestCase):
    """The `method_hint` parameter was redundant — `_resolution_record`
    re-derived it from the proposal via `_workflow_id_from_proposal`
    internally. Caller passed a value that was never trusted."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        import ci_triage
        self.mod = ci_triage

    def test_resolution_record_takes_no_method_hint_kwarg(self):
        import inspect
        sig = inspect.signature(self.mod._resolution_record)
        self.assertEqual(len(sig.parameters), 1,  # self not counted in py3.13, but here it's module-level func
                         "_resolution_record now takes only `case` — no method_hint")
        # The single parameter should be `case`
        self.assertIn("case", sig.parameters)


class TestSummarySubcommand(unittest.TestCase):
    """`summary` wraps `scan` + `report` in one CLI call. It's the
    "just tell me what's in the store now" flow: a fresh scan bumps
    already-known signatures, then renders the full store. No judging
    step happens here — that's still a separate `record --from-json`
    call."""

    def setUp(self):
        sys.path.insert(0, str(LIB))
        from ci_triage import _cli, render_report
        self._cli = _cli
        self.render_report = render_report

    def _make_store(self, tmp: Path) -> Path:
        """Write a minimal store file so `report` has something to render."""
        store_path = tmp / "store.json"
        store_path.write_text(json.dumps({
            "schema_version": 3,
            "cases": [{
                "id": "sig_test_001",
                "workflow": "test.yml",
                "status": "open",
                "primary_cause": "harness",
                "secondary_cause": "state-contamination",
                "evidence": "synthetic evidence for the test",
                "repro": "echo synthetic",
                "regression_test": "N/A: synthetic test case",
                "proposal": "no-op",
                "hook_proposal": None,
                "occurrences": [{"commit": "deadbeef00000000000000000000000000000000",
                                 "run_id": 1, "date": "2026-08-24T00:00:00Z",
                                 "url": "https://example/runs/1"}],
            }],
        }))
        return store_path

    def test_no_scan_renders_store_without_calling_scan(self):
        """`summary --no-scan` skips the scan and just prints the report.
        scan() must not be invoked."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            store_path = self._make_store(tmp)
            with patch("ci_triage.scan") as scan_mock, \
                 patch("sys.argv", ["ci_triage", "--store", str(store_path),
                                    "summary", "--no-scan"]):
                rc = self._cli()
            self.assertEqual(rc, 0)
            scan_mock.assert_not_called()

    def test_no_scan_output_contains_existing_case(self):
        """`summary --no-scan` output must include the rendered case."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            store_path = self._make_store(tmp)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with patch("sys.argv", ["ci_triage", "--store", str(store_path),
                                    "summary", "--no-scan"]), \
                 redirect_stdout(buf):
                self._cli()
            out = buf.getvalue()
            self.assertIn("sig_test_001", out)
            self.assertIn("harness", out)

    def test_count_invokes_scan_then_renders_report(self):
        """`summary --count N` must call scan() with N, then render the
        post-scan store. The scan mock returns no new failures."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            store_path = self._make_store(tmp)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            fake_scan = {
                "commits": [],
                "unjudged": [],
                "already_known": [],
            }
            with patch("ci_triage.scan", return_value=fake_scan) as scan_mock, \
                 patch("sys.argv", ["ci_triage", "--store", str(store_path),
                                    "summary", "--count", "5"]), \
                 redirect_stdout(buf):
                rc = self._cli()
            self.assertEqual(rc, 0)
            scan_mock.assert_called_once()
            # scan() is keyword-only; check the kwargs.
            _, kwargs = scan_mock.call_args
            self.assertEqual(kwargs.get("count"), 5)
            self.assertEqual(kwargs.get("store_path"), store_path)
            self.assertIsNone(kwargs.get("commits"))
            out = buf.getvalue()
            # After scan (which bumped no new), the report still includes
            # the pre-existing case we wrote into the store.
            self.assertIn("sig_test_001", out)


if __name__ == "__main__":
    unittest.main()
