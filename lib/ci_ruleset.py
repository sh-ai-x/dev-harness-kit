"""ci_ruleset.py - Ruleset workflow job-name cross-check.

Issue #774: GitHub branch-protection required-status checks match
against workflow job `name:` strings by EXACT name (not prefix, not
substring, not regex). When a workflow job is renamed (e.g. PR #763
added the `injection_scan` pre-gate and renamed `severity gate (review
+ security)` -> `severity gate (review + security + injection_scan)`),
the corresponding ruleset context MUST be renamed in the same PR -
otherwise:

  - The new (longer) job's PR Checks UI shows pass.
  - The ruleset's required context is not satisfied (different string),
    so the PR is `mergeStateStatus: BLOCKED` while
    `mergeable: MERGEABLE`. Invisible until someone forces the ruleset
    view.

This module factors the parsing + cross-check used by:

  - `tests/test_ci_ruleset_contract.py` (the Iron Law L1 regression
    test - must guard future job renames)
  - `lib/ci_doctor.py` (the `/dev-kit:ci-doctor` cross-check - surfaces
    the same divergence to operators pre-PR)

Stdlib json + PyYAML (pinned in requirements.lock). No hand-rolled
YAML parsing - using a real parser avoids a hand-rolled scanner
silently agreeing with a similarly-buggy workflow.

Public surface:
    RulesetContext       # namedtuple of (file, context_name)
    load_ruleset_contexts(target_dir) -> list[RulesetContext]
    load_workflow_job_names(target_dir) -> tuple[set[str], set[str], list[str]]
    check_ruleset_contract(target_dir, *, source_repo=False) -> list[_CheckRow]
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple


class RulesetContext(NamedTuple):
    """One required-status-check context extracted from a ruleset file.

    `file` is the ruleset JSON path relative to `target_dir` (so error
    messages can point the operator at the exact file). `context_name`
    is the literal string GitHub matches against workflow job `name:`.
    """
    file: str
    context_name: str


def _read_json(path: Path) -> Any | None:
    """Read JSON from `path`, returning None on parse error.

    The caller decides how to surface parse errors (the regression
    test fails loud; the ci-doctor check surfaces as WARN so a corrupt
    ruleset file never blocks PRs).
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_ruleset_payload(payload: Any) -> list[dict]:
    """Yield rule-dict entries from a ruleset JSON payload.

    GitHub ruleset files come in two shapes:

      - A single top-level ruleset object (most common): keys `name`,
        `target`, `enforcement`, `rules` (an array).
      - A top-level array of ruleset objects (export of multiple).

    Both forms appear in the wild. We normalize by collecting every
    `rules[]` array we encounter, in document order.
    """
    if isinstance(payload, dict):
        rules = payload.get("rules")
        if isinstance(rules, list):
            return [r for r in rules if isinstance(r, dict)]
        return []
    if isinstance(payload, list):
        out: list[dict] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            rules = entry.get("rules")
            if isinstance(rules, list):
                out.extend(r for r in rules if isinstance(r, dict))
        return out
    return []


def _extract_contexts_from_rule(rule: dict) -> list[str]:
    """Pull required-status-check context strings out of one rule dict.

    GitHub has shipped two shapes for required checks:

      Legacy - top-level `required_status_checks.contexts[]` (a flat
      list of strings) on the rule OR on `parameters` (older export
      forms).

      Current - under `parameters.required_status_checks[]` array of
      `{context, integration_id}` objects.
    """
    contexts: list[str] = []
    if isinstance(rule.get("contexts"), list):
        contexts.extend(
            s for s in rule["contexts"] if isinstance(s, str) and s
        )
    params = rule.get("parameters")
    if isinstance(params, dict):
        rs = params.get("required_status_checks")
        if isinstance(rs, list):
            for item in rs:
                if isinstance(item, str) and item:
                    contexts.append(item)
                elif isinstance(item, dict):
                    ctx = item.get("context")
                    if isinstance(ctx, str) and ctx:
                        contexts.append(ctx)
        if isinstance(rs, dict):
            rs_ctx = rs.get("contexts")
            if isinstance(rs_ctx, list):
                contexts.extend(
                    s for s in rs_ctx if isinstance(s, str) and s
                )
    seen: set[str] = set()
    out: list[str] = []
    for c in contexts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def load_ruleset_contexts(target_dir: Path) -> list[RulesetContext]:
    """Load all required-status-check contexts declared by any ruleset
    under `<target>/.github/rulesets/*.json`.

    Returns a list of `(rel_path, context)` pairs. Empty list if no
    ruleset files exist (the consumer may not author them locally -
    GitHub rulesets can be configured out-of-band).

    Silently ignores unparseable JSON files; the ci-doctor caller
    surfaces a WARN row for those.
    """
    out: list[RulesetContext] = []
    ruleset_dir = Path(target_dir) / ".github" / "rulesets"
    if not ruleset_dir.is_dir():
        return out
    for json_path in sorted(ruleset_dir.glob("*.json")):
        payload = _read_json(json_path)
        if payload is None:
            continue
        for rule in _normalize_ruleset_payload(payload):
            if rule.get("type") != "required_status_checks":
                continue
            rel = json_path.relative_to(target_dir).as_posix()
            for ctx in _extract_contexts_from_rule(rule):
                out.append(RulesetContext(rel, ctx))
    return out


