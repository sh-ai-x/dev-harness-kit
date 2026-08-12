from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills/security-metrics/scripts/score_security.py"
SPEC = importlib.util.spec_from_file_location("security_metrics", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_scorecard_has_all_owasp_categories(tmp_path: Path) -> None:
    categories = MODULE.scan(tmp_path)
    assert len(categories) == 10
    assert [(item.code, item.name) for item in categories] == list(MODULE.NAMES.items())
    assert all(0 <= item.score <= 100 for item in categories)


def test_scorecard_is_deterministic_for_same_content(tmp_path: Path) -> None:
    (tmp_path / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    first = MODULE.scan(tmp_path)
    second = MODULE.scan(tmp_path)
    assert [(item.name, item.score, item.findings) for item in first] == [
        (item.name, item.score, item.findings) for item in second
    ]


def test_render_contains_markdown_score_table(tmp_path: Path) -> None:
    report = MODULE.render(tmp_path, MODULE.scan(tmp_path))
    assert "Overall score:" in report
    assert "| OWASP area | Score | Status | Evidence / deductions |" in report
    assert report.count("/100") >= 11
    assert "/dev-kit:security" in report


def test_render_is_deterministic_and_escapes_table_cells(tmp_path: Path) -> None:
    categories = MODULE.scan(tmp_path)
    categories[0].findings = ["-1: evidence | contains a pipe"]
    assert MODULE.render(tmp_path, categories) == MODULE.render(tmp_path, categories)
    assert "evidence \\| contains a pipe" in MODULE.render(tmp_path, categories)


def test_access_design_and_authentication_rules_are_assessed(tmp_path: Path) -> None:
    categories = {item.code: item for item in MODULE.scan(tmp_path)}
    assert categories["A01"].score < 100
    assert categories["A06"].score < 100
    assert categories["A07"].score < 100
