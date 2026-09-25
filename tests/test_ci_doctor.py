"""test_ci_doctor.py — Tests for `/dev-kit:ci-doctor` audit engine.

Issue #212-D1: the audit must answer "is CI ready?" deterministically,
read-only, with one PASS/FAIL summary. These tests pin every check to
known behavior and exercise both the happy path (after a fresh
`ci-setup` install) and the most common failure modes.

Audit-grade verified: parametrized fixture matrix over every check.
Each scenario = (mutations, expected label substring, expected state,
detail substring). The matrix covers install-shape, workflow
diagnostics, branch protection, open-PR state, ruleset wrapper,
templates-current, and provider-consistency checks.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load(mod_name, file):
    spec = importlib.util.spec_from_file_location(mod_name, PROJECT_ROOT / "lib" / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def cs():
    return _load("ci_setup", "ci_setup.py")


@pytest.fixture(scope="session")
def cd():
    return _load("ci_doctor", "ci_doctor.py")


# Minimal workflow bodies the tests' `_minimal_install` writes for
# diagnostic-only tests. Real install shape is exercised by
# `installed_target`.
STUB_REVIEW = (
    "on:\n  pull_request:\njobs:\n  review:\n"
    "    name: review\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
)
STUB_AUTO_FIX = (
    "on:\n  pull_request_review:\n    types: [submitted]\njobs:\n"
    "  auto-fix:\n    name: auto-fix\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo\n"
)
STUB_CI = (
    "on:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n"
    "  test:\n    name: test\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
)


def _write_workflow(target, name, body):
    p = target / ".github" / "workflows" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        p.write_bytes(body)
    else:
        p.write_text(body, encoding="utf-8")
    return p


def _minimal_install(target):
    """Marker + .env.example + stub workflows so per-file diagnostic tests
    exercise the row emitter without going through `install_ci_config`."""
    (target / ".github").mkdir(parents=True, exist_ok=True)
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (target / ".env.example").write_text(
        "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8"
    )
    (target / ".dev-kit").mkdir(parents=True, exist_ok=True)
    (target / ".dev-kit" / "ci-config.json").write_text(json.dumps({
        "schema_version": 1, "installed_at": "2026-01-01T00:00:00Z",
        "provider_env_key": "CI_REVIEW_PROVIDER"}), encoding="utf-8")
    _write_workflow(target, "review.yml", STUB_REVIEW)
    _write_workflow(target, "auto-fix-pr.yml", STUB_AUTO_FIX)
    _write_workflow(target, "ci.yml", STUB_CI)


def _audit(target, cd):
    """Run audit() with stubs for gh auth + secrets (the side-effect-free
    baseline used by smoke checks)."""
    with patch.object(cd, "_check_gh_auth",
                      return_value=cd.Check("gh auth", "SKIP", "")
         ), patch.object(cd, "_check_secrets", return_value=[]):
        return cd.audit(target)


def _mark_source_repo(target):
    (target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (target / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "dev-kit", "version": "0.0.0"}), encoding="utf-8"
    )


# ---------------------------------------------------------------------
# Fixture-driven targets
# ---------------------------------------------------------------------

@pytest.fixture
def installed_target(tmp_path, cs):
    cs.install_ci_config(tmp_path)
    (tmp_path / ".env.example").write_text(
        "CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def minimal_target(tmp_path):
    _minimal_install(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------
# Smoke tests (one-shot, no matrix)
# ---------------------------------------------------------------------

def test_audit_passes_after_fresh_install(installed_target, cd):
    """Happy path: install leaves a target that audit returns PASS (modulo gh rows)."""
    r = _audit(installed_target, cd)
    shape = [c for c in r.checks
             if not c.label.startswith(("gh auth", "repo context", "secret set:"))]
    failing = [c for c in shape if c.state == "FAIL"]
    assert failing == [], f"install-shape audit failed: {[(c.label, c.state, c.detail) for c in failing]}"


def test_audit_handles_target_dir_that_does_not_exist(cd):
    r = cd.audit(Path("/nonexistent/ci_doctor_test_xyz_987"))
    assert not r.ok
    assert len(r.failing()) == 1
    assert r.failing()[0].label == "target dir"


def test_summary_lines_shows_warn_count(cd):
    r = cd.DoctorReport()
    r.checks.extend([
        cd.Check("a", "WARN", "x"),
        cd.Check("b", "WARN", "y"),
        cd.Check("c", "PASS", "z"),
    ])
    lines = r.summary_lines()
    assert lines[0].startswith("ci-doctor verdict: PASS")
    assert "warnings: 2" in lines[1]
    assert "failing: 0" in lines[1]
    assert r.ok


def test_warn_rows_do_not_flip_ok(cd):
    r = cd.DoctorReport()
    r.checks.append(cd.Check("a", "WARN", "x"))
    assert r.ok and len(r.warnings()) == 1 and r.failing() == []


def test_info_rows_not_in_warnings(cd):
    r = cd.DoctorReport()
    r.checks.extend([cd.Check("a", "INFO", "x"), cd.Check("b", "WARN", "y")])
    assert len(r.warnings()) == 1 and r.failing() == [] and r.ok


def test_no_fail_regression_in_fresh_install(installed_target, cd):
    r = _audit(installed_target, cd)
    shape = [c for c in r.checks
             if not c.label.startswith(("gh auth", "repo context", "secret set:"))]
    assert [c for c in shape if c.state == "FAIL"] == []
    assert r.ok


def test_summary_lines_renders(cd):
    """`summary_lines()` is suitable for stdout."""
    r = cd.DoctorReport()
    r.checks.append(cd.Check("ok", "PASS", "fine"))
    lines = r.summary_lines()
    assert lines[0].startswith("ci-doctor verdict: PASS")
    assert "PASS" in "\n".join(lines)


def test_doctor_report_dataclass_shape(cd):
    r = cd.DoctorReport()
    r.checks.append(cd.Check("foo", "PASS", "ok"))
    assert r.ok and r.failing() == []
    assert cd.Check("foo", "PASS", "ok").row() == "[PASS] foo: ok"


# ---------------------------------------------------------------------
# Install-shape checks: required_files / marker_payload / provider / secrets
# ---------------------------------------------------------------------

def _strip_env_fn(t):
    """No provider anywhere: delete .env.example and any .env."""
    if (t / ".env.example").exists():
        (t / ".env.example").unlink()
    if (t / ".env").exists():
        (t / ".env").unlink()


def _unknown_provider_in_env_fn(t):
    """`.env` carries an unknown provider; .env.example deleted so the
    fallback can't rescue the row. Mirrors issue #212 setup."""
    if (t / ".env.example").exists():
        (t / ".env.example").unlink()
    (t / ".env").write_text("CI_REVIEW_PROVIDER=gpt5\n", encoding="utf-8")