def load_workflow_job_names(
    target_dir: Path,
) -> tuple[set[str], set[str], list[str]]:
    """Return `(named-job-names, bare-job-keys, parse-error-messages)`.

    A job surfaces in the GitHub UI under its `name:` if set, otherwise
    as its bare key. Both shapes can appear as a ruleset context. The
    caller treats the union as the matching surface; the regression
    test asserts no job has an empty `name:`.

    `parse_error_messages` lists per-file failures so the ci-doctor
    row can name them.
    """
    import yaml  # PyYAML - pinned in requirements.lock

    named: set[str] = set()
    bare_keys: set[str] = set()
    errors: list[str] = []
    wf_dir = Path(target_dir) / ".github" / "workflows"
    if not wf_dir.is_dir():
        return named, bare_keys, errors
    for yml_path in sorted(wf_dir.glob("*.yml")):
        rel = yml_path.relative_to(target_dir).as_posix()
        try:
            data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:  # noqa: F821
            errors.append(f"{rel}: {e}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}: top-level YAML is not a mapping")
            continue
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for key, spec in jobs.items():
            if not isinstance(spec, dict):
                continue
            name = spec.get("name")
            if isinstance(name, str) and name.strip():
                named.add(name)
            else:
                bare_keys.add(key)
    return named, bare_keys, errors


@dataclass
class _CheckRow:
    """Lightweight Check row used by `check_ruleset_contract`.

    Reusing `lib.ci_doctor.Check` would couple this module to the
    ci-doctor import shape (which loads with `lib.dual_import`
    because the consumer install doesn't ship an `__init__.py`).
    The cross-check function in ci_doctor wraps each row back into
    its own `Check` so the SKIP/WARN semantics stay consistent
    across the two surfaces.
    """
    label: str
    state: str  # PASS | FAIL | WARN | SKIP | INFO
    detail: str


def check_ruleset_contract(
    target_dir: Path,
    *,
    source_repo: bool = False,
) -> list[_CheckRow]:
    """Cross-check every required-status-check context in any local
    ruleset file against the workflow job names under
    `.github/workflows/`.

    Rows:
      - One INFO row when `.github/rulesets/` doesn't exist (nothing
        to validate against; consumer may author rulesets out-of-band).
      - One PASS row when ruleset files exist but declare no required
        checks.
      - One PASS row when every required context matches a job name OR
        a bare-key fallback.
      - One WARN row per unparseable ruleset file.
      - One FAIL row listing every ruleset context with no matching
        job name (plus a remediation hint).

    The `source_repo` flag is honored for symmetry with ci-doctor;
    today it has no effect.
    """
    _ = source_repo
    ruleset_dir = Path(target_dir) / ".github" / "rulesets"
    if not ruleset_dir.is_dir():
        return [_CheckRow(
            "ruleset workflow contract", "INFO",
            "no .github/rulesets/*.json - drift check requires a local "
            "ruleset file or a `gh api /repos/<owner>/<repo>/rulesets/<id>` "
            "call (see docs/quality/ci-ruleset-contract.md)",
        )]
    contexts = load_ruleset_contexts(target_dir)
    if not contexts:
        return [_CheckRow(
            "ruleset workflow contract", "PASS",
            "no required_status_checks rules in local ruleset files",
        )]
    named, bare_keys, errors = load_workflow_job_names(target_dir)
    rows: list[_CheckRow] = []
    for err in errors:
        rows.append(_CheckRow(
            "ruleset workflow contract", "WARN",
            f"workflow parse error: {err}",
        ))
    matching_surface = named | bare_keys
    missing = [
        (rel, ctx) for rel, ctx in contexts
        if ctx not in matching_surface
    ]
    if missing:
        by_file: dict[str, list[str]] = {}
        for rel, ctx in missing:
            by_file.setdefault(rel, []).append(ctx)
        details: list[str] = []
        for rel in sorted(by_file):
            details.append(
                f"{rel} requires {by_file[rel]} but no workflow job "
                f"has that name",
            )
        remediation = (
            "fix: rename the workflow job to match the required "
            "context (preferred - branch protection picks up the new "
            "context on the next commit), or update each ruleset "
            "context above to match a real workflow job `name:` - see "
            "docs/quality/ci-ruleset-contract.md"
        )
        rows.append(_CheckRow(
            "ruleset workflow contract", "FAIL",
            "; ".join(details) + f". {remediation}",
        ))
        return rows
    rows.append(_CheckRow(
        "ruleset workflow contract", "PASS",
        f"{len(contexts)} required contexts match workflow job names",
    ))
    return rows
