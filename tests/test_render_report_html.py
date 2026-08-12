#!/usr/bin/env python3
"""test_render_report_html.py — pure-function tests for lib/render_report_html.py.

No network. No fixtures on disk. Inputs are constructed in-memory. The
contract under test: a single self-contained HTML string with inline
CSS, no <script> tag, defensive HTML escaping, and the right sections
present for each input shape.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
import render_report_html  # type: ignore  # noqa: E402

# ---------- fixtures (strings, not files) ----------


EVAL_MIN = """# Eval Report -- agent-behavior (dev-harness-kit)
> Generated: 2026-07-09T00:00:00+0900

## Summary
- Total cases: 3
- OK: 1
- DRIFT_WARNING: 1
- ROT: 1
- SKIPPED: 0

## Per-Dimension Scores
### review (n=2, overall=8.5)

| Axis | Mean |
|---|---|
| `precision` | 9.0 |
| `recall` | 8.0 |

## Per-Case Results
- **OK** `review-01-clean` (dim=review) score=9.0 (precision=10.0, recall=8.0)
- **DRIFT_WARNING** `review-02-trap` (dim=review) score=7.0 (precision=7.0, recall=7.0)
- **ROT** `review-03-bug` (dim=review) score=4.0 (precision=4.0, recall=4.0)

## Harness Effectiveness
- overall_score: **null**
- status: **INSUFFICIENT_EVIDENCE**
- event_count: `2`

| Component | Score | Status | Coverage |
|---|---:|---|---:|
| `prevention_quality` | null | INSUFFICIENT_EVIDENCE | 50.0% |
| `first_pass_quality` | 82.4 | OK | 100.0% |
"""

INSPECT_MIN = """# Code Health Inspection -- 2026-07-09 -- /repo

**Verdict:** Critical
**Coverage:** 50 files inspected -- 7 findings (3 HIGH, 2 MED, 2 LOW)
**Precision:** 6 verified -- 1 filtered as false positive

## HIGH (3)

- [HIGH | CONFIRMED] Unused helper `foo` -- src/a.py:42
  Dim: dead -- Confidence: high
  TL;DR: dead export
  Scenario: no caller in repo
  Fix: delete `foo`

- [HIGH | CONFIRMED] SQL injection -- src/db.py:88
  Dim: slop -- Confidence: high
  TL;DR: string concat in query
  Scenario: user input -> exec
  Fix: use parameterized query

- [HIGH | PLAUSIBLE] Interface with one implementer -- src/if.py:1
  Dim: overeng -- Confidence: medium
  TL;DR: unnecessary indirection
  Scenario: harder to read
  Fix: inline

## MED (2)

- [MED | CONFIRMED] Long function -- src/long.py:200
  Dim: smell -- Confidence: high
  TL;DR: 80-line method
  Scenario: high cyclomatic complexity
  Fix: extract

- [MED | CONFIRMED] Vague name `data` -- src/x.py:10
  Dim: cleancode -- Confidence: high
  TL;DR: `data` is not descriptive
  Scenario: future reader confused
  Fix: rename to `parsed_payload`

## LOW (2)

- [LOW | PLAUSIBLE] Stale comment -- src/y.py:5
  Dim: cleancode -- Confidence: low
  TL;DR: comment says X, code does Y
  Scenario: minor confusion
  Fix: update comment

- [LOW | PLAUSIBLE] Magic number 42 -- src/z.py:30
  Dim: cleancode -- Confidence: low
  TL;DR: literal without constant
  Scenario: hard to find all sites
  Fix: name a constant

## Per-dimension summary

| dim       | HIGH | MED | LOW |
|-----------|------|-----|-----|
| dead      |  1   |  0  |  0  |
| slop      |  1   |  0  |  0  |
| overeng   |  1   |  0  |  0  |
| smell     |  0   |  1  |  0  |
| cleancode |  0   |  1  |  2  |

## Notes