INSTALL_SHAPE_MUTATIONS = {
    "required_workflow_missing":
        lambda t: (t / ".github" / "workflows" / "review.yml").unlink(),
    "missing_marker":
        lambda t: shutil.rmtree(t / ".dev-kit"),
    "corrupt_marker":
        lambda t: (t / ".dev-kit" / "ci-config.json").write_text("not-json{"),
    "missing_provider_env": _strip_env_fn,
    "unknown_provider_in_env": _unknown_provider_in_env_fn,
}


INSTALL_SHAPE_CASES = [
    ("required_workflow_missing", "review.yml", "FAIL"),
    ("missing_marker", "marker parseable", "FAIL"),
    ("corrupt_marker", "marker parseable", "FAIL"),
    ("missing_provider_env", "provider declared", "FAIL"),
    ("unknown_provider_in_env", "provider declared", "FAIL"),
]


@pytest.mark.parametrize("name,label_substr,expected_state", INSTALL_SHAPE_CASES)
def test_install_shape_scenarios(installed_target, cd, name, label_substr, expected_state):
    INSTALL_SHAPE_MUTATIONS[name](installed_target)
    r = cd.audit(installed_target)
    matches = [c for c in r.checks if label_substr in c.label]
    assert matches, f"{name}: no row matched {label_substr!r}"
    assert any(c.state == expected_state for c in matches), \
        f"{name}: expected {expected_state} in {[(c.label, c.state) for c in matches]}"


