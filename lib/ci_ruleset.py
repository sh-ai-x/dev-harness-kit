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
    BypassActor          # namedtuple of (file, actor_type, repository_role, actor_id, bypass_mode)
    load_ruleset_contexts(target_dir) -> list[RulesetContext]
    load_workflow_job_names(target_dir) -> tuple[set[str], set[str], list[str]]
    check_ruleset_contract(target_dir) -> list[_CheckRow]
    load_ruleset_bypass_actors(target_dir) -> list[BypassActor]
    check_ruleset_bypass_actors(target_dir) -> list[_CheckRow]
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


class BypassActor(NamedTuple):
    """One `bypass_actors[]` entry extracted from a ruleset file.

    This is the local SSOT for the GitHub UI "Allow specified actors to
    bypass required pull requests" checkbox list. Each entry renders in
    the ruleset UI as a per-actor checkbox; clearing the list (or
    removing the only ADMIN entry) silently blocks admin/maintain
    emergency merge — the exact failure mode this SSOT guards against.

    Fields
    ------
    file : str
        Ruleset JSON path relative to `target_dir` (so error messages
        can point the operator at the offending file).
    actor_type : str
        GitHub actor discriminator: ``RepositoryRole``, ``User``,
        ``Team``, ``Integration``. Mirrors the GitHub REST rulesets
        API's `bypass_actors[].actor_type` enum verbatim.
    repository_role : str
        Uppercase role name (``ADMIN``, ``MAINTAIN``, ``WRITE``,
        ``TRIAGE``, ``READ``) when `actor_type == RepositoryRole`;
        empty string for User/Team/Integration actors.
    actor_id : int
        Numeric GitHub actor ID when `actor_type` is User/Team/
        Integration; 0 for RepositoryRole entries.
    bypass_mode : str
        One of ``always``, ``pull_request``, ``exempt`` (the GitHub
        REST enum). ``always`` lets the actor push directly to the
        protected ref; ``pull_request`` only bypasses status checks on
        PR branches (the contract `version-bump.yml` relies on for
        `DEV_KIT_GITHUB_TOKEN` admin-PAT pushes to merge_group refs).
    """
    file: str
    actor_type: str
    repository_role: str
    actor_id: int
    bypass_mode: str


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


def load_ruleset_bypass_actors(target_dir: Path) -> list[BypassActor]:
    """Load every ``bypass_actors[]`` entry from any ruleset under
    `<target>/.github/rulesets/*.json`.

    Returns a list of `BypassActor` namedtuples (one per actor entry,
    across all ruleset files). Empty list when no ruleset directory
    exists. Silently ignores unparseable JSON files; the ci-doctor
    caller surfaces a WARN row for those.

    The SSOT for these entries lives at
    `.github/rulesets/protect-main.json` in the source repo. When the
    local SSOT is absent (e.g. on a consumer install that configures
    rulesets out-of-band) this loader returns [] and the consumer-side
    ci-doctor emits an INFO row, not a FAIL — the contract is
    "reproducible when you author the SSOT, advisory otherwise".
    """
    out: list[BypassActor] = []
    ruleset_dir = Path(target_dir) / ".github" / "rulesets"
    if not ruleset_dir.is_dir():
        return out
    for json_path in sorted(ruleset_dir.glob("*.json")):
        payload = _read_json(json_path)
        if payload is None:
            continue
        rel = json_path.relative_to(target_dir).as_posix()
        # `payload` is guaranteed to be a dict here: `_read_json`
        # returned `None` for anything else (line 230-231 above), and
        # `payload is None` short-circuited this iteration. The
        # previous `isinstance(payload, dict)` ternary was redundant
        # dead code.
        actors = payload.get("bypass_actors")
        if not isinstance(actors, list):
            continue
        for entry in actors:
            if not isinstance(entry, dict):
                continue
            actor_type = entry.get("actor_type")
            if not isinstance(actor_type, str) or not actor_type:
                continue
            bypass_mode = entry.get("bypass_mode")
            if not isinstance(bypass_mode, str) or not bypass_mode:
                continue
            repository_role_raw = entry.get("repository_role")
            repository_role = (
                repository_role_raw.upper()
                if isinstance(repository_role_raw, str) and repository_role_raw
                else ""
            )
            actor_id_raw = entry.get("actor_id")
            # Accept either JSON int or a digit-string (e.g. `"12345"`
            # from a hand-edited SSOT). Other shapes (None, dict, list,
            # non-digit string) silently coerce to 0 instead of raising
            # — the SSOT may be partially-populated by GitHub's export
            # for actor types where actor_id is meaningless (Team /
            # RepositoryRole entries carry no actor_id).
            if isinstance(actor_id_raw, int) and not isinstance(actor_id_raw, bool):
                actor_id = actor_id_raw
            elif isinstance(actor_id_raw, str) and actor_id_raw.lstrip("-").isdigit():
                actor_id = int(actor_id_raw)
            else:
                actor_id = 0
            out.append(BypassActor(
                file=rel,
                actor_type=actor_type,
                repository_role=repository_role,
                actor_id=actor_id,
                bypass_mode=bypass_mode,
            ))
    return out


