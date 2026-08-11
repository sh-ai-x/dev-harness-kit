#!/usr/bin/env python3
"""test_analysis_core_runner.py — End-to-end on a tiny synthetic repo.

Builds a 3-file synthetic repo, runs the engine with a synthetic
candidate stream, and asserts:

  - the engine resolves the right dimensions for each mode
  - candidate JSON parses into Evidence
  - the FP filter pipeline drops/retains as expected
  - the renderer produces stable markdown with the per-dim summary
  - the diff emitter emits `rm` for delete mode and a `# rewrite:`
    header for rewrite mode
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.analysis_core import (  # noqa: E402
    Severity,
    emit_suggested_diffs,
    group,
    render_markdown,
    run_analysis,
)


def _build_synth_repo() -> Path:
    """Create a tiny 3-file repo. Returns the tmp dir."""
    tmp = tempfile.mkdtemp(prefix="ac-synth-")
    root = Path(tmp)
    (root / "a.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text(
        "def add(a, b):  # dup\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (root / "c.py").write_text(
        "import os\n"
        "x = os.environ.get('X')\n",
        encoding="utf-8",
    )
    return root


class TestRunAnalysisKeywordOnly(unittest.TestCase):
    """`candidates` and `verdicts` are keyword-only optional args on
    `run_analysis`. Positional use of these parameters is rejected so a
    future caller cannot accidentally swap order under refactor.
    """

    def test_candidates_positional_raises(self):
        with self.assertRaises(TypeError):
            run_analysis(
                ["correctness"], "read-only", [],
                {"correctness": []},  # candidates positional → reject
            )

    def test_verdicts_positional_raises(self):
        with self.assertRaises(TypeError):
            run_analysis(
                ["correctness"], "read-only", [],
                None, [],  # verdicts positional → reject
            )


class TestRunAnalysis(unittest.TestCase):
    def test_run_analysis_review_keeps_real_finds(self):
        """Review-mode run on a synthetic repo with realistic candidate stream."""
        repo = _build_synth_repo()
        candidates = {
            "correctness": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "missing input validation",
                    "tldr": "add() does not check types",
                    "failure_scenario": "add(1, 'x') raises TypeError",
                    "fix_hint": "guard with isinstance",
                },
            ],
            "security": [
                {
                    "file": str(repo / "c.py"),
                    "line": 2,
                    "severity": "critical",
                    "confidence": "high",
                    "title": "insecure env read",
                    "tldr": "no secret filter",
                    "failure_scenario": "leaks X in logs",
                },
            ],
            "architecture": [],
        }
        result = run_analysis(
            dimensions=group("review"),
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(result.kept_count, 2)
        self.assertEqual(result.filtered_count, 0)
        self.assertEqual(len(result.findings), 2)
        severities = [f.severity for f in result.findings]
        self.assertEqual(
            sorted(severities, key=lambda s: s.value),
            sorted([Severity.CRITICAL, Severity.MAJOR], key=lambda s: s.value),
        )

    def test_run_analysis_filters_missing_failure_scenario(self):
        repo = _build_synth_repo()
        candidates = {
            "correctness": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "speculative",
                    "tldr": "maybe",
                    "failure_scenario": "",  # empty → drop
                },
            ],
        }
        result = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(result.kept_count, 0)
        self.assertGreaterEqual(result.filtered_count, 1)

    def test_run_analysis_delete_drops_nits(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "b.py"),
                    "line": 1,
                    "severity": "nit",
                    "confidence": "medium",
                    "title": "unused",
                    "tldr": "trivia",
                    "failure_scenario": "no callers",
                    "fix_hint": "rm b.py",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(result.kept_count, 0)

    def test_run_analysis_unknown_dimension_raises(self):
        with self.assertRaises(KeyError):
            run_analysis(
                dimensions=["not-a-dim"],
                mode="read-only",
                paths=[],
                candidates={},
            )


class TestRenderMarkdown(unittest.TestCase):
    def test_markdown_contains_per_dim_summary(self):
        repo = _build_synth_repo()
        candidates = {
            "correctness": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "missing guard",
                    "tldr": "no type check",
                    "failure_scenario": "bad input crashes",
                },
            ],
        }
        result = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        md = render_markdown(result)
        self.assertIn("# Analysis Report", md)
        self.assertIn("correctness", md)
        self.assertIn("missing guard", md)
        self.assertIn("Verdict:", md)

    def test_empty_findings_renders_clean(self):
        repo = _build_synth_repo()
        result = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates={"correctness": []},
        )
        md = render_markdown(result)
        self.assertIn("Verdict:", md)
        self.assertIn("Healthy", md)


class TestEmitSuggestedDiffs(unittest.TestCase):
    def test_delete_mode_emits_rm(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "unused file",
                    "tldr": "no importers",
                    "failure_scenario": "no callers",
                    "deletion_scope": "whole-file",
                    "deletion_root_cause": "orphan module",
                    "deletion_proof": {"no_importers": True, "no_callers": True, "no_references": True, "no_runtime_calls": True},
                    "fix_hint": "rm a.py",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        self.assertIn("rm ", diffs[0].command)

    def test_rewrite_mode_emits_header(self):
        repo = _build_synth_repo()
        candidates = {
            "smell": [
                {
                    "file": str(repo / "c.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "long method",
                    "tldr": "too big",
                    "failure_scenario": "unmaintainable",
                    "fix_hint": "split into helpers",
                },
            ],
        }
        result = run_analysis(
            dimensions=["smell"],
            mode="rewrite",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        self.assertIn("# rewrite:", diffs[0].command)

    def test_read_only_emits_no_command(self):
        repo = _build_synth_repo()
        candidates = {
            "correctness": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "x",
                    "tldr": "y",
                    "failure_scenario": "z",
                },
            ],
        }
        result = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(emit_suggested_diffs(result), [])


class TestDeterministicEndToEnd(unittest.TestCase):
    """Two runs with identical inputs MUST produce identical outputs."""

    def test_repeatable_output(self):
        repo = _build_synth_repo()
        candidates = {
            "correctness": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "x",
                    "tldr": "y",
                    "failure_scenario": "z",
                },
            ],
        }
        r1 = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        r2 = run_analysis(
            dimensions=["correctness"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(render_markdown(r1), render_markdown(r2))


class TestScopeEnforcement(unittest.TestCase):
    """`paths` is a real scope filter — out-of-scope findings must not
    reach the dedupe pipeline or the mutation emitter.
    """

    def test_out_of_scope_finding_dropped(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": "/some/other/path/elsewhere.py",
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "out of scope",
                    "tldr": "x",
                    "failure_scenario": "y",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(result.kept_count, 0)

    def test_in_scope_finding_kept(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "in scope",
                    "tldr": "x",
                    "failure_scenario": "y",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        self.assertEqual(result.kept_count, 1)

    def test_candidate_dimension_cannot_spoof_outer_dimension(self):
        repo = _build_synth_repo()
        candidates = {"smell": [{
            "file": str(repo / "a.py"), "line": 1, "dim": "dead",
            "severity": "major", "confidence": "high", "title": "long method",
            "tldr": "too big", "failure_scenario": "unmaintainable",
        }]}
        result = run_analysis(["smell"], "delete", [repo], candidates=candidates)
        self.assertEqual(result.findings[0].dim, "smell")
        self.assertEqual(emit_suggested_diffs(result), [])

    def test_dimension_severity_floor_is_enforced(self):
        repo = _build_synth_repo()
        result = run_analysis(["owasp-a05"], "read-only", [repo], candidates={"owasp-a05": [{
            "file": str(repo / "a.py"), "line": 1, "severity": "nit",
            "confidence": "high", "title": "noise", "tldr": "noise",
            "failure_scenario": "not actionable",
        }]})
        self.assertEqual(result.findings, ())


class TestPerDimModeRespect(unittest.TestCase):
    """`emit_suggested_diffs` MUST skip dims whose `Dimension.mode`
    doesn't match the requested mutation mode. Otherwise a refactor
    smell would surface as `git rm` and destroy a valid source file.
    """

    def test_rewrite_only_dim_skipped_in_delete_mode(self):
        repo = _build_synth_repo()
        # 'smell' has mode='rewrite'. In delete mode it must NOT emit git rm.
        candidates = {
            "smell": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "long method",
                    "tldr": "too big",
                    "failure_scenario": "unmaintainable",
                    "fix_hint": "extract helper",
                },
            ],
        }
        result = run_analysis(
            dimensions=["smell"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(diffs, [])

    def test_delete_dim_emits_git_rm_in_delete_mode(self):
        repo = _build_synth_repo()
        # 'dead' has mode='delete'. In delete mode it MUST emit git rm.
        candidates = {
            "dead": [
                {
                    "file": str(repo / "b.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "unused",
                    "tldr": "no importers",
                    "failure_scenario": "no callers",
                    "deletion_scope": "whole-file",
                    "deletion_root_cause": "orphan module",
                    "deletion_proof": {"no_importers": True, "no_callers": True, "no_references": True, "no_runtime_calls": True},
                    "fix_hint": "rm b.py",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        self.assertIn("git rm", diffs[0].command)


class TestMarkdownFormatCompatibility(unittest.TestCase):
    """`render_markdown` MUST emit bullets that
    `lib/render_report_html._parse_inspect_findings` can consume.
    Otherwise the HTML report silently drops findings.
    """

    def test_bullets_match_report_parser(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "critical",
                    "confidence": "high",
                    "title": "unused module",
                    "tldr": "no importers",
                    "failure_scenario": "no callers",
                    "fix_hint": "rm a.py",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        md = render_markdown(result)
        from lib.render_report_html import _parse_inspect_findings
        # Section header shape is HIGH/MED/LOW (N) — matches the
        # dispatch keys in lib/render_report_html.py:387-393.
        body_start = md.find("## HIGH (")
        self.assertNotEqual(body_start, -1, "missing '## HIGH (N)' section header")
        body = md[body_start:]
        parsed = _parse_inspect_findings(body)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["severity"], "CRITICAL")
        self.assertEqual(parsed[0]["confidence"], "HIGH")
        self.assertEqual(parsed[0]["title"], "unused module")
        self.assertEqual(parsed[0]["Dim"], "dead")

    def test_field_keys_match_report_parser(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "x",
                    "tldr": "tl",
                    "failure_scenario": "sc",
                    "fix_hint": "fh",
                },
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        # Each known field key must appear in the report body.
        for key in ("Dim", "TL;DR", "Scenario", "Fix"):
            self.assertIn(f"{key}:", md, f"missing field key: {key}")

    def test_findings_bucketed_into_high_med_low(self):
        # Sections are emitted so the HTML consumer's dispatch at
        # lib/render_report_html.py:387-393 can route every block.
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {"file": str(repo / "a.py"), "line": 1,
                 "severity": "critical", "confidence": "high",
                 "title": "h", "tldr": "t", "failure_scenario": "s"},
                {"file": str(repo / "b.py"), "line": 1,
                 "severity": "minor", "confidence": "high",
                 "title": "m", "tldr": "t", "failure_scenario": "s"},
                {"file": str(repo / "c.py"), "line": 1,
                 "severity": "nit", "confidence": "high",
                 "title": "l", "tldr": "t", "failure_scenario": "s"},
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        self.assertIn("## HIGH (1)", md)
        self.assertIn("## MED (1)", md)
        self.assertIn("## LOW (1)", md)

    def test_end_to_end_html_render_includes_findings(self):
        # Integration test: drive render_report_html.render directly so
        # the section-header → dispatch → HTML pipeline is exercised.
        # This locks in the fix for finding #5 (silently dropped
        # findings in /dev-kit:report output).
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "critical",
                    "confidence": "high",
                    "title": "unused module",
                    "tldr": "no importers",
                    "failure_scenario": "no callers",
                },
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        from lib.render_report_html import render as render_html
        html = render_html("", md)
        self.assertIn("unused module", html)
        self.assertIn("CRITICAL", html)
        self.assertIn("finding", html.lower())


class TestSecretMasking(unittest.TestCase):
    """Secret-dim free-text MUST be masked at the engine boundary."""

    def test_aws_key_masked_in_markdown(self):
        repo = _build_synth_repo()
        candidates = {
            "secret": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "critical",
                    "confidence": "high",
                    "title": "AWS key leaked",
                    "tldr": "t",
                    "failure_scenario": "found AKIA" + "IOSFODNN7EXAMPLE in env",
                    "fix_hint": "rotate AKIA" + "IOSFODNN7EXAMPLE",
                },
            ],
        }
        result = run_analysis(
            dimensions=["secret"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        )
        md = render_markdown(result)
        self.assertNotIn("AKIA" + "IOSFODNN7EXAMPLE", md)
        self.assertIn("[REDACTED]", md)

    def test_gcp_key_masked_in_markdown(self):
        repo = _build_synth_repo()
        candidates = {
            "secret": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "GCP key",
                    "tldr": "t",
                    "failure_scenario": "AIzaSyA-aBcDeFgHiJkLmNoPqRsTuVwXyZ01234 leaked",
                },
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["secret"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        self.assertNotIn("AIzaSyA-aBcDeFgHiJkLmNoPqRsTuVwXyZ01234", md)
        self.assertIn("[REDACTED]", md)

    def test_secret_in_suggested_diff_path_masked(self):
        repo = _build_synth_repo()
        # secret-dim finding whose file path embeds a key
        candidates = {
            "secret": [
                {
                    "file": "/tmp/" + "AKIA" + "IOSFODNN7EXAMPLE-leak.log",
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "leak file",
                    "tldr": "t",
                    "failure_scenario": "x",
                },
            ],
        }
        # No scope match → out of scope, so this should be dropped.
        # Use a real path under repo to exercise diff path masking.
        secret_file = repo / ("AKIA" + "IOSFODNN7EXAMPLE.log")
        secret_file.write_text("x")
        candidates["secret"][0]["file"] = str(secret_file)
        result = run_analysis(
            dimensions=["secret"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        # secret dim has mode=read-only → no diff emitted (correct)
        self.assertEqual(diffs, [])

    def test_anthropic_sk_ant_key_masked(self):
        from lib.analysis_core.runner import _mask_secrets
        self.assertNotIn(
            "sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456",
            _mask_secrets("found sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456 in env"),
        )

    def test_github_oauth_token_masked(self):
        from lib.analysis_core.runner import _mask_secrets
        self.assertNotIn(
            "gho_abcdefghijklmnopqrstuvwxyz0123456789AB",
            _mask_secrets("token=gho_abcdefghijklmnopqrstuvwxyz0123456789AB"),
        )

    def test_pem_private_key_block_masked(self):
        from lib.analysis_core.runner import _mask_secrets
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxxxx...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        masked = _mask_secrets(pem)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", masked)
        self.assertIn("[REDACTED]", masked)

    def test_postgres_credential_uri_masked(self):
        from lib.analysis_core.runner import _mask_secrets
        uri = "postgres://user:pass@host:5432/db"
        masked = _mask_secrets(uri)
        self.assertNotIn(uri, masked)
        self.assertIn("[REDACTED]", masked)

    def test_mongodb_credential_uri_masked(self):
        from lib.analysis_core.runner import _mask_secrets
        uri = "mongodb+srv://user:pass@cluster.example.net/db"
        masked = _mask_secrets(uri)
        self.assertNotIn(uri, masked)
        self.assertIn("[REDACTED]", masked)

    def test_secret_path_masked_in_markdown_bullet(self):
        # f.file goes through _mask_secrets in render_markdown so a
        # secret-shaped path cannot leak through the bullet line.
        repo = _build_synth_repo()
        secret_path = repo / ("sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456.log")
        secret_path.write_text("x")
        candidates = {
            "secret": [
                {
                    "file": str(secret_path),
                    "line": 1,
                    "severity": "critical",
                    "confidence": "high",
                    "title": "x",
                    "tldr": "t",
                    "failure_scenario": "y",
                },
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["secret"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        self.assertNotIn("sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456", md)
        self.assertIn("[REDACTED]", md)

    def test_delete_mode_diff_file_is_masked(self):
        # SuggestedDiff.file in delete mode must also be masked.
        # Use a delete-mode-supported dim so we actually get a diff.
        repo = _build_synth_repo()
        secret_path = repo / "gho_abcdefghijklmnopqrstuvwxyz0123456789AB.log"
        secret_path.write_text("x")
        candidates = {
            "dead": [
                {
                    "file": str(secret_path),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "unused",
                    "tldr": "t",
                    "failure_scenario": "y",
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        self.assertNotIn(
            "gho_abcdefghijklmnopqrstuvwxyz0123456789AB", diffs[0].file
        )
        self.assertNotIn(
            "gho_abcdefghijklmnopqrstuvwxyz0123456789AB", diffs[0].command
        )



class TestDeleteModePromotesLineEvidence(unittest.TestCase):
    """mode == "delete" means delete the WHOLE FILE, never a single line.
    A line-level evidence (e.g. line=42) promoted to file-level so the
    suggested diff anchors on the file, not on a line that might
    misread as "delete line N of X".
    """

    def test_delete_mode_promotes_line_evidence_to_file_level(self):
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 42,  # explicitly line-level
                    "severity": "major",
                    "confidence": "high",
                    "title": "dead import",
                    "tldr": "no callers",
                    "failure_scenario": "line 42 never reached",
                    "fix_hint": "rm a.py",
                    "deletion_scope": "whole-file",
                    "deletion_root_cause": "orphan module",
                    "deletion_proof": {"no_importers": True, "no_callers": True, "no_references": True, "no_runtime_calls": True},
                },
            ],
        }
        result = run_analysis(
            dimensions=["dead"],
            mode="delete",
            paths=[repo],
            candidates=candidates,
        )
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        # With deletion_proof in place, delete-mode emits `git rm <file>`,
        # NOT a `delete-line N` patch — the human reviewer can still tell
        # the suggestion is file-level because line=None is the anchor.
        self.assertIn("git rm", diffs[0].command)
        self.assertIsNone(diffs[0].line)



class TestDeleteModeRequiresWholeFileProof(unittest.TestCase):
    def test_line_finding_never_emits_git_rm_without_proof(self):
        repo = _build_synth_repo()
        result = run_analysis(["dead"], "delete", [repo], candidates={"dead": [{
            "file": str(repo / "a.py"), "line": 4, "severity": "major",
            "confidence": "high", "title": "unused export", "tldr": "line only",
            "failure_scenario": "one export is unused", "fix_hint": "remove export",
        }]})
        diffs = emit_suggested_diffs(result)
        self.assertEqual(len(diffs), 1)
        self.assertNotIn("git rm", diffs[0].command)
        self.assertEqual(diffs[0].line, 4)

    def test_whole_file_requires_no_importer_and_no_caller_proof(self):
        repo = _build_synth_repo()
        result = run_analysis(["dead"], "delete", [repo], candidates={"dead": [{
            "file": str(repo / "a.py"), "line": 0, "severity": "major",
            "confidence": "high", "title": "orphan module", "tldr": "unused",
            "failure_scenario": "module is unreachable", "deletion_scope": "whole-file",
            "deletion_root_cause": "orphan module",
            "deletion_proof": {"no_importers": True, "no_callers": False},
        }]})
        self.assertNotIn("git rm", emit_suggested_diffs(result)[0].command)


class TestResultContracts(unittest.TestCase):
    def test_verifier_matches_stable_candidate_id_and_preserves_metadata(self):
        repo = _build_synth_repo()
        candidates = {"correctness": [
            {"id": "major-id", "file": str(repo / "a.py"), "line": 1,
             "severity": "major", "confidence": "high", "title": "major",
             "tldr": "major", "failure_scenario": "major"},
            {"id": "critical-id", "file": str(repo / "b.py"), "line": 1,
             "severity": "critical", "confidence": "high", "title": "critical",
             "tldr": "critical", "failure_scenario": "critical"},
        ]}
        result = run_analysis(["correctness"], "read-only", [repo], candidates=candidates,
            verdicts=[("critical-id", "REJECTED", "not reproducible"),
                      ("major-id", "CONFIRMED", "reproduced")])
        self.assertEqual([f.evidence_id for f in result.findings], ["major-id"])
        self.assertEqual(result.findings[0].verdict.value, "CONFIRMED")
        self.assertEqual(result.findings[0].verdict_reason, "reproduced")

    def test_scope_header_masks_secret_shaped_path(self):
        repo = _build_synth_repo()
        secret_root = repo / ("sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456")
        secret_root.mkdir()
        source = secret_root / "a.py"
        source.write_text("x = 1\n", encoding="utf-8")
        result = run_analysis(["correctness"], "read-only", [secret_root], candidates={"correctness": [{
            "file": str(source), "line": 1, "severity": "major", "confidence": "high",
            "title": "x", "tldr": "x", "failure_scenario": "y"
        }]})
        md = render_markdown(result)
        self.assertNotIn("sk-ant-" + "abcdefghijklmnopqrstuvwxyz123456", md)
        self.assertIn("[REDACTED]", md)

    def test_verdict_uses_legacy_high_and_medium_thresholds(self):
        repo = _build_synth_repo()
        def candidate(line, severity):
            return {"file": str(repo / "a.py"), "line": line, "severity": severity,
                    "confidence": "high", "title": severity, "tldr": severity,
                    "failure_scenario": severity}
        self.assertEqual(run_analysis(["correctness"], "read-only", [repo],
            candidates={"correctness": [candidate(1, "major")]}).verdict, "Critical")
        self.assertEqual(run_analysis(["correctness"], "read-only", [repo],
            candidates={"correctness": [candidate(1, "minor"), candidate(2, "minor")] }).verdict,
            "Minor drift")
        self.assertEqual(run_analysis(["correctness"], "read-only", [repo],
            candidates={"correctness": [candidate(1, "minor"), candidate(2, "minor"), candidate(3, "minor")] }).verdict,
            "Major drift")


class TestBucketAssignmentIsSingleSource(unittest.TestCase):
    """The HIGH/MED/LOW bucket assigned to a single Evidence must be
    computed by ONE function and used by BOTH render paths (the per-dim
    summary table AND the section header dispatcher). Previously the
    table and the sections disagreed for any MAJOR finding (table=Med
    vs section=High).
    """

    def test_bucket_assignment_is_single_ssource(self):
        from lib.analysis_core.evidence import Severity
        from lib.analysis_core.runner import _bucket_for
        # Same evidence: both severities bucket identically through
        # the same helper.
        for sev, expected in [
            (Severity.CRITICAL, "HIGH"),
            (Severity.MAJOR, "HIGH"),
            (Severity.MINOR, "MED"),
            (Severity.NIT, "LOW"),
        ]:
            self.assertEqual(
                _bucket_for(sev), expected,
                f"{sev.value} must bucket to {expected}",
            )

    def test_render_table_and_sections_agree_on_major(self):
        # A MAJOR finding is the contradiction case: under the old code
        # the table counted it as MED while the section header counted
        # it as HIGH. After the SSOT, both agree.
        repo = _build_synth_repo()
        candidates = {
            "dead": [
                {
                    "file": str(repo / "a.py"),
                    "line": 1,
                    "severity": "major",
                    "confidence": "high",
                    "title": "dead m",
                    "tldr": "t",
                    "failure_scenario": "s",
                },
            ],
        }
        md = render_markdown(run_analysis(
            dimensions=["dead"],
            mode="read-only",
            paths=[repo],
            candidates=candidates,
        ))
        # Per-dim table row for `dead` should show HIGH=1, MED=0, LOW=0
        # (because MAJOR buckets to HIGH under the SSOT).
        self.assertIn("| dead | 1 | 0 | 0 |", md,
                      "table row for a single MAJOR finding must show HIGH=1 (SSOT)")
        # Section header: a single MAJOR finding goes to the HIGH section.
        self.assertIn("## HIGH (1)", md,
                      "section header for a single MAJOR must show HIGH (1) (SSOT)")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSecretSSOTComment(unittest.TestCase):
    """Doc-contract: `lib/analysis_core/runner.py`'s secret-pattern SSOT
    comment must NOT reference the deleted `skills/audit/SKILL.md`
    (PR-589, review M3). The pattern set is the engine-side SSOT
    required by the `secret` dimension charter; with `skills/audit/`
    removed in favor of `/dev-kit:inspect --secrets/--slop`, the
    comment should now point at the new inspection surface.
    """

    def test_runner_comment_does_not_reference_deleted_audit_skill(self):
        runner = (PROJECT_ROOT / "lib/analysis_core/runner.py").read_text()
        self.assertNotIn("skills/audit/SKILL.md", runner,
                         "runner.py secret-SSOT comment must not cite the "
                         "deleted skills/audit/SKILL.md (PR-589 review M3)")

    def test_runner_comment_documents_new_inspect_surface(self):
        runner = (PROJECT_ROOT / "lib/analysis_core/runner.py").read_text()
        self.assertIn("skills/inspect/SKILL.md", runner,
                      "runner.py secret-SSOT comment should point at "
                      "skills/inspect/SKILL.md after PR-589 merge")