def test_provider_override_changes_required_secrets(installed_target, cd):
    (installed_target / ".env").write_text("CI_REVIEW_PROVIDER=anthropic\n", encoding="utf-8")
    r = cd.audit(installed_target)
    declared = [c for c in r.checks if "provider declared" in c.label]
    assert declared[0].state == "PASS"
    assert "anthropic" in declared[0].detail


# ---------------------------------------------------------------------
# Source-repo detection
# ---------------------------------------------------------------------

@pytest.mark.parametrize("manifest_payload,expected", [
    ({"name": "dev-kit"}, True),
    (None, False),
    ({"name": "some-other-plugin"}, False),
])
def test_is_source_repo(tmp_path, cd, manifest_payload, expected):
    if manifest_payload is not None:
        (tmp_path / ".claude-plugin").mkdir(parents=True)
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(manifest_payload), encoding="utf-8"
        )
    assert cd._is_source_repo(tmp_path) is expected


def test_source_repo_skips_marker_rows(tmp_path, cs, cd):
    cs.install_ci_config(tmp_path)
    shutil.rmtree(tmp_path / ".dev-kit")
    _mark_source_repo(tmp_path)
    r = _audit(tmp_path, cd)
    marker_rows = [c for c in r.checks if "ci-config.json" in c.label or c.label.startswith("marker")]
    assert marker_rows and all(c.state == "SKIP" for c in marker_rows)


def test_source_repo_skips_dev_kit_github_token(tmp_path, cs, cd):
    cs.install_ci_config(tmp_path)
    _mark_source_repo(tmp_path)
    r = _audit(tmp_path, cd)
    pat_fail = [c for c in r.checks if "DEV_KIT_GITHUB_TOKEN" in c.label and c.state == "FAIL"]
    assert pat_fail == []


# ---------------------------------------------------------------------
# Workflow diagnostics (parametrized matrix)
# ---------------------------------------------------------------------

def _override_workflow(target, rel, body):
    _write_workflow(target, rel, body)


