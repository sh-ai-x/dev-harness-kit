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


# --- Exclusions regression tests (issue #641 follow-up) ---


def _score(tmp_path: Path) -> dict[str, MODULE.Category]:
    return {item.code: item for item in MODULE.scan(tmp_path)}


def test_files_excludes_stale_worktree_dirs(tmp_path: Path) -> None:
    """Files under `.worktrees/` and `.claude/worktrees/` must not be scored.

    Files under `.claude/` outside the worktree scratch directory
    (e.g. `.claude/settings.json`, `.claude/hooks/foo.sh`) are checked-in
    config and MUST still be scored.
    """
    # Real source — clean (no findings expected)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/clean.py").write_text("x = 1\n", encoding="utf-8")
    # Stale worktree pollution — would inflate findings if scanned.
    # Only the `.worktrees/...` and `.claude/worktrees/...` paths are
    # excluded; `.claude/agents/...` is checked-in config and IS scored.
    for stale_dir in (".worktrees/old", ".claude/worktrees/old"):
        d = tmp_path / stale_dir
        d.mkdir(parents=True)
        (d / "bad.py").write_text(
            "import subprocess\nsubprocess.run('id', shell=True)\n"
            "import hashlib\nhashlib.md5(b'x')\n",
            encoding="utf-8",
        )
    cats = _score(tmp_path)
    # If stale dirs leaked through, A05 / A04 would be deducted.
    assert cats["A05"].score == 100
    assert cats["A04"].score == 100


def test_files_does_not_exclude_checked_in_claude_config(tmp_path: Path) -> None:
    """Checked-in `.claude/` config (settings.json, hooks/) IS scored."""
    (tmp_path / ".claude/hooks").mkdir(parents=True)
    (tmp_path / ".claude/settings.json").write_text(
        '{"permissions": {"allow": ["Bash"]}}\n', encoding="utf-8"
    )
    (tmp_path / ".claude/hooks/foo.sh").write_text(
        "#!/usr/bin/env bash\necho ok\n", encoding="utf-8"
    )
    cats = _score(tmp_path)
    # No production paths changed → docs_ok / scorer should be clean.
    assert cats["A05"].score == 100


def test_assessed_files_excludes_intentional_fixtures(tmp_path: Path) -> None:
    """Bug fixtures under skills/review/fixtures/ must not be scored."""
    fx = tmp_path / "skills/review/fixtures/real-bugs"
    fx.mkdir(parents=True)
    (fx / "sql_injection.py").write_text(
        "def find(name):\n    return f\"SELECT * FROM users WHERE name = '{name}'\"\n",
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A05"].score == 100


def test_sql_regex_ignores_jq_select_queries(tmp_path: Path) -> None:
    """`jq` `select(... | contains(...)) | {body: .body}` is not SQL injection."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "ci.sh").write_text(
        'gh pr view "$PR_NUMBER" --json comments \\\n'
        '  --jq \'.comments[] | select(.author.login | startswith("claude")) | {body, createdAt}\'\n',
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert not any("SQL-like" in finding for finding in cats["A05"].findings), (
        f"jq select() should not trip SQL regex, got: {cats['A05'].findings}"
    )


def test_sql_regex_still_flags_real_sql_injection(tmp_path: Path) -> None:
    """Sanity: SELECT … FROM … {var} still trips A05."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "vuln.py").write_text(
        'def find(name):\n'
        '    return f"SELECT * FROM users WHERE name = \'{name}\'"\n',
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A05"].score < 100
    assert any("SQL-like" in finding for finding in cats["A05"].findings)


def test_sql_regex_catches_identifier_interpolation(tmp_path: Path) -> None:
    """SELECT {cols} FROM users — identifier interpolation BEFORE FROM is also SQL injection."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "vuln.py").write_text(
        'def bad():\n'
        '    cols = "id, name"\n'
        '    return f"SELECT {cols} FROM users WHERE active = 1"\n',
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A05"].score < 100
    assert any("SQL-like" in finding for finding in cats["A05"].findings), (
        f"identifier interpolation should trip SQL regex, got: {cats['A05'].findings}"
    )


def test_assessed_files_excludes_self_references(tmp_path: Path) -> None:
    """The scorer itself must not score its own regex literals."""
    sm = tmp_path / "skills/security-metrics/scripts"
    sm.mkdir(parents=True)
    (sm / "score_security.py").write_text(
        "if re.search(r'\\bshell\\s*=\\s*True\\b', text): pass\n"
        "if re.search(r'\\bcurl\\b[^\\n|]*\\|\\s*(ba)?sh\\b', text): pass\n",
        encoding="utf-8",
    )
    (sm / "report.md").write_text(
        "# Security Metrics\n\n- shell=True detected\n- curl|sh detected\n",
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A05"].score == 100
    assert cats["A08"].score == 100


def test_credential_regex_ignores_shell_var_references(tmp_path: Path) -> None:
    """`TOKEN="${ENV_VAR}"` is an env-var reference, not a hardcoded secret."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "ci.sh").write_text(
        'TOKEN="${APPROVE_TOKEN:-$GH_TOKEN}"\n'
        'gh pr review "$PR_NUMBER" --approve\n',
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert not any("hardcoded credential" in finding for finding in cats["A02"].findings), (
        f"shell-var reference should not trip A02, got: {cats['A02'].findings}"
    )


def test_credential_regex_still_flags_literal_secret(tmp_path: Path) -> None:
    """Sanity: a real-looking hardcoded secret still trips A02."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "leak.py").write_text(
        # Clearly synthetic placeholder — must not look like a real
        # production key, or GitHub secret-scanning will block the push.
        'API_KEY = "EXAMPLE_TOKEN_1234567890ABCDEFGHIJKL"\n',
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A02"].score < 100
    assert any("hardcoded credential" in finding for finding in cats["A02"].findings)


def test_scorer_output_artifact_excluded_by_filename(tmp_path: Path) -> None:
    """Re-running the scorer must not self-trigger on its own report."""
    (tmp_path / "security-metrics.md").write_text(
        "# Security Metrics\n\n| A05 | shell=True detected |\n",
        encoding="utf-8",
    )
    cats = _score(tmp_path)
    assert cats["A05"].score == 100
