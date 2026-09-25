"""ci_install_shape.py — install-shape check helpers for ci-doctor.

Pulled out of `lib/ci_doctor.py`. Verifies the consumer repo has the
files + marker + provider declaration the install was supposed to
leave behind. Caller passes the `Check` factory to keep this module
decoupled from `lib/ci_doctor.py`'s dataclass identity.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

CheckFactory = Callable[..., "object"]


def check_required_files(target: Path, source_repo: bool, marker_payload,
                         required_files: tuple[str, ...],
                         consumer_only_files: frozenset[str],
                         Check: CheckFactory) -> list["object"]:
    out: list["object"] = []
    base = (".github/workflows/ci.yml", ".github/workflows/auto-fix-pr.yml",
            ".dev-kit/ci-config.json")
    if marker_payload and isinstance(marker_payload, dict):
        runners = marker_payload.get("runners")
        if isinstance(runners, list):
            required = tuple(sorted(set(base)
                                   | {f".github/workflows/{r}" for r in runners
                                      if isinstance(r, str)}))
        else:
            required = required_files
    else:
        required = required_files
    for rel in required:
        p = target / rel
        if p.is_file():
            out.append(Check(label=f"file present: {rel}", state="PASS",
                             detail=f"{p.stat().st_size} bytes"))
        elif source_repo and rel in consumer_only_files:
            out.append(Check(label=f"file present: {rel}", state="SKIP",
                             detail="source repo: consumer marker not applicable"))
        else:
            out.append(Check(label=f"file present: {rel}", state="FAIL",
                             detail="missing"))
    return out


def check_marker_payload(target: Path, source_repo: bool,
                         Check: CheckFactory) -> list["object"]:
    marker_path = target / ".dev-kit" / "ci-config.json"
    if not marker_path.is_file():
        if source_repo:
            return [Check(label="marker parseable", state="SKIP",
                          detail="source repo: consumer marker not applicable")]
        return [Check(label="marker parseable", state="FAIL",
                      detail=".dev-kit/ci-config.json missing")]
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [Check(label="marker parseable", state="FAIL",
                      detail=f"parse error: {e}")]
    out = [Check(label="marker parseable", state="PASS", detail="JSON ok")]
    if not isinstance(payload, dict) or not payload:
        out.append(Check(label="marker non-empty", state="FAIL", detail="empty payload"))
    else:
        out.append(Check(label="marker non-empty", state="PASS",
                         detail=f"{len(payload)} keys"))
    if payload.get("provider_env_key") != "CI_REVIEW_PROVIDER":
        out.append(Check(label="marker records provider key", state="FAIL",
                         detail="expected `provider_env_key: CI_REVIEW_PROVIDER`"))
    else:
        out.append(Check(label="marker records provider key", state="PASS", detail=""))
    return out


def check_templates_current(target: Path, source_repo: bool,
                            diff_ci_install, Check: CheckFactory) -> list["object"]:
    if source_repo:
        return [Check(label="templates current", state="SKIP",
                      detail="source repo: self-comparison not meaningful")]
    if diff_ci_install is None:
        return [Check(label="templates current", state="SKIP",
                      detail="ci_update module not importable from this environment")]
    marker_path = target / ".dev-kit" / "ci-config.json"
    if not marker_path.is_file():
        return [Check(label="templates current", state="SKIP",
                      detail="no .dev-kit/ci-config.json marker")]
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [Check(label="templates current", state="FAIL",
                      detail=f"marker parse error: {e}")]
    installed_version = marker.get("installed_dev_kit_version", "")
    if not installed_version or installed_version == "unknown":
        return [Check(label="templates current", state="SKIP",
                      detail="marker lacks installed_dev_kit_version; "
                             "run /dev-kit:ci-setup to backfill")]
    try:
        report = diff_ci_install(target)
    except Exception as e:
        return [Check(label="templates current", state="FAIL",
                      detail=f"diff engine error: {e}")]
    n_new = len(report.new)
    n_updated = len(report.updated)
    n_consumer_modified = len(report.consumer_modified)
    n_diverged = len(report.diverged)
    detail = (f"installed={installed_version}; new={n_new} updated={n_updated} "
              f"consumer_modified={n_consumer_modified} diverged={n_diverged}")
    if n_consumer_modified == 0 and n_diverged == 0 and n_updated == 0 and n_new == 0:
        return [Check(label="templates current", state="PASS", detail=detail)]
    if n_consumer_modified == 0 and n_diverged == 0:
        return [Check(label="templates current", state="INFO", detail=detail)]
    return [Check(label="templates current", state="WARN", detail=detail)]


def check_gates_consistency(target: Path, marker_payload,
                            Check: CheckFactory) -> list["object"]:
    """Audit `.dev-kit/gates.json` vs GH repo variables (issue #834)."""
    gates_path = target / ".dev-kit" / "gates.json"
    report: list["object"] = []
    if not gates_path.is_file():
        if marker_payload is not None:
            report.append(Check(label="gates.json present", state="WARN",
                detail="no .dev-kit/gates.json - run /dev-kit:gate-select init "
                       "to synthesize one from marker.runners"))
        return report
    try:
        gates_payload = json.loads(gates_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return report
    gh = shutil.which("gh")
    for gate_key, entry in (gates_payload.get("gates") or {}).items():
        if not isinstance(entry, dict):
            continue
        var = entry.get("var")
        if not isinstance(var, str):
            continue
        enabled = entry.get("enabled") is True
        row = _gate_var_check(gh, gate_key, var, enabled, Check)
        if row is not None:
            report.append(row)
    return report


def _gate_var_check(gh: str | None, gate_key: str, var: str, enabled: bool,
                    Check: CheckFactory) -> "object | None":
    if not gh:
        return Check(label=f"gate var {gate_key}={var}", state="SKIP",
                     detail="gh not on PATH; cannot read GH repo variable")
    try:
        cp = subprocess.run(
            [gh, "variable", "get", var],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return Check(label=f"gate var {gate_key}={var}", state="SKIP",
                     detail="gh variable get errored (gh not authenticated?)")
    if cp.returncode != 0:
        return Check(label=f"gate var {gate_key}={var}", state="SKIP",
                     detail=f"gh variable get {var} returned exit={cp.returncode}")
    remote = (cp.stdout or "").strip().lower()
    local = "true" if enabled else "false"
    if remote == local:
        return Check(label=f"gate var {gate_key}={var}", state="PASS",
                     detail=f"{gate_key}: local={local}, remote={remote}")
    return Check(label=f"gate var {gate_key}={var}", state="WARN",
                 detail=f"{gate_key}: drift (gates.json={local}, "
                        f"gh variable={remote!r}); run /dev-kit:gate-select sync")


def check_provider_declared(target: Path, provider_secrets,
                           read_env_key, Check: CheckFactory) -> list["object"]:
    env_val = _safe_getenv("CI_REVIEW_PROVIDER")
    env_file_val = read_env_key(target / ".env", "CI_REVIEW_PROVIDER").lower() if (target / ".env").is_file() else ""
    example_val = read_env_key(target / ".env.example", "CI_REVIEW_PROVIDER").lower() if (target / ".env.example").is_file() else ""
    resolved = next((v for v in (env_val, env_file_val, example_val) if v in provider_secrets), "")
    if not resolved:
        return [Check(label="provider declared", state="FAIL",
                      detail="CI_REVIEW_PROVIDER not set in process env, .env, or .env.example")]
    source = "process env" if env_val == resolved else ".env" if env_file_val == resolved else ".env.example"
    return [Check(label="provider declared", state="PASS",
                  detail=f"{resolved} (via {source})")]


def _safe_getenv(name: str) -> str:
    import os
    return os.environ.get(name, "").strip().lower()


def check_secrets(target: Path, provider: str | None, source_repo: bool,
                 consumer_only_secrets: frozenset[str],
                 detect_owner_repo_fn, list_repo_secrets_fn,
                 read_provider_fn, required_secrets_fn, gh_secret_set_fn,
                 Check: CheckFactory) -> list["object"]:
    repo = detect_owner_repo_fn(target)
    if not repo:
        return [Check(label="repo context", state="SKIP", detail="no GitHub remote on origin")]
    secrets, degraded = list_repo_secrets_fn(repo)
    if degraded:
        return [Check(label="repo secrets", state="SKIP", detail=degraded)]
    provider = provider or read_provider_fn(target)
    needed = required_secrets_fn(provider)
    out: list["object"] = []
    for name in needed:
        if source_repo and name in consumer_only_secrets:
            out.append(Check(label=f"secret set: {name}", state="SKIP",
                             detail="source repo: PAT not required"))
        elif name in secrets:
            out.append(Check(label=f"secret set: {name}", state="PASS", detail=""))
        else:
            out.append(Check(label=f"secret set: {name}", state="FAIL",
                             detail=f"run: {gh_secret_set_fn(repo, name)}"))
    return out


def check_gh_auth(gh_available_fn, Check: CheckFactory) -> "object":
    gh, degraded = gh_available_fn(timeout=5)
    if not gh:
        return Check(label="gh CLI", state="SKIP", detail=degraded or "gh not on PATH")
    return Check(label="gh auth", state="PASS", detail="" if not degraded else degraded)