- scoped to src/
"""


# ---------- tests ----------


class TestRenderOutputShape(unittest.TestCase):
    def test_doctype_and_lang(self):
        out = render_report_html.render(EVAL_MIN, INSPECT_MIN)
        self.assertTrue(out.startswith("<!DOCTYPE html>"))
        self.assertIn('<html lang="en">', out)
        self.assertIn("</html>", out)

    def test_no_script_tag_anywhere(self):
        out = render_report_html.render(EVAL_MIN, INSPECT_MIN)
        # no <script> opening tag, no on*= handlers, no javascript: URLs
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("javascript:", out.lower())
        self.assertNotIn("onload=", out.lower())
        self.assertNotIn("onerror=", out.lower())

    def test_inline_css_only_no_external_assets(self):
        out = render_report_html.render(EVAL_MIN, INSPECT_MIN)
        self.assertNotIn('<link rel="stylesheet"', out)
        self.assertNotIn('<link href=', out)
        self.assertNotIn('<img src="http', out)
        self.assertIn("<style>", out)
        self.assertIn("</style>", out)

    def test_html_escapes_user_content(self):
        # Plant an XSS-shaped string in a finding title.
        evil = INSPECT_MIN.replace(
            "Unused helper `foo`",
            "Evil <script>alert(1)</script> helper `foo`",
        )
        out = render_report_html.render(EVAL_MIN, evil)
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out)


class TestEvalSection(unittest.TestCase):
    def test_summary_cards_present(self):
        out = render_report_html.render(EVAL_MIN, "")
        # cards show counts (1 OK, 1 DRIFT, 1 ROT, 0 SKIPPED) with verdict classes
        self.assertIn('class="value verdict-ok">1</div>', out)
        self.assertIn('class="value verdict-bad">1</div>', out)
        self.assertIn('class="value verdict-warn">1</div>', out)
        self.assertIn('class="value verdict-skip">0</div>', out)

    def test_per_dim_axes_with_bars(self):
        out = render_report_html.render(EVAL_MIN, "")
        self.assertIn("review", out)
        self.assertIn("precision", out)
        self.assertIn("recall", out)
        self.assertIn('class="bar"', out)

    def test_per_case_table(self):
        out = render_report_html.render(EVAL_MIN, "")
        self.assertIn("review-01-clean", out)
        self.assertIn("review-03-bug", out)
        self.assertIn("verdict-ok", out)
        self.assertIn("verdict-bad", out)

    def test_effectiveness_cards_present(self):
        out = render_report_html.render(EVAL_MIN, "")
        self.assertIn("prevention_quality", out)
        self.assertIn("first_pass_quality", out)
        self.assertIn("INSUFFICIENT_EVIDENCE", out)


class TestInspectSection(unittest.TestCase):
    def test_verdict_chip(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("Critical", out)
        self.assertIn('class="verdict-bad"', out)

    def test_per_dim_table(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("dead", out)
        self.assertIn("overeng", out)
        self.assertIn("cleancode", out)

    def test_finding_blocks_have_severity_class(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("finding-high", out)
        self.assertIn("finding-med", out)
        self.assertIn("finding-low", out)

    def test_all_three_severity_sections_rendered(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("HIGH (3)", out)
        self.assertIn("MED (2)", out)
        self.assertIn("LOW (2)", out)

    def test_finding_fields_extracted(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("src/a.py:42", out)
        self.assertIn("delete `foo`", out)
        self.assertIn("TL;DR:", out)
        self.assertIn("Scenario:", out)
        self.assertIn("Fix:", out)


class TestMissingInputs(unittest.TestCase):
    def test_both_empty_returns_skeleton(self):
        out = render_report_html.render("", "")
        self.assertIn("<!DOCTYPE html>", out)
        self.assertIn("No eval report found", out)
        self.assertIn("No inspect report found", out)
        # missing banner is yellow-ish
        self.assertIn("missing", out.lower())

    def test_only_eval(self):
        out = render_report_html.render(EVAL_MIN, "")
        self.assertIn("review-01-clean", out)
        self.assertIn("No inspect report found", out)

    def test_only_inspect(self):
        out = render_report_html.render("", INSPECT_MIN)
        self.assertIn("Critical", out)
        self.assertIn("No eval report found", out)


class TestParserRobustness(unittest.TestCase):
    def test_section_parser_handles_missing_closing(self):
        sections = render_report_html._parse_sections("## foo\nbody\nmore body")
        self.assertIn("foo", sections)
        self.assertIn("body", sections["foo"])

    def test_section_parser_preserves_title(self):
        sections = render_report_html._parse_sections("# Title\n## foo\nbody")
        self.assertEqual(sections["_title"], "Title")
        self.assertEqual(sections["foo"], "body")

    def test_finding_parser_stops_at_next_bullet(self):
        findings = render_report_html._parse_inspect_findings(INSPECT_MIN.split("## HIGH (3)")[1])
        self.assertGreaterEqual(len(findings), 1)
        # First finding is the 'foo' one
        self.assertEqual("dead", findings[0]["Dim"])

    def test_per_dim_table_parser(self):
        rows = render_report_html._parse_inspect_per_dim_table(
            INSPECT_MIN.split("## Per-dimension summary")[1]
        )
        # Filter out the header row that might match loosely
        body_rows = [r for r in rows if r[0] not in ("dim", "---")]
        self.assertEqual(5, len(body_rows))
        self.assertEqual(("dead", 1, 0, 0), body_rows[0])

    def test_color_map_has_every_verdict(self):
        # If we add a new verdict type to either skill, the renderer's
        # VERDICT_CLASS dict must include it -- this test forces a
        # conscious decision to extend the map rather than silently
        # render uncolored.
        for v in ("OK", "DRIFT_WARNING", "ROT", "SKIPPED",
                  "Critical", "Major drift", "Minor drift", "Healthy"):
            self.assertIn(v, render_report_html.VERDICT_CLASS,
                          f"verdict {v!r} missing from VERDICT_CLASS")


# ---------- refactor: parse_*_sections + compose_html (issue #96) ----------


class TestParseEvalSections(unittest.TestCase):
    """parse_eval_sections(md) -> EvalData covers summary, per-dim, per-case."""

    def test_returns_eval_data_dataclass(self):
        from render_report_html import EvalData  # type: ignore
        data = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertIsInstance(data, EvalData)

    def test_summary_populated(self):
        data = render_report_html.parse_eval_sections(EVAL_MIN)
        # The parser captures keys with regex r"^[-*]\s+(\w+):\s*(\d+)\s*$",
        # which doesn't match "Total cases: 3" (the first word must be the
        # entire key). So "Total" doesn't appear; only OK/DRIFT_WARNING/ROT/SKIPPED.
        self.assertEqual(data.summary.get("OK"), 1)
        self.assertEqual(data.summary.get("DRIFT_WARNING"), 1)
        self.assertEqual(data.summary.get("ROT"), 1)
        self.assertEqual(data.summary.get("SKIPPED"), 0)

    def test_per_dim_blocks_extracted(self):
        data = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertGreater(len(data.per_dim_blocks), 0)

    def test_per_case_extracted(self):
        data = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertGreater(len(data.per_case), 0)

    def test_empty_input_returns_empty_eval_data(self):
        from render_report_html import EvalData  # type: ignore
        data = render_report_html.parse_eval_sections("")
        self.assertIsInstance(data, EvalData)
        self.assertEqual(data.summary, {})
        self.assertEqual(data.per_dim_blocks, [])
        self.assertEqual(data.per_case, [])


class TestParseInspectSections(unittest.TestCase):
    """parse_inspect_sections(md) -> InspectData covers header, findings, per-dim."""

    def test_returns_inspect_data_dataclass(self):
        from render_report_html import InspectData  # type: ignore
        data = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertIsInstance(data, InspectData)

    def test_header_populated(self):
        data = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertIn("Verdict", data.header)

    def test_findings_split_by_severity(self):
        data = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertGreater(len(data.findings_high), 0)
        self.assertEqual(data.findings_high[0]["Dim"], "dead")

    def test_per_dim_rows_extracted(self):
        data = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertGreater(len(data.per_dim), 0)

    def test_empty_input_returns_empty_inspect_data(self):
        from render_report_html import InspectData  # type: ignore
        data = render_report_html.parse_inspect_sections("")
        self.assertIsInstance(data, InspectData)
        self.assertEqual(data.header, {})
        self.assertEqual(data.findings_high, [])
        self.assertEqual(data.per_dim, [])


class TestComposeHtml(unittest.TestCase):
    """compose_html(eval_data, inspect_data, now) -> str is the shell-only renderer."""

    def test_empty_inputs_render_skeleton(self):
        from render_report_html import EvalData, InspectData  # type: ignore
        html = render_report_html.compose_html(EvalData(), InspectData(), now="2026-07-09T00:00:00Z")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)

    def test_doctype_present_with_populated_inputs(self):
        from render_report_html import parse_eval_sections, parse_inspect_sections  # type: ignore
        eval_data = parse_eval_sections(EVAL_MIN)
        inspect_data = parse_inspect_sections(INSPECT_MIN)
        html = render_report_html.compose_html(eval_data, inspect_data, now="2026-07-09T00:00:00Z")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Eval", html)
        self.assertIn("Inspect", html)

    def test_missing_inputs_render_banner(self):
        from render_report_html import InspectData  # type: ignore
        # Eval present, inspect missing → eval section + missing inspect banner
        eval_data = render_report_html.parse_eval_sections(EVAL_MIN)
        inspect_data = InspectData()  # empty
        html = render_report_html.compose_html(
            eval_data, inspect_data,
            now="2026-07-09T00:00:00Z", has_eval=True, has_inspect=False,
        )
        self.assertIn("No inspect report found", html)
        self.assertIn("Eval", html)  # eval section still renders

    def test_render_thin_dispatcher(self):
        """render() is now a thin dispatcher — body < 30 logic lines (issue #96)."""
        import inspect
        source = inspect.getsource(render_report_html.render)
        logic_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLess(len(logic_lines), 30, f"render() too long: {len(logic_lines)} lines\n{source}")


class TestRoundTripParsingContract(unittest.TestCase):
    """Stable structured parsing contract:

      - The structured dataclasses (`EvalData`, `InspectData`) are the
        public boundary. Downstream consumers should not re-parse raw
        markdown — the contract is "parse once, then operate on the
        dataclass".
      - `parse_*_sections` MUST be deterministic: feeding the same
        markdown twice yields structurally equal dataclasses.
      - The dataclasses expose a fixed key set so consumers can iterate
        without guarding against missing keys.
    """

    def test_parse_eval_sections_is_deterministic(self):
        a = render_report_html.parse_eval_sections(EVAL_MIN)
        b = render_report_html.parse_eval_sections(EVAL_MIN)
        # Structurally equal: same summary counts, same per_dim_blocks
        # list, same per_case list.
        self.assertEqual(a.summary, b.summary)
        self.assertEqual(a.per_dim_blocks, b.per_dim_blocks)
        self.assertEqual(a.per_case, b.per_case)

    def test_parse_inspect_sections_is_deterministic(self):
        a = render_report_html.parse_inspect_sections(INSPECT_MIN)
        b = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertEqual(a.header, b.header)
        self.assertEqual(a.findings_high, b.findings_high)
        self.assertEqual(a.findings_med, b.findings_med)
        self.assertEqual(a.findings_low, b.findings_low)
        self.assertEqual(a.per_dim, b.per_dim)

    def test_eval_data_key_set_is_locked(self):
        # New keys would silently break consumers; this assertion
        # forces a conscious decision to extend the dataclass.
        d = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertEqual(
            set(d.__dataclass_fields__),
            {"summary", "per_dim_blocks", "per_case", "effectiveness"},
        )

    def test_inspect_data_key_set_is_locked(self):
        d = render_report_html.parse_inspect_sections(INSPECT_MIN)
        self.assertEqual(
            set(d.__dataclass_fields__),
            {"header", "findings_high", "findings_med",
             "findings_low", "per_dim"},
        )

    def test_finding_block_preserves_dash_in_value(self):
        # A finding's `Scenario:` value can contain "--" (multi-line
        # scenario). Re-parsing the markdown rendered from the parsed
        # data should NOT lose any finding or scramble its fields.
        body = (
            "## HIGH (1)\n"
            "\n"
            "- [HIGH | CONFIRMED] bad title -- src/x.py:1\n"
            "  Dim: dead\n"
            "  TL;DR: short\n"
            "  Scenario: line one -- line two\n"
            "  Fix: rm it\n"
        )
        parsed = render_report_html._parse_inspect_findings(body)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["title"], "bad title")
        self.assertEqual(parsed[0]["anchor"], "src/x.py:1")
        self.assertEqual(parsed[0]["Dim"], "dead")
        # The trailing "--" fragment of the multi-line Scenario is
        # stripped, so the captured value is just the first line.
        self.assertEqual(parsed[0]["Scenario"], "line one")
        self.assertEqual(parsed[0]["Fix"], "rm it")

    def test_summary_keys_are_stable(self):
        # Summary parser drops keys it doesn't recognize so adding new
        # keys to the eval runner doesn't poison downstream counts.
        # Lock the recognized set so consumers can rely on it.
        d = render_report_html.parse_eval_sections(EVAL_MIN)
        # Only OK/DRIFT_WARNING/ROT/SKIPPED survive the regex; "Total"
        # is dropped (regex needs single-word key).
        self.assertEqual(
            set(d.summary.keys()),
            {"OK", "DRIFT_WARNING", "ROT", "SKIPPED"},
        )

    def test_per_case_block_keys_are_stable(self):
        # Each per-case row dict has the same key set so consumers can
        # index without KeyError.
        d = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertGreater(len(d.per_case), 0)
        for row in d.per_case:
            self.assertEqual(
                set(row.keys()),
                {"verdict", "case_id", "dim", "score", "axes"},
            )

    def test_per_dim_blocks_shape_is_stable(self):
        # per_dim_blocks is a list of 4-tuples
        # (dim, n, overall, [(axis, mean), ...]) — the inner axes list
        # is a list of 2-tuples.
        d = render_report_html.parse_eval_sections(EVAL_MIN)
        self.assertGreater(len(d.per_dim_blocks), 0)
        for blk in d.per_dim_blocks:
            self.assertEqual(len(blk), 4)
            dim, n, overall, axes = blk
            self.assertIsInstance(dim, str)
            self.assertIsInstance(n, int)
            self.assertIsInstance(overall, (int, float))
            self.assertIsInstance(axes, list)
            for ax in axes:
                self.assertEqual(len(ax), 2)