WF_CASES = [
    ("trigger_warns_no_pr", "review.yml",
     "name: review\non:\n  workflow_dispatch:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "workflow triggers: review.yml", "WARN", "pull_request"),
    ("trigger_passes_pull_request", "review.yml",
     "on:\n  pull_request:\n    types: [opened]\njobs:\n  review:\n    name: review\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "workflow triggers: review.yml", "PASS", "pull_request"),
    ("fork_gap_warns_pull_request_only", "review.yml",
     "on:\n  pull_request:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "fork-PR secret gap: review.yml", "WARN", "fork PRs lose repo secrets"),
    ("fork_gap_passes_with_pull_request_target", "review.yml",
     "on:\n  pull_request:\n  pull_request_target:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "fork-PR secret gap: review.yml", "PASS", None),
    ("fork_gap_passes_with_fork_guard", "review.yml",
     "on:\n  pull_request:\njobs:\n  review:\n    name: review\n    if: github.event.pull_request.head.repo.full_name == github.repository\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "fork-PR secret gap: review.yml", "PASS", "same-repo guard"),
    ("paths_filter_info", "review.yml",
     "on:\n  pull_request:\n    paths:\n      - 'lib/**'\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "paths filter: review.yml", "INFO", "lib/**"),
    ("branches_filter_info", "review.yml",
     "on:\n  pull_request:\n    branches:\n      - main\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "branches filter: review.yml", "INFO", "main"),
    ("concurrency_cancel_warns", "review.yml",
     "on:\n  pull_request:\nconcurrency:\n  group: ${{ github.event.pull_request.number }}\n  cancel-in-progress: true\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "concurrency: review.yml", "WARN", "cancel-in-progress=true"),
    ("concurrency_cancel_false_passes", "review.yml",
     "on:\n  pull_request:\nconcurrency:\n  group: ${{ github.event.pull_request.number }}\n  cancel-in-progress: false\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "concurrency: review.yml", "PASS", None),
    ("job_if_info", "review.yml",
     "on:\n  pull_request:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    if: \"github.event.pull_request.title != 'bot'\"\n    steps:\n      - run: echo\n",
     "job if: review.yml/review", "INFO", "bot"),
    ("job_name_missing_review_info", "review.yml",
     "on:\n  pull_request:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "job name: review.yml/review", "INFO", None),
    ("job_name_missing_auto_fix_warns", "auto-fix-pr.yml",
     "on:\n  pull_request_review:\n    types: [submitted]\njobs:\n  auto-fix:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "job name: auto-fix-pr.yml/auto-fix", "WARN", None),
    ("unparseable_yaml_info", "review.yml",
     b"\x00\x01\x02\xffnot yaml",
     "workflow triggers: review.yml", "INFO", "parse"),
    ("quoted_on_key_passes", "review.yml",
     "\"on\": pull_request\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
     "workflow triggers: review.yml", "PASS", "pull_request"),
    ("action_pin_info", "review.yml",
     "on:\n  pull_request:\njobs:\n  review:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: anthropics/claude-code-action@v1\n",
     "action ref mutable: review.yml", "INFO", "anthropics/claude-code-action@v1"),
]


@pytest.mark.parametrize("name,rel,body,label_substr,expected_state,detail_assert",
                         WF_CASES, ids=[c[0] for c in WF_CASES])
def test_workflow_diagnostics(minimal_target, cd, name, rel, body, label_substr,
                              expected_state, detail_assert):
    _override_workflow(minimal_target, rel, body)
    r = cd.audit(minimal_target)
    matches = [c for c in r.checks if label_substr in c.label]
    assert matches, f"{name}: no row matched {label_substr!r}"
    primary = matches[0]
    assert primary.state == expected_state, f"{name}: {primary.row()}"
    if detail_assert is not None:
        if name == "unparseable_yaml_info":
            assert detail_assert in primary.detail or "read error" in primary.detail
        else:
            assert detail_assert in primary.detail, f"{name}: {primary.row()}"
    if name.startswith("trigger_warns") or name.startswith("fork_gap_warns"):
        assert r.ok, f"{name}: verdict must remain PASS — failures={r.failing()}"
    if name == "unparseable_yaml_info":
        assert any(c.label == "file present: .github/workflows/review.yml"
                   and c.state == "PASS" for c in r.checks)
        review_diags = [
            c for c in r.checks
            if "review.yml" in c.label
            and any(c.label.startswith(p) for p in (
                "workflow triggers:", "fork-PR secret gap:",
                "concurrency:", "paths filter:", "branches filter:",
                "job if:", "job name:", "action ref mutable:",
            ))
        ]
        assert review_diags
        for d in review_diags:
            assert d.state in {"INFO", "WARN"}


# ---------------------------------------------------------------------
# Branch protection
# ---------------------------------------------------------------------

BP_CASES = [
    # (name, side_effects, expected_state, detail_assert)
    ("skip_when_no_repo", {"_detect_owner_repo": ""}, "SKIP", "no GitHub remote"),
    ("skip_when_gh_missing", {"_detect_owner_repo": "example/repo",
                              "_fetch_required_status_checks": (set(), "gh not on PATH")},
     "SKIP", "gh not on PATH"),
    ("skip_when_gh_unauth", {"_detect_owner_repo": "example/repo",
                            "_fetch_required_status_checks": (set(), "gh not authenticated")},
     "SKIP", "gh not authenticated"),
    ("warn_on_mismatch", {"_detect_owner_repo": "example/repo",
                          "_fetch_required_status_checks": ({"lint"}, "")},
     "WARN", "lint"),
    ("pass_on_full_match", {"_detect_owner_repo": "example/repo",
                            "_fetch_required_status_checks": ({"lint", "test (python 3.12)"}, "")},
     "PASS", None),
]


@pytest.mark.parametrize("name,extras,expected_state,detail_assert", BP_CASES,
                         ids=[c[0] for c in BP_CASES])
def test_branch_protection(installed_target, cd, name, extras, expected_state, detail_assert):
    if name in {"warn_on_mismatch", "pass_on_full_match"}:
        # Replace review.yml with a workflow whose job `name:`s match the
        # mocked required-check set (or don't, for the mismatch case).
        body = (
            "on:\n  pull_request:\njobs:\n"
            "  review:\n    name: review\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo\n"
            "  security:\n    name: security\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo\n"
        )
        if name == "pass_on_full_match":
            body = body.replace("name: review\n", "name: lint\n"
                              ).replace("name: security\n", "name: test (python 3.12)\n")
        _override_workflow(installed_target, "review.yml", body)
    extras_patches = [
        patch.object(cd, k, return_value=v)
        for k, v in extras.items()
    ]
    std_patches = [
        patch.object(cd, "_check_gh_auth",
                     return_value=cd.Check("gh auth", "PASS", "")),
        patch.object(cd, "_check_secrets", return_value=[]),
        patch.object(cd, "_check_required_files", return_value=[]),
        patch.object(cd, "_check_marker_payload", return_value=[]),
        patch.object(cd, "_check_provider_declared", return_value=[]),
    ]
    with std_patches[0], std_patches[1], std_patches[2], std_patches[3], std_patches[4]:
        for ctx in extras_patches:
            ctx.start()
        try:
            r = cd.audit(installed_target)
        finally:
            for ctx in extras_patches:
                try:
                    ctx.stop()
                except Exception:
                    pass
    matches = [c for c in r.checks if "branch policy" in c.label]
    assert matches
    assert matches[0].state == expected_state
    if detail_assert:
        assert detail_assert in matches[0].detail
    if name == "pass_on_full_match":
        assert r.ok
    if name == "warn_on_mismatch":
        assert r.ok


def test_branch_protection_info_in_source_repo(installed_target, cd):
    _mark_source_repo(installed_target)
    r = cd.audit(installed_target)
    matches = [c for c in r.checks if "branch policy" in c.label]
    assert matches and matches[0].state == "INFO" and "source repo" in matches[0].detail


# ---------------------------------------------------------------------
# Open PR state (issue #249)
# ---------------------------------------------------------------------

def _gh_pr_json(*, mergeable, is_draft=False, title=""):
    return {"mergeable": mergeable,
            "mergeStateStatus": "DIRTY" if mergeable == "CONFLICTING" else "CLEAN",
            "isDraft": is_draft, "title": title}


OPEN_PR_CASES = [
    # (name, fetch_state_return, label_substr, expected_state, detail_assert)
    ("conflicting_flips_verdict_to_fail",
     (_gh_pr_json(mergeable="CONFLICTING", title="fix: x"), ""),
     "open PR mergeable", "FAIL", "conflict"),
    ("mergeable_emits_pass",
     (_gh_pr_json(mergeable="MERGEABLE", title="feat: ok"), ""),
     "open PR mergeable", "PASS", None),
    ("unknown_warns",
     (_gh_pr_json(mergeable="UNKNOWN", title="fix: ?"), ""),
     "open PR mergeable", "WARN", None),
    ("draft_emits_info",
     (_gh_pr_json(mergeable="MERGEABLE", is_draft=True, title="WIP"), ""),
     "open PR draft", "INFO", "draft"),
    ("bump_title_emits_info",
     (_gh_pr_json(mergeable="MERGEABLE", title="chore(release): bump dev-kit to v0.3.92"), ""),
     "open PR title", "INFO", "bump"),
    ("no_open_pr_skips",
     ({}, "no open PR for current branch"),
     "open PR state", "SKIP", "no open PR for current branch"),
    ("gh_unavailable_skips",
     ({}, "gh not on PATH"),
     "open PR state", "SKIP", "gh not on PATH"),
]


@pytest.mark.parametrize("name,fetch_state_return,label_substr,expected_state,detail_assert",
                         OPEN_PR_CASES, ids=[c[0] for c in OPEN_PR_CASES])
def test_open_pr(tmp_path, cd, name, fetch_state_return, label_substr, expected_state, detail_assert):
    with patch.object(cd, "_check_gh_auth",
                      return_value=cd.Check("gh auth", "SKIP", "")), \
         patch.object(cd, "_check_required_files", return_value=[]), \
         patch.object(cd, "_check_marker_payload", return_value=[]), \
         patch.object(cd, "_check_provider_declared", return_value=[]), \
         patch.object(cd, "_check_secrets", return_value=[]), \
         patch.object(cd, "_fetch_open_pr_state", return_value=fetch_state_return):
        r = cd.audit(tmp_path)
    matches = [c for c in r.checks if "open PR" in c.label and label_substr in c.label]
    if matches:
        assert matches[0].state == expected_state
        if detail_assert:
            assert detail_assert.lower() in matches[0].detail.lower()
    else:
        rows = [c for c in r.checks if c.label.startswith("open PR ")]
        assert any(c.state == expected_state for c in rows), \
            f"{name}: no row with state {expected_state} in {[(c.label, c.state) for c in rows]}"
    if name != "conflicting_flips_verdict_to_fail":
        assert r.ok


def test_open_pr_diagnostic_rows_helper(cd):
    r = cd.DoctorReport()
    r.checks.append(cd.Check("open PR mergeable", "PASS", "ok"))
    r.checks.append(cd.Check("other", "FAIL", "x"))
    rows = [c for c in r.checks if "open PR " in c.label]
    assert len(rows) == 1
    assert rows[0].label == "open PR mergeable"


# ---------------------------------------------------------------------
# Templates-current check
# ---------------------------------------------------------------------

def test_templates_current_passes_on_clean_install(tmp_path, cs, cd):
    cs.install_ci_config(tmp_path)
    rows = [c for c in _audit(tmp_path, cd).checks if "templates current" in c.label]
    assert len(rows) == 1 and rows[0].state == "PASS"


def test_templates_current_warns_on_consumer_drift(tmp_path, cs, cd):
    cs.install_ci_config(tmp_path)
    rel = "scripts/validate.py"
    (tmp_path / rel).write_bytes((tmp_path / rel).read_bytes() + b"\n# edit\n")
    rows = [c for c in _audit(tmp_path, cd).checks if "templates current" in c.label]
    assert rows[0].state == "WARN" and "consumer_modified" in rows[0].detail


def test_templates_current_skips_when_marker_lacks_version(tmp_path, cs, cd):
    cs.install_ci_config(tmp_path)
    marker_path = tmp_path / ".dev-kit" / "ci-config.json"
    payload = json.loads(marker_path.read_text())
    payload.pop("installed_dev_kit_version", None)
    payload.pop("template_shas", None)
    marker_path.write_text(json.dumps(payload))
    rows = [c for c in _audit(tmp_path, cd).checks if "templates current" in c.label]
    assert rows[0].state == "SKIP"


# ---------------------------------------------------------------------
# Provider consistency (issue #712)
# ---------------------------------------------------------------------

PC_CASES = [
    ("emits_one_row", "OK", "both unset", "PASS", "unset"),
    ("ok_maps_to_pass", "OK", "both unset", "PASS", "unset"),
    ("warn_preserved", "WARN",
     "local .env=CI_REVIEW_PROVIDER=anthropic but vars.CI_REVIEW_PROVIDER=minimax; "
     "sync with `gh variable set CI_REVIEW_PROVIDER --body anthropic`",
     "WARN", "anthropic"),
    ("skip_preserved", "SKIP", "gh not on PATH", "SKIP", "gh"),
]


@pytest.mark.parametrize("name,status,message,expected_state,detail_assert",
                         PC_CASES, ids=[c[0] for c in PC_CASES])
def test_provider_consistency(tmp_path, cd, name, status, message, expected_state, detail_assert):
    with patch.object(cd, "_check_required_files", return_value=[]), \
         patch.object(cd, "_check_marker_payload", return_value=[]), \
         patch.object(cd, "_check_provider_declared", return_value=[]), \
         patch.object(cd, "_check_gh_auth", return_value=cd.Check("gh auth", "SKIP", "")), \
         patch.object(cd, "_check_secrets", return_value=[]), \
         patch.object(cd, "_check_workflow_diagnostics", return_value=[]), \
         patch.object(cd, "_check_open_pr", return_value=[]), \
         patch.object(cd, "check_provider_consistency", return_value=(status, message)):
        r = cd.audit(tmp_path)
    rows = [c for c in r.checks if "CI_REVIEW_PROVIDER consistency" in c.label]
    assert len(rows) == 1 and rows[0].state == expected_state
    if detail_assert:
        assert detail_assert in rows[0].detail
    if expected_state in {"WARN", "SKIP"}:
        assert r.ok


# ---------------------------------------------------------------------
# Ruleset wrapper (issue #774)
# ---------------------------------------------------------------------

def test_no_local_ruleset_files_emits_info(cd, tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    rows = cd._check_ruleset_workflow_contract(tmp_path)
    assert len(rows) == 1 and rows[0].state == "INFO" and rows[0].label == "ruleset workflow contract"


def test_mismatch_yields_fail(cd, tmp_path):
    (tmp_path / ".github" / "rulesets").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "rulesets" / "protect-main.json").write_text(
        '{"rules":[{"type":"required_status_checks",'
        '"parameters":{"required_status_checks":['
        '{"context":"severity gate (review + security + injection_scan)",'
        '"integration_id":null}]}}]}',
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "review.yml").write_text(
        "jobs:\n  gate:\n    name: severity gate (review + security)\n"
        "    runs-on: ubuntu-latest\n    steps: [{run: echo}]\n",
        encoding="utf-8",
    )
    rows = cd._check_ruleset_workflow_contract(tmp_path)
    assert any(r.state == "FAIL" for r in rows)
    joined = " ".join(r.detail for r in rows)
    assert "severity gate (review + security + injection_scan)" in joined
    assert "protect-main.json" in joined


def test_match_yields_pass(cd, tmp_path):
    import yaml
    (tmp_path / ".github" / "rulesets").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "rulesets" / "protect-main.json").write_text(
        '{"rules":[{"type":"required_status_checks",'
        '"parameters":{"required_status_checks":['
        '{"context":"ci","integration_id":null}]}}]}',
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(yaml.safe_dump({
        "jobs": {"ci": {"name": "ci", "runs-on": "ubuntu-latest",
                  "steps": [{"run": "echo"}]}}
    }), encoding="utf-8")
    rows = cd._check_ruleset_workflow_contract(tmp_path)
    assert any(r.state == "PASS" for r in rows)


def test_wrapper_wired_into_audit_call(cd, tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".env.example").write_text("CI_REVIEW_PROVIDER=minimax\n", encoding="utf-8")
    r = cd.audit(tmp_path)
    labels = [c.label for c in r.checks]
    assert "ruleset workflow contract" in labels
