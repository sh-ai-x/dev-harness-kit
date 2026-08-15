"""test_skill_usage.py -- unit tests for tools/skill_usage.py.

Coverage:
- attributionSkill counts vs Skill tool_use invocations are tracked
  separately (two distinct signals).
- Window filter applies to both signals.
- cwd prefix filter scopes results to a target workspace, with a
  boundary-aware match that rejects sibling prefixes.
- Empty / malformed lines are tolerated (no crash).
- last_seen timestamp is the maximum observed per skill.
- Per-cwd breakdown is preserved when requested.
- Brace alternatives in --logs-glob (``logs/{a,b}/**/*.jsonl``) are
  resolved by ``_iter_logs``.

Tests that read the calendar-windowed fixture inject ``now=_REF_NOW``
so they don't rot as real time advances.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import skill_usage  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "skill_usage" / "mixed.jsonl"


def _fixture_max_ts() -> _dt.datetime:
    """Return the maximum ISO timestamp in ``FIXTURE``.

    Computed once per process from the on-disk fixture so the suite
    tracks any future edits to the fixture's date span.
    """
    max_ts: _dt.datetime | None = None
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("timestamp")
            if not isinstance(ts, str) or not ts:
                continue
            parsed = skill_usage._parse_iso(ts)
            if parsed is None:
                continue
            if max_ts is None or parsed > max_ts:
                max_ts = parsed
    if max_ts is None:
        raise RuntimeError("fixture contained no parseable timestamps")
    return max_ts


# Reference 'now' for windowed tests: fixture max + 7 days. Chosen so:
# - window=30 keeps the oldest record (within 30d of max_ts + 7d)
# - window=20 drops the records at 2026-06-20 / 2026-06-26
_REF_NOW = _fixture_max_ts() + _dt.timedelta(days=7)


class TestAggregateCounts(unittest.TestCase):
    def test_window_default_captures_recent_turns(self):
        agg = skill_usage.aggregate_skill_usage(str(FIXTURE), window_days=30,
                                                 now=_REF_NOW)
        self.assertEqual(agg["dev-kit:inspect"]["turns"], 4)
        self.assertEqual(agg["dev-kit:inspect"]["invocations"], 0)
        self.assertEqual(agg["dev-kit:feat-fix"]["turns"], 2)
        self.assertEqual(agg["dev-kit:feat-fix"]["invocations"], 2)
        self.assertEqual(agg["dev-kit:babysit-pr"]["turns"], 1)
        self.assertEqual(agg["dev-kit:babysit-pr"]["invocations"], 1)
        self.assertEqual(agg["dev-kit:prune"]["turns"], 1)
        self.assertEqual(agg["dev-kit:prune"]["invocations"], 0)

    def test_window_filter_excludes_old_records(self):
        agg = skill_usage.aggregate_skill_usage(str(FIXTURE), window_days=20,
                                                 now=_REF_NOW)
        self.assertEqual(agg["dev-kit:inspect"]["turns"], 3)
        self.assertNotIn("dev-kit:prune", agg)

    def test_cwd_prefix_filter_scopes_to_workspace(self):
        agg = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30, cwd_prefix="/repo/dev-harness-kit",
            now=_REF_NOW)
        self.assertEqual(agg["dev-kit:inspect"]["turns"], 3)
        self.assertEqual(agg["dev-kit:feat-fix"]["turns"], 2)
        self.assertEqual(agg["dev-kit:feat-fix"]["invocations"], 2)
        self.assertNotIn("dev-kit:babysit-pr", agg)

    def test_cwd_prefix_does_not_match_sibling(self):
        """A prefix like ``/repo/dev-harness-kit`` must not match
        ``/repo/dev-harness-kit-old``."""
        agg = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30,
            cwd_prefix="/repo/dev-harness-kit-old", now=_REF_NOW)
        self.assertEqual(agg, {})

    def test_cwd_prefix_trailing_slash_normalised(self):
        """Both ``/repo/x`` and ``/repo/x/`` should match the same records."""
        agg_no = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30, cwd_prefix="/repo/dev-harness-kit",
            now=_REF_NOW)
        agg_yes = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30, cwd_prefix="/repo/dev-harness-kit/",
            now=_REF_NOW)
        self.assertEqual(agg_no, agg_yes)

    def test_last_seen_is_max_observed(self):
        agg = skill_usage.aggregate_skill_usage(str(FIXTURE), window_days=30,
                                                 now=_REF_NOW)
        self.assertEqual(agg["dev-kit:inspect"]["last_seen"],
                         "2026-07-10T10:00:25.000Z")
        self.assertEqual(agg["dev-kit:feat-fix"]["last_seen"],
                         "2026-07-10T10:00:20.000Z")

    def test_per_cwd_breakdown_when_requested(self):
        agg = skill_usage.aggregate_skill_usage(
            str(FIXTURE), window_days=30, include_per_cwd=True, now=_REF_NOW)
        inspect = agg["dev-kit:inspect"]
        self.assertIn("cwds", inspect)
        self.assertEqual(inspect["cwds"]["/repo/dev-harness-kit"]["turns"], 3)
        self.assertEqual(inspect["cwds"]["/repo/other-project"]["turns"], 1)


class TestMalformedInput(unittest.TestCase):
    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30)
            self.assertEqual(agg, {})
        finally:
            Path(path).unlink()

    def test_blank_and_malformed_lines_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n")
            fh.write("not json\n")
            fh.write(json.dumps({"type": "assistant", "message": {}}) + "\n")
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30)
            self.assertEqual(agg, {})
        finally:
            Path(path).unlink()


class TestSkillNameExtraction(unittest.TestCase):
    def test_explicit_skill_tool_use_without_attribution_still_counts(self):
        # Timestamp built relative to ``_REF_NOW`` (windowed-fixture
        # reference time) so the record stays inside the 30-day window
        # regardless of when the suite runs (calendar-rot fix).
        ts = (_REF_NOW - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({
                "type": "assistant", "isSidechain": False,
                "sessionId": "x", "cwd": "/r",
                "timestamp": ts,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Skill",
                     "input": {"skill": "dev-kit:foo"}}
                ]}
            }) + "\n")
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30, now=_REF_NOW)
            self.assertEqual(agg["dev-kit:foo"]["invocations"], 1)
            self.assertEqual(agg["dev-kit:foo"]["turns"], 0)
        finally:
            Path(path).unlink()

    def test_invalid_skill_field_in_tool_use_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({
                "type": "assistant", "isSidechain": False,
                "sessionId": "x", "cwd": "/r",
                "timestamp": "2026-07-15T10:00:00.000Z",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Skill",
                     "input": {"skill": 123}}
                ]}
            }) + "\n")
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30)
            self.assertEqual(agg, {})
        finally:
            Path(path).unlink()


class TestNormalizeUsageRecord(unittest.TestCase):
    """The Codex wire shape wraps several fields under ``payload.*``
    while leaving ``attributionSkill`` at the top level. The pre-refactor
    aggregator only read top-level ``timestamp`` / ``cwd`` so any record
    whose timestamp lived under ``payload`` was silently dropped as
    undated -- the skill showed up as a delete candidate even though it
    was used. The normalizer must resolve both layers."""

    def test_top_level_fields(self):
        rec = {"attributionSkill": "dev-kit:foo",
               "timestamp": "2026-07-15T10:00:00.000Z",
               "cwd": "/repo/x"}
        norm = skill_usage._normalize_usage_record(rec)
        self.assertEqual(norm.skill, "dev-kit:foo")
        self.assertEqual(norm.cwd, "/repo/x")
        self.assertEqual(norm.ts_str, "2026-07-15T10:00:00.000Z")
        self.assertIsNotNone(norm.ts)

    def test_extracts_nested_timestamp(self):
        """Codex shape: ``attributionSkill`` at top level, but
        ``timestamp`` / ``cwd`` nested under ``payload``."""
        rec = {"attributionSkill": "dev-kit:foo",
               "payload": {"timestamp": "2026-07-15T10:00:00.000Z",
                           "cwd": "/repo/x"}}
        norm = skill_usage._normalize_usage_record(rec)
        self.assertEqual(norm.skill, "dev-kit:foo")
        self.assertEqual(norm.cwd, "/repo/x")
        self.assertEqual(norm.ts_str, "2026-07-15T10:00:00.000Z")
        self.assertIsNotNone(norm.ts)

    def test_codex_nested_record_aggregates(self):
        """End-to-end: a Codex record with nested timestamp contributes
        to the aggregate (was dropped as undated before the refactor).

        The timestamp is built relative to ``_REF_NOW`` (windowed-fixture
        reference time) instead of a fixed calendar date so the test
        stays in the 30-day window regardless of when it runs.
        """
        ts = (_REF_NOW - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({
                "attributionSkill": "dev-kit:nested",
                "payload": {"timestamp": ts, "cwd": "/repo/x"}
            }) + "\n")
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30,
                                                    now=_REF_NOW)
            self.assertIn("dev-kit:nested", agg)
            self.assertEqual(agg["dev-kit:nested"]["turns"], 1)
        finally:
            Path(path).unlink()

    def test_non_string_skill_ignored(self):
        rec = {"attributionSkill": 42,
               "timestamp": "2026-07-15T10:00:00.000Z"}
        norm = skill_usage._normalize_usage_record(rec)
        self.assertEqual(norm.skill, "")

    def test_missing_payload_uses_top_level(self):
        rec = {"timestamp": "2026-07-15T10:00:00.000Z",
               "cwd": "/repo/x"}
        norm = skill_usage._normalize_usage_record(rec)
        self.assertEqual(norm.cwd, "/repo/x")
        self.assertEqual(norm.ts_str, "2026-07-15T10:00:00.000Z")


class TestPrintTable(unittest.TestCase):
    def test_print_table_renders_columns(self):
        agg = {
            "dev-kit:foo": {"turns": 5, "invocations": 2,
                            "last_seen": "2026-07-15T10:00:00.000Z"},
            "dev-kit:bar": {"turns": 1, "invocations": 1,
                            "last_seen": "2026-07-15T11:00:00.000Z"},
        }
        out = skill_usage.format_table(agg)
        self.assertIn("SKILL", out)
        self.assertIn("TURNS", out)
        self.assertIn("INVOCATIONS", out)
        self.assertIn("LAST_SEEN", out)
        foo_idx = out.index("dev-kit:foo")
        bar_idx = out.index("dev-kit:bar")
        self.assertLess(foo_idx, bar_idx)

    def test_print_json_emits_machine_readable(self):
        agg = {"dev-kit:foo": {"turns": 5, "invocations": 2,
                               "last_seen": "2026-07-15T10:00:00.000Z"}}
        out = skill_usage.format_json(agg)
        parsed = json.loads(out)
        self.assertEqual(parsed["dev-kit:foo"]["turns"], 5)
        self.assertEqual(parsed["dev-kit:foo"]["invocations"], 2)


class TestFilterByCwdPrefix(unittest.TestCase):
    def _agg(self):
        return {
            "dev-kit:foo": {
                "turns": 5, "invocations": 1,
                "last_seen": "2026-07-15T10:00:00.000Z",
                "cwds": {
                    "/repo/a": {"turns": 3, "invocations": 0,
                                "last_seen": "2026-07-14T10:00:00.000Z"},
                    "/repo/a/sub": {"turns": 2, "invocations": 1,
                                    "last_seen": "2026-07-15T10:00:00.000Z"},
                    "/repo/b": {"turns": 7, "invocations": 0,
                                "last_seen": "2026-07-15T10:00:00.000Z"},
                },
            },
            "dev-kit:bar": {
                "turns": 2, "invocations": 2,
                "last_seen": "2026-07-15T10:00:00.000Z",
                "cwds": {
                    "/repo/c": {"turns": 2, "invocations": 2,
                                "last_seen": "2026-07-15T10:00:00.000Z"},
                },
            },
        }

    def test_prefix_match_rolls_counts(self):
        agg = self._agg()
        out = skill_usage.filter_by_cwd_prefix(agg, "/repo/a")
        # /repo/a + /repo/a/sub both match; /repo/b does not.
        self.assertEqual(out["dev-kit:foo"]["turns"], 5)
        self.assertEqual(out["dev-kit:foo"]["invocations"], 1)
        # bar has no cwd under /repo/a -> dropped.
        self.assertNotIn("dev-kit:bar", out)

    def test_no_match_yields_empty(self):
        agg = self._agg()
        out = skill_usage.filter_by_cwd_prefix(agg, "/nope")
        self.assertEqual(out, {})

    def test_skips_aggregate_without_per_cwd(self):
        agg = {"dev-kit:x": {"turns": 5, "invocations": 0, "last_seen": None}}
        self.assertEqual(skill_usage.filter_by_cwd_prefix(agg, "/anything"), {})

    def test_empty_prefix_yields_empty(self):
        agg = self._agg()
        # Defensive: empty prefix would match everything; reject to
        # keep caller contract explicit.
        self.assertEqual(skill_usage.filter_by_cwd_prefix(agg, ""), {})

    def test_sibling_directory_does_not_match(self):
        """``/repo/a`` must not roll up ``/repo/a-old`` records."""
        agg = {
            "dev-kit:foo": {
                "turns": 0, "invocations": 0, "last_seen": None,
                "cwds": {
                    "/repo/a": {"turns": 3, "invocations": 0,
                                "last_seen": "2026-07-15T10:00:00.000Z"},
                    "/repo/a-old": {"turns": 9, "invocations": 0,
                                    "last_seen": "2026-07-15T10:00:00.000Z"},
                },
            },
        }
        out = skill_usage.filter_by_cwd_prefix(agg, "/repo/a")
        self.assertEqual(out["dev-kit:foo"]["turns"], 3)


class TestIterLogs(unittest.TestCase):
    def test_brace_alternative_walks_every_branch(self):
        """``logs/{a,b}/**/*.jsonl`` must walk both ``logs/a/`` and ``logs/b/``.

        Regression: previously the anchor split kept the literal
        ``logs/{a,b}`` path, which is not a directory, so zero files
        were yielded on a fresh checkout with both log sources present.
        """
        with tempfile.TemporaryDirectory() as root:
            root_p = Path(root)
            (root_p / "logs" / "a").mkdir(parents=True)
            (root_p / "logs" / "b").mkdir(parents=True)
            file_a = root_p / "logs" / "a" / "1.jsonl"
            file_b = root_p / "logs" / "b" / "2.jsonl"
            file_a.write_text("{}\n")
            file_b.write_text("{}\n")
            (root_p / "logs" / "c").mkdir(parents=True)  # present but not in brace

            old_cwd = os.getcwd()
            try:
                os.chdir(root_p)
                walked = sorted(p.name for p in skill_usage._iter_logs(
                    "logs/{a,b}/**/*.jsonl"))
            finally:
                os.chdir(old_cwd)
            self.assertEqual(walked, ["1.jsonl", "2.jsonl"])

    def test_brace_no_match_returns_empty(self):
        """When none of the brace alternatives exist, no files are
        yielded (and no exception is raised)."""
        with tempfile.TemporaryDirectory() as root:
            old_cwd = os.getcwd()
            try:
                os.chdir(root)
                walked = list(skill_usage._iter_logs(
                    "logs/{missing-a,missing-b}/**/*.jsonl"))
            finally:
                os.chdir(old_cwd)
            self.assertEqual(walked, [])


class TestNormalizeToolUses(unittest.TestCase):
    """`_iter_tool_uses(record)` flattens Claude/Codex tool_use blocks
    into a uniform sequence so the aggregation loop does not have to
    branch on record shape.

    Claude-Code nests blocks under ``record.message.content`` (list of
    dicts). Codex nests them under ``record.payload`` (sometimes a list
    itself). Some intermediate builds nest one level deeper (``content``
    inside a wrapper). The normalizer must yield each block exactly
    once and skip non-tool-use / non-dict entries silently."""

    def test_claude_message_content_blocks(self):
        rec = {"message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "Skill",
             "input": {"skill": "dev-kit:foo"}},
        ]}}
        out = list(skill_usage._iter_tool_uses(rec))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["input"]["skill"], "dev-kit:foo")

    def test_codex_payload_list(self):
        rec = {"payload": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "dev-kit:bar"}},
        ]}
        out = list(skill_usage._iter_tool_uses(rec))
        self.assertEqual(out[0]["input"]["skill"], "dev-kit:bar")

    def test_codex_payload_dict_with_tool_uses_key(self):
        rec = {"payload": {"tool_uses": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "dev-kit:baz"}},
        ]}}
        out = list(skill_usage._iter_tool_uses(rec))
        self.assertEqual(out[0]["input"]["skill"], "dev-kit:baz")

    def test_nested_content_is_flattened(self):
        """Some Codex/Claude builds wrap blocks one level deeper
        (e.g. ``content`` inside a top-level dict). The normalizer
        must walk both layers."""
        rec = {"message": {"content": {"content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "dev-kit:deep"}},
        ]}}}
        out = list(skill_usage._iter_tool_uses(rec))
        self.assertEqual(out[0]["input"]["skill"], "dev-kit:deep")

    def test_skips_non_tool_use_blocks(self):
        rec = {"message": {"content": [
            {"type": "text", "text": "ignored"},
            {"type": "tool_use", "name": "Read", "input": {}},
        ]}}
        out = list(skill_usage._iter_tool_uses(rec))
        # Normalizer yields tool_use blocks of any name; the
        # aggregator (not the normalizer) filters by name=="Skill".
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Read")

    def test_no_message_no_payload_yields_empty(self):
        self.assertEqual(list(skill_usage._iter_tool_uses({})), [])
        self.assertEqual(list(skill_usage._iter_tool_uses(
            {"type": "user", "message": "hi"})), [])

    def test_aggregation_picks_up_codex_skill_kicks(self):
        """End-to-end: a Codex-shaped record carrying a Skill tool_use
        must bump ``invocations`` for that skill, matching the Claude
        contract. Without normalization the Codex kick would silently
        be dropped because ``message.content`` is absent."""
        # Timestamp built relative to ``_REF_NOW`` (windowed-fixture
        # reference time) so the record stays inside the 30-day window
        # regardless of when the suite runs (calendar-rot fix).
        ts = (_REF_NOW - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as fh:
            fh.write(json.dumps({
                "type": "event_msg",
                "timestamp": ts,
                "cwd": "/r",
                "payload": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "dev-kit:codex-only"}},
                ],
            }) + "\n")
            path = fh.name
        try:
            agg = skill_usage.aggregate_skill_usage(path, window_days=30, now=_REF_NOW)
            self.assertEqual(agg["dev-kit:codex-only"]["invocations"], 1)
            self.assertEqual(agg["dev-kit:codex-only"]["turns"], 0)
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
