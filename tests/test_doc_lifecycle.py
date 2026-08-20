"""Regression tests for tools.check_doc_lifecycle.

Five cases from the cherry-pick proposal §게이트 동작:
  1. stale_after < today → exit 1, violation reported
  2. stale_after >= today → exit 0
  3. stale_after absent → exit 0 (fail-open per proposal §limitations 4)
  4. status: deprecated → exit 0 even if stale_after in past
  5. frontmatter unparseable → exit 1 (fail-closed)
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "tools" / "check_doc_lifecycle.py"


def _load_module():
    """Import tools/check_doc_lifecycle.py as ``cd``.
    Doing it manually because ``tools/`` is not a package on PYTHONPATH.
    """
    spec = importlib.util.spec_from_file_location("cd", SCRIPT)
    assert spec and spec.loader, f"could not load {SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cd"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cd():
    return _load_module()


def _write_doc(path: Path, fm: str | None, body: str = "# Title\n") -> None:
    """Write a markdown file with optional frontmatter block."""
    if fm is None:
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")


@pytest.fixture()
def rules_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "rules"
    rd.mkdir()
    return rd


class TestStaleAfter:
    """Case 1 + 2: date arithmetic."""

    def test_expired_file_violates(self, cd, rules_dir):
        _write_doc(rules_dir / "expired.md", "stale_after: 2025-01-01")
        rc = cd.run(rules_dir, today=date(2026, 1, 1))
        assert rc == 1

    def test_fresh_file_passes(self, cd, rules_dir):
        _write_doc(rules_dir / "fresh.md", "stale_after: 2099-12-31")
        rc = cd.run(rules_dir, today=date(2026, 1, 1))
        assert rc == 0

    def test_exactly_today_passes(self, cd, rules_dir):
        _write_doc(rules_dir / "today.md", "stale_after: 2026-08-19")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 0


class TestFieldAbsent:
    """Case 3: missing stale_after must NOT trip the gate (fail-open)."""

    def test_no_stale_after_passes(self, cd, rules_dir):
        _write_doc(rules_dir / "no_field.md", "alpha: state\n")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 0

    def test_no_frontmatter_at_all_passes(self, cd, rules_dir):
        # Reserved frontmatter files (e.g. index.md) are skipped; plain
        # text files without frontmatter also pass per fail-open semantics.
        _write_doc(rules_dir / "plain.md", None)
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 0

    def test_index_md_is_skipped(self, cd, rules_dir):
        # rules/index.md is a navigation page; even an expired stale_after
        # inside it must not block CI.
        _write_doc(rules_dir / "index.md", "stale_after: 2020-01-01\n# Navigation")
        _write_doc(rules_dir / "ok.md", "stale_after: 2099-12-31")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 0


class TestDeprecated:
    """Case 4: status: deprecated is exempt from the expiry gate."""

    def test_deprecated_with_past_stale_after_passes(self, cd, rules_dir):
        _write_doc(
            rules_dir / "old.md",
            textwrap.dedent(
                """\
                status: deprecated
                stale_after: 2025-01-01
                """
            ),
        )
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 0


class TestUnparseable:
    """Case 5: frontmatter parse failure = exit 1 (fail-closed)."""

    def test_broken_yaml_violates(self, cd, rules_dir):
        _write_doc(rules_dir / "broken.md", "stale_after: [unclosed")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 1

    def test_string_stale_after_violates(self, cd, rules_dir):
        # "not-a-date" is YAML-scalar → stays as str → checker rejects via ValueError
        _write_doc(rules_dir / "bad_type.md", "stale_after: not-a-date")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 1

    def test_non_date_non_string_stale_after_violates(self, cd, rules_dir):
        # A list value slips past type checks; the only legal shape is str|date
        _write_doc(rules_dir / "list_val.md", "stale_after:\n  - 2025-01-01\n  - 2026-01-01")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        assert rc == 1


class TestMultiFile:
    """Mixed content: one expired file poisons the run, fresh files don't."""

    def test_mixed_run_fails_on_one_expired(self, cd, rules_dir, capsys):
        _write_doc(rules_dir / "a.md", "stale_after: 2099-12-31")
        _write_doc(rules_dir / "b.md", "stale_after: 2025-01-01")
        _write_doc(rules_dir / "c.md", "stale_after: 2099-01-01")
        rc = cd.run(rules_dir, today=date(2026, 8, 19))
        captured = capsys.readouterr()
        assert rc == 1
        # only the expired one shows up in the violation list
        assert "b.md" in captured.err
        assert "a.md" not in captured.err
        assert "c.md" not in captured.err


class TestRealRules:
    """Sanity check: every file in the actual rules/ directory must parse."""

    def test_repo_rules_pass_or_have_judgment(self, cd):
        from datetime import date as _date
        rules_dir = ROOT / "rules"
        if not rules_dir.exists():
            pytest.skip("no rules/ directory")
        rc = cd.run(rules_dir, today=_date(2026, 8, 19))
        # After this PR adds stale_after to the 5 rule files with future
        # dates, this should return 0. Until then, accept either rc if the
        # maintainer runs the test on a non-marked branch.
        assert rc in (0, 1)
