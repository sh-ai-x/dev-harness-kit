"""ci_doctor.py — Read-only CI readiness audit for `/dev-kit:ci-doctor`.

Engine for the `/dev-kit:ci-doctor` skill. Pure stdlib, no external
deps. Returns a `DoctorReport` dataclass; the skill body renders the
PASS/FAIL summary.

Public surface:
    audit(target_dir, *, provider=None) -> DoctorReport
    DoctorReport          # dataclass with `checks` + `summary_lines()`
    Check                  # one row of the audit table

Heavy lifting moved into sibling modules:
    lib/ci_workflow_yaml       : hand-rolled YAML parser + WorkflowShape
    lib/ci_workflow_diag       : per-workflow WARN/INFO diagnostic rows
    lib/ci_install_shape       : file / marker / provider / secrets checks
    lib/ci_branch_policy       : required-checks vs review.yml job names
    lib/ci_open_pr             : open PR mergeable / draft / bump-title
    lib/ci_ruleset             : ruleset workflow + bypass-actors wrappers
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from lib.ci_branch_policy import fetch_required_status_checks
from lib.ci_install_shape import (
    check_gates_consistency,
    check_gh_auth,
    check_marker_payload,
    check_provider_declared,
    check_required_files,
    check_secrets,
    check_templates_current,
)
from lib.ci_open_pr import fetch_open_pr_state as _fetch_open_pr_state_impl
from lib.ci_ruleset import (
    check_ruleset_bypass_actors as _ci_ruleset_bypass_check,
)
from lib.ci_ruleset import (
    check_ruleset_contract as _ci_ruleset_check,
)
from lib.ci_setup import (
    check_provider_consistency,
    detect_owner_repo,
    gh_secret_set_command,
    read_provider,
    required_secrets_for_provider,
)
from lib.ci_workflow_diag import run_diagnostics as _run_wf_diagnostics
from lib.ci_workflow_yaml import read_workflow
from lib.gh_cli import _gh_available

# ci_update may be absent in this checkout (the plugin is its own dev
# environment). The check returns SKIP when missing; tests do not exercise it.
try:
    from lib.ci_update import diff_ci_install  # type: ignore
except ImportError:
    diff_ci_install = None  # type: ignore[assignment]


@dataclass
class Check:
    """One row of the audit table.

    `state` is one of: PASS, FAIL, SKIP, INFO, WARN. WARN/INFO are
    advisory and never flip the audit verdict.
    """
    label: str
    state: str
    detail: str = ""

    def row(self) -> str:
        return f"[{self.state:<4}] {self.label}: {self.detail}".rstrip()


@dataclass
class DoctorReport:
    """Aggregate audit result. `ok` is True iff no FAIL row was recorded."""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.state != "FAIL" for c in self.checks)

    def failing(self) -> list[Check]:
        return [c for c in self.checks if c.state == "FAIL"]

    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.state == "WARN"]

    def summary_lines(self) -> list[str]:
        """Render a PASS/FAIL summary table for stdout."""
        verdict = "PASS" if self.ok else "FAIL"
        lines = [f"ci-doctor verdict: {verdict}"]
        lines.append(
            f"  checks: {len(self.checks)}  "
            f"failing: {len(self.failing())}  "
            f"skipped: {sum(1 for c in self.checks if c.state == 'SKIP')}  "
            f"warnings: {len(self.warnings())}"
        )
        lines.extend(f"  {c.row()}" for c in self.checks)
        return lines


# Files the install MUST leave behind (subset of ci_setup.EXPECTED_PATHS).
REQUIRED_FILES: tuple[str, ...] = (
    ".github/workflows/ci.yml",
    ".github/workflows/review.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".dev-kit/ci-config.json",
)
CONSUMER_ONLY_FILES: frozenset[str] = frozenset({".dev-kit/ci-config.json"})
CONSUMER_ONLY_SECRETS: frozenset[str] = frozenset({"DEV_KIT_GITHUB_TOKEN"})
EXPECTED_PR_TRIGGERS: dict[str, frozenset[str]] = {
    "review.yml": frozenset({"pull_request", "pull_request_target", "workflow_run"}),
    "auto-fix-pr.yml": frozenset({"pull_request_review"}),
    "ci.yml": frozenset({"pull_request", "push"}),
    "maintenance.yml": frozenset({"pull_request", "pull_request_target", "workflow_run"}),
}
WORKFLOW_FILES: tuple[str, ...] = (
    ".github/workflows/review.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/maintenance.yml",
)


def _read_marker(target: Path) -> dict | None:
    """Read `.dev-kit/ci-config.json` as a dict; None on absent/parse error."""
    p = target / ".dev-kit" / "ci-config.json"
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_source_repo(target_dir: Path) -> bool:
    """True iff `target_dir` is the dev-kit plugin authoring source."""
    manifest = target_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("name") == "dev-kit"


def _detect_owner_repo(target_dir: Path) -> str:
    """Re-export ci_setup.detect_owner_repo; strip the placeholder."""
    result = detect_owner_repo(target_dir)
    return "" if result.startswith("<OWNER>/<REPO>") else result


def required_files_for(marker_payload):
    """Derive required-files list from marker.runners (issue #834)."""
    base = (
        ".github/workflows/ci.yml",
        ".github/workflows/auto-fix-pr.yml",
        ".dev-kit/ci-config.json",
    )
    if not marker_payload or not isinstance(marker_payload, dict):
        return REQUIRED_FILES
    runners = marker_payload.get("runners")
    if runners is None or not isinstance(runners, list):
        return REQUIRED_FILES
    return tuple(sorted(set(base) | {f".github/workflows/{r}" for r in runners if isinstance(r, str)}))


# ---- Thin orchestrators that delegate to sibling modules ----------------

def _check_required_files(target: Path, source_repo: bool) -> list[Check]:
    return check_required_files(target, source_repo, _read_marker(target),
                               REQUIRED_FILES, CONSUMER_ONLY_FILES, Check)


def _check_marker_payload(target: Path, source_repo: bool) -> list[Check]:
    return check_marker_payload(target, source_repo, Check)


def _check_templates_current(target: Path, source_repo: bool) -> list[Check]:
    return check_templates_current(target, source_repo, diff_ci_install, Check)


def _check_gates_consistency(target: Path) -> list[Check]:
    return check_gates_consistency(target, _read_marker(target), Check)


def _check_provider_declared(target: Path) -> list[Check]:
    from lib.ci_setup import PROVIDER_SECRETS, read_env_key
    return check_provider_declared(target, PROVIDER_SECRETS, read_env_key, Check)


def _check_provider_consistency(target: Path) -> Check:
    """Issue #712: surface `.env` vs `vars.CI_REVIEW_PROVIDER` drift.

    Wraps `lib.ci_setup.check_provider_consistency` and translates its
    `(status, message)` tuple into the Check state set. Inlined so test
    patches on `self.cd.check_provider_consistency` take effect.
    """
    try:
        status, message = check_provider_consistency(target)
    except Exception as e:  # pragma: no cover
        return Check("CI_REVIEW_PROVIDER consistency", "SKIP",
                     f"check_provider_consistency errored: {type(e).__name__}")
    state = {"OK": "PASS", "WARN": "WARN", "SKIP": "SKIP", "FAIL": "FAIL"}.get(status, "SKIP")
    return Check("CI_REVIEW_PROVIDER consistency", state, message)


def _check_secrets(target: Path, provider: str | None, source_repo: bool) -> list[Check]:
    return check_secrets(target, provider, source_repo, CONSUMER_ONLY_SECRETS,
                         _detect_owner_repo, _list_repo_secrets,
                         read_provider, required_secrets_for_provider,
                         gh_secret_set_command, Check)


def _list_repo_secrets(repo: str) -> tuple[set[str], str]:
    """Return (set-of-secret-names, degraded-message)."""
    import subprocess
    gh, degraded = _gh_available(timeout=10)
    if not gh:
        return set(), degraded or "gh not on PATH"
    try:
        cp = subprocess.run(
            [gh, "secret", "list", "--repo", repo, "--json", "name"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        return set(), f"gh secret list error: {e}"
    if cp.returncode != 0:
        err = (cp.stderr or "").strip().splitlines()[-1] if cp.stderr else ""
        return set(), f"gh secret list failed: {err or 'unknown'}"
    try:
        names = {row.get("name", "") for row in json.loads(cp.stdout)}
    except json.JSONDecodeError:
        return set(), "gh secret list JSON parse error"
    return names, ""


def _check_gh_auth() -> Check:
    return check_gh_auth(_gh_available, Check)


def _check_workflow_diagnostics(target: Path, source_repo: bool) -> list[Check]:
    return _run_wf_diagnostics(
        target=target, source_repo=source_repo,
        expected_triggers=EXPECTED_PR_TRIGGERS,
        workdir_files=WORKFLOW_FILES, Check=Check,
    )


def _check_branch_protection(target: Path, source_repo: bool) -> Check:
    """WARN on branch-policy vs review.yml job-name mismatch; SKIP/INFO otherwise.

    Inlined (not delegated to lib.ci_branch_policy) so test patches on
    `self.cd._detect_owner_repo` and `self.cd._fetch_required_status_checks`
    take effect — Python resolves the bare names at call time through the
    module globals, which `patch.object` already mutates.
    """
    if source_repo:
        return Check("branch policy", "INFO", "source repo: branch policy not audited")
    repo = _detect_owner_repo(target)
    if not repo:
        return Check("branch policy", "SKIP", "no GitHub remote on origin")
    required, degraded = _fetch_required_status_checks(repo)
    if degraded:
        return Check("branch policy", "SKIP", degraded)
    review = target / ".github" / "workflows" / "review.yml"
    if not review.is_file():
        return Check("branch policy", "INFO", "review.yml not present; nothing to compare")
    raw, shape, err = read_workflow(review.parent, review.name)
    if raw is None:
        return Check("branch policy", "INFO", err)
    assert shape is not None
    if shape.parse_error or not shape.jobs:
        return Check("branch policy", "INFO",
                     f"could not extract review.yml job names: "
                     f"{shape.parse_error or 'no jobs'}")
    job_names = {j.name for j in shape.jobs if j.name}
    if not job_names:
        return Check("branch policy", "INFO",
                     "review.yml jobs lack `name:` — bare-key matching required")
    missing = sorted(required - job_names)
    extra = sorted(job_names - required)
    if not missing and not extra:
        return Check("branch policy", "PASS",
                     f"required={sorted(required)}  workflow={sorted(job_names)}")
    return Check("branch policy", "WARN",
                 f"required-vs-workflow mismatch: "
                 f"required but not emitted by any review job={missing}; "
                 f"emitted by review but not required={extra}")


def _fetch_required_status_checks(repo: str) -> tuple[set[str], str]:
    return fetch_required_status_checks(_gh_available, repo)


def _check_open_pr(target: Path) -> list[Check]:
    """Diagnose the open PR's merge state."""
    data, degraded = _fetch_open_pr_state(target)
    if degraded:
        return [Check("open PR state", "SKIP", degraded)]
    rows: list[Check] = []
    mergeable = data.get("mergeable", "")
    if mergeable == "CONFLICTING":
        rows.append(Check("open PR mergeable", "FAIL",
            "open PR has merge conflicts with main — CI will not run. "
            "Run: git fetch origin main && git merge origin/main"))
    elif mergeable == "UNKNOWN":
        rows.append(Check("open PR mergeable", "WARN",
            "GitHub still computing merge state — re-run /dev-kit:ci-doctor in 30s"))
    elif mergeable == "MERGEABLE":
        rows.append(Check("open PR mergeable", "PASS", "no conflicts"))
    else:
        rows.append(Check("open PR mergeable", "INFO", f"unrecognized mergeable value: {mergeable!r}"))
    if data.get("isDraft"):
        rows.append(Check("open PR draft", "INFO",
            "PR is a draft — required checks gated until marked ready for review"))
    if data.get("title", "").startswith("chore(release): bump dev-kit to v"):
        rows.append(Check("open PR title", "INFO",
            "bump-PR — ci/review/security explicitly skip per templates/ci/.github/workflows/ci.yml"))
    return rows


def _fetch_open_pr_state(target: Path) -> tuple[dict, str]:
    return _fetch_open_pr_state_impl(target, _gh_available, Check)


def _check_ruleset_workflow_contract(target: Path) -> list[Check]:
    return [Check(label=r.label, state=r.state, detail=r.detail)
            for r in _ci_ruleset_check(target)]


def _check_ruleset_bypass_actors(target: Path) -> list[Check]:
    return [Check(label=r.label, state=r.state, detail=r.detail)
            for r in _ci_ruleset_bypass_check(target)]


def audit(target_dir: Path, *, provider: str | None = None) -> DoctorReport:
    """Run the full check suite. Side-effect free."""
    target = Path(target_dir).resolve()
    if not target.is_dir():
        return DoctorReport(checks=[
            Check("target dir", "FAIL", f"not a directory: {target}"),
        ])
    report = DoctorReport()
    report.checks.append(Check("target dir", "PASS", str(target)))
    source_repo = _is_source_repo(target)
    if source_repo:
        report.checks.append(Check("repo role", "INFO",
                                   "dev-kit source repo: consumer-only checks skipped"))
    report.checks.extend(_check_required_files(target, source_repo))
    report.checks.extend(_check_marker_payload(target, source_repo))
    report.checks.extend(_check_gates_consistency(target))
    report.checks.extend(_check_templates_current(target, source_repo))
    report.checks.extend(_check_provider_declared(target))
    report.checks.append(_check_provider_consistency(target))
    report.checks.append(_check_gh_auth())
    report.checks.extend(_check_secrets(target, provider, source_repo))
    report.checks.extend(_check_workflow_diagnostics(target, source_repo))
    report.checks.append(_check_branch_protection(target, source_repo))
    report.checks.extend(_check_open_pr(target))
    report.checks.extend(_check_ruleset_workflow_contract(target))
    report.checks.extend(_check_ruleset_bypass_actors(target))
    return report


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = audit(target)
    for line in r.summary_lines():
        print(line)
    sys.exit(0 if r.ok else 1)