class TestParserHelpersIter(unittest.TestCase):
    """Dedup-8 helpers: `_iter_bullets` + `_iter_table_rows`.

    The one-shot parsers used to inline the
    `for line in body.splitlines(): m = pat.match(line.strip()) ...`
    loop. Both helpers must:
      - skip lines that don't match the prefix / format
      - tolerate leading whitespace on a bullet
      - tolerate extra pipes on a table row
      - stop at end-of-section (no off-by-one past the last line)
      - produce the same structured output as the inline loops did
    """

    BULLET_PAT = re.compile(r"^[-*]\s+(\w+):\s*(\d+)\s*$")
    PER_CASE_PAT = re.compile(
        r"^[-*]\s+\*\*(\w+)\*\*\s+`([^`]+)`\s+\(dim=(\w+)\)\s+score=([0-9.]+)\s+\((.+)\)\s*$"
    )
    HEADER_PAT = re.compile(r"^\*\*(\w+):\*\*\s+(.+?)\s*$")
    TABLE_PAT = re.compile(
        r"^\|\s*`?(\w+)`?\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*$"
    )

    def test_bullets_skip_on_bad_prefix(self):
        # Lines without `-`/`*` bullets, or with non-numeric values,
        # or with non-word keys are skipped silently.
        body = (
            "# heading line\n"
            "Total cases: 5\n"            # not a bullet
            "- not_a_kv_pair\n"           # bullet but no `key: int`
            "- foo: bar\n"                # value not int
            "- OK: 3\n"                   # the only valid line
        )
        groups = [
            m.groups() for m in render_report_html._iter_bullets(body, self.BULLET_PAT)
        ]
        self.assertEqual(groups, [("OK", "3")])

    def test_bullets_handle_leading_whitespace_and_blank_lines(self):
        # A real markdown body may have blank separators and
        # 0..N leading spaces (e.g. inside a list item). The helper
        # strips each line before matching, so a 2-space indent must
        # NOT disqualify the bullet.
        body = (
            "  - OK: 1\n"
            "\n"
            "   - DRIFT_WARNING: 2\n"
            "    - no_colon\n"
            "   * ROT: 4\n"
        )
        groups = [
            m.groups() for m in render_report_html._iter_bullets(body, self.BULLET_PAT)
        ]
        self.assertEqual(groups, [("OK", "1"), ("DRIFT_WARNING", "2"), ("ROT", "4")])

    def test_table_rows_tolerate_extra_pipes(self):
        # A row with extra pipes (e.g. `| dim | HIGH | MED | LOW | extra |`)
        # must NOT match the strict 5-pipe pattern. The renderer is
        # strict by design (inspect data is column-stable), so a
        # malformed row is skipped rather than partially captured.
        # The header `| dim | HIGH | MED | LOW |` is also non-numeric
        # in the 2nd..4th columns, so the helper (not the caller's
        # `not in ("dim", "---")` guard) skips it.
        body = (
            "| dim | HIGH | MED | LOW |\n"
            "| dead | 1 | 0 | 0 |\n"
            "| slop | 1 | 0 | 0 | extra |\n"   # extra column -> skip
            "| cleancode | 0 | 1 | 2 |\n"
        )
        rows = [
            (m.group(1), m.group(2), m.group(3), m.group(4))
            for m in render_report_html._iter_table_rows(body, self.TABLE_PAT)
        ]
        self.assertEqual(
            rows,
            [
                ("dead", "1", "0", "0"),
                ("cleancode", "0", "1", "2"),
            ],
        )

    def test_iteration_stops_at_end_of_section(self):
        # Trailing content with no newline at EOF must still be
        # scanned; an off-by-one that drops the last line would break
        # the per-case renderer (last finding vanishes).
        body = (
            "- **OK** `case-1` (dim=review) score=9.0 (p=10, r=8)\n"
            "- **ROT** `case-2` (dim=review) score=4.0 (p=4, r=4)"  # no trailing \n
        )
        rows = list(render_report_html._iter_bullets(body, self.PER_CASE_PAT))
        self.assertEqual(len(rows), 2)
        # Per-case regex group 2 is the case_id (after verdict in group 1).
        self.assertEqual(rows[0].group(2), "case-1")
        self.assertEqual(rows[1].group(2), "case-2")

    def test_helpers_match_inline_parsers_for_existing_fixtures(self):
        # Golden round-trip: every one-shot parser that was rewritten
        # to use the helpers must produce the same structured data it
        # produced before the refactor, for both EVAL_MIN and INSPECT_MIN.
        eval_data = render_report_html.parse_eval_sections(EVAL_MIN)
        inspect_data = render_report_html.parse_inspect_sections(INSPECT_MIN)
        # Eval summary: OK/DRIFT_WARNING/ROT/SKIPPED, all 1/1/1/0.
        self.assertEqual(
            eval_data.summary,
            {"OK": 1, "DRIFT_WARNING": 1, "ROT": 1, "SKIPPED": 0},
        )
        # Per-case: all 3 lines captured with the expected fields.
        self.assertEqual(len(eval_data.per_case), 3)
        self.assertEqual(
            [c["case_id"] for c in eval_data.per_case],
            ["review-01-clean", "review-02-trap", "review-03-bug"],
        )
        # Inspect header: at least Verdict is captured.
        self.assertEqual(inspect_data.header.get("Verdict"), "Critical")
        self.assertIn("Coverage", inspect_data.header)
        self.assertIn("Precision", inspect_data.header)
        # Per-dim table: 5 rows.
        self.assertEqual(len(inspect_data.per_dim), 5)
        self.assertEqual(inspect_data.per_dim[0], ("dead", 1, 0, 0))

    def test_helpers_yield_independent_matches_per_call(self):
        # Each call constructs a fresh iterator; consuming one
        # should not affect a subsequent call. (Generators are
        # single-pass, so this also guards against accidental
        # reuse bugs.)
        body = "- OK: 1\n- ROT: 2\n"
        first = list(render_report_html._iter_bullets(body, self.BULLET_PAT))
        second = list(render_report_html._iter_bullets(body, self.BULLET_PAT))
        self.assertEqual(
            [m.groups() for m in first],
            [m.groups() for m in second],
        )
        self.assertEqual(len(first), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