def check_ruleset_bypass_actors(
    target_dir: Path,
) -> list[_CheckRow]:
    """Cross-check the local ruleset `bypass_actors[]` against the
    admin-bypass SSOT contract.

    The contract (pinned by `tests/test_ruleset_bypass_actors.py`):

      - At least one `RepositoryRole:ADMIN` actor must be present with
        `bypass_mode == "always"`. Without it, the "Include
        administrators" / admin-bypass checkbox is silently unchecked
        and admin pushes (e.g. the `DEV_KIT_GITHUB_TOKEN` PAT path
        `version-bump.yml` relies on) start failing the ruleset.
      - The bypass_actors block must not be empty (an empty list is
        the explicit "checkbox cleared" state and surfaces as FAIL).

    Rows:
      - One INFO row when `.github/rulesets/` doesn't exist (nothing
        to validate against; consumer may author rulesets out-of-band).
      - One PASS row when the contract is satisfied.
      - One FAIL row listing every violation (missing ADMIN, ADMIN
        actor with bypass_mode != 'always', empty bypass_actors) plus
        a remediation hint pointing at
        `docs/quality/ci-ruleset-contract.md`.
      - WARN rows per unparseable ruleset file.

    The `source_repo` flag was previously honored for symmetry with the
    `check_ruleset_contract` contract but had no effect, so the
    keyword was dropped from both helpers (issue: dead-weight API
    surface kept "for symmetry" — better to drop and reintroduce when
    a real consumer-vs-source-repo distinction materializes).
    """
    ruleset_dir = Path(target_dir) / ".github" / "rulesets"
    if not ruleset_dir.is_dir():
        return [_CheckRow(
            "ruleset bypass actors", "INFO",
            "no .github/rulesets/*.json — SSOT absent, bypass config "
            "is advisory-only here (consumer may configure rulesets "
            "out-of-band). See docs/quality/ci-ruleset-contract.md",
        )]
    actors = load_ruleset_bypass_actors(target_dir)
    rows: list[_CheckRow] = []
    # Surface unparseable ruleset files as WARN so a corrupt JSON
    # never silently masks the contract (issue #774 same rationale:
    # the loader MUST NOT crash, but a parse failure deserves an
    # operator-visible signal).
    if ruleset_dir.is_dir():
        for json_path in sorted(ruleset_dir.glob("*.json")):
            if _read_json(json_path) is None:
                rel = json_path.relative_to(target_dir).as_posix()
                rows.append(_CheckRow(
                    "ruleset bypass actors", "WARN",
                    f"unparseable ruleset file: {rel}",
                ))
    # Empty bypass_actors list is the explicit "checkbox cleared"
    # state. Surface as FAIL — this is the failure mode the user
    # reported ("the checkbox feature disappeared").
    if not actors:
        rows.append(_CheckRow(
            "ruleset bypass actors", "FAIL",
            "no bypass_actors in any .github/rulesets/*.json — the "
            "admin bypass checkbox is cleared. Restore by adding at "
            "least one RepositoryRole:ADMIN actor with bypass_mode "
            "'always' to .github/rulesets/protect-main.json and "
            "running `bin/ruleset-sync.sh`. See "
            "docs/quality/ci-ruleset-contract.md.",
        ))
        return rows
    admin_actors = [
        a for a in actors
        if a.actor_type == "RepositoryRole" and a.repository_role == "ADMIN"
    ]
    if not admin_actors:
        files = sorted({a.file for a in actors})
        rows.append(_CheckRow(
            "ruleset bypass actors", "FAIL",
            f"no RepositoryRole:ADMIN actor in any "
            f".github/rulesets/*.json — the admin bypass checkbox is "
            f"missing. Found actors in {files}: "
            f"{[(a.file, a.actor_type, a.repository_role, a.bypass_mode) for a in actors]}. "
            f"See docs/quality/ci-ruleset-contract.md.",
        ))
        return rows
    wrong_mode = [
        a for a in admin_actors
        if a.bypass_mode != "always"
    ]
    if wrong_mode:
        rows.append(_CheckRow(
            "ruleset bypass actors", "FAIL",
            f"RepositoryRole:ADMIN actor(s) with bypass_mode != 'always' "
            f"in {wrong_mode[0].file}: "
            f"{[(a.file, a.bypass_mode) for a in wrong_mode]}. "
            f"'always' is the only mode that lets an admin push to "
            f"main directly — anything else keeps the bypass checkbox "
            f"effectively unchecked. See "
            f"docs/quality/ci-ruleset-contract.md.",
        ))
        return rows
    rows.append(_CheckRow(
        "ruleset bypass actors", "PASS",
        f"{len(admin_actors)} RepositoryRole:ADMIN actor(s) with "
        f"bypass_mode 'always' across "
        f"{sorted({a.file for a in admin_actors})} — admin bypass "
        f"checkbox is reproducible from the local SSOT.",
    ))
    return rows


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

    The `source_repo` flag was previously honored for symmetry with
    ci-doctor but had no effect, so the keyword was dropped. See
    `check_ruleset_bypass_actors` docstring for the rationale.
    """
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
