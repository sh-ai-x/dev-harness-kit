"""ci_workflow_diag.py — per-workflow diagnostic row emitters for ci-doctor.

Pulled out of `lib/ci_doctor.py`. Provides WARN/INFO-only diagnostic
rows that surface *why* a workflow might fail to run even when the
install is otherwise clean. Verdict-neutral (never FAIL, never flip
the audit). Caller passes the `Check` factory so this module stays
decoupled from `lib/ci_doctor.py`'s dataclass identity (no circular
import).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from lib.ci_workflow_yaml import (
    FIRST_PARTY_ACTION_PREFIXES,
    PR_FAMILY_TRIGGERS,
)
from lib.ci_workflow_yaml import (
    has_fork_guard as _has_fork_guard,
)
from lib.ci_workflow_yaml import (
    read_workflow as _read_workflow,
)

# Sentinel returned by `_wf_safe` when the workflow file couldn't be
# loaded OR couldn't be parsed. Caller emits an INFO row.
_INFO_KIND_MISSING = "missing"
_INFO_KIND_PARSE = "parse"


def _wf_safe(path: Path, kind: str) -> tuple[tuple[str, "object"] | None, "object" | None]:
    """Load workflow + parse shape; on miss/parse-error return an INFO Check.

    Returns `(None, info_check)` when the row to emit is just the
    INFO row (file missing / parse error). Otherwise returns
    `((raw, shape), None)` so the caller can probe fields.
    """
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None:
        return None, _info("INFO", rel_prefix=None, detail=err)
    assert shape is not None
    if shape.parse_error:
        return None, _info("INFO", rel_prefix=None,
                           detail=f"could not parse: {shape.parse_error}")
    return (raw, shape), None


def _info(state: str, rel_prefix: str | None, detail: str) -> "object":
    """Build a Check; implementation injected by caller as a Check factory."""
    raise RuntimeError("Check factory not configured — call run_diagnostics(...)")


def _trigger(rel: str, raw: str, shape: object, expected: frozenset[str],
             Check) -> "object":
    pr_family = shape.triggers & PR_FAMILY_TRIGGERS
    if not pr_family:
        return Check(
            label=f"workflow triggers: {rel}",
            state="WARN",
            detail=(
                f"no PR-family trigger; expected one of {sorted(expected)}; "
                f"got {sorted(shape.triggers) or 'none'}"
            ),
        )
    return Check(
        label=f"workflow triggers: {rel}",
        state="PASS",
        detail=f"triggers={sorted(shape.triggers)}",
    )


def _fork_pr_secret_gap(rel: str, raw: str, shape: object,
                        source_repo: bool, Check) -> "object":
    pr = "pull_request" in shape.triggers
    tgt = "pull_request_target" in shape.triggers
    wr = "workflow_run" in shape.triggers
    if not (pr and not (tgt or wr)):
        return Check(
            label=f"fork-PR secret gap: {rel}",
            state="PASS",
            detail=f"pull_request={'y' if pr else 'n'}  "
                   f"pull_request_target={'y' if tgt else 'n'}  "
                   f"workflow_run={'y' if wr else 'n'}",
        )
    if _has_fork_guard(raw):
        return Check(
            label=f"fork-PR secret gap: {rel}",
            state="PASS",
            detail="pull_request only, but fork PRs are skipped by a same-repo "
                   "guard (head.repo.full_name == github.repository)",
        )
    if source_repo:
        return Check(
            label=f"fork-PR secret gap: {rel}",
            state="INFO",
            detail="source repo: internal-branch PRs only — fork gap N/A",
        )
    return Check(
        label=f"fork-PR secret gap: {rel}",
        state="WARN",
        detail=("uses pull_request; no pull_request_target / workflow_run and no "
                "same-repo fork guard — fork PRs lose repo secrets"),
    )


def _paths_filter(rel: str, shape: object, Check) -> "object | None":
    if not shape.pr_paths:
        return None
    return Check(
        label=f"paths filter: {rel}",
        state="INFO",
        detail=f"pull_request paths={shape.pr_paths}  "
               "(verify it includes your changes)",
    )


def _branches_filter(rel: str, shape: object, Check) -> "object | None":
    if not shape.pr_branches:
        return None
    return Check(
        label=f"branches filter: {rel}",
        state="INFO",
        detail=f"pull_request branches={shape.pr_branches}  "
               "(verify your PR's target branch is in this list)",
    )


def _concurrency(rel: str, shape: object, Check) -> "object":
    if shape.concurrency_cancel:
        return Check(
            label=f"concurrency: {rel}",
            state="WARN",
            detail="cancel-in-progress=true — mid-run cancellation could drop a review",
        )
    return Check(label=f"concurrency: {rel}", state="PASS", detail="ok")


def _job_if_rows(rel: str, shape: object, Check) -> list["object"]:
    return [Check(label=f"job if: {rel}/{j.key}", state="INFO", detail=j.if_expr)
            for j in shape.jobs if j.if_expr]


def _job_name_rows(rel: str, shape: object, severity: str, Check) -> list["object"]:
    return [Check(
        label=f"job name: {rel}/{j.key}",
        state=severity,
        detail="no `name:` — surfaces as bare key in GitHub UI",
    ) for j in shape.jobs if not j.name]


def _action_pin(rel: str, shape: object, Check) -> "object | None":
    if not shape.uses:
        return None
    mutable: list[str] = []
    for ref in shape.uses:
        if any(ref.startswith(p) for p in FIRST_PARTY_ACTION_PREFIXES):
            continue
        if "@" not in ref:
            mutable.append(f"{ref} (no version)")
            continue
        ver = ref.rsplit("@", 1)[1]
        if not re.fullmatch(r"[0-9a-f]{40}", ver):
            mutable.append(ref)
    if not mutable:
        return None
    sample = mutable[:5]
    suffix = "…" if len(mutable) > 5 else ""
    return Check(
        label=f"action ref mutable: {rel}",
        state="INFO",
        detail=f"non-SHA refs: {sample}{suffix}  "
               "(consider pinning 3rd-party actions to a 40-char SHA for "
               "supply-chain hardening)",
    )


def _safe_info_rows(path: Path, Check) -> tuple[list["object"], tuple[str, "object"] | None]:
    """Return (info_rows, parsed_payload).

    `parsed_payload` is `(raw, shape)` on success so callers can emit
    `INFO` rows for each sub-check before the parsed probe. Returns
    `([], None)` when the file is missing/unparseable.
    """
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None:
        return [Check(label=_label_kind(rel, "workflow triggers"),
                      state="INFO", detail=err),
                Check(label=_label_kind(rel, "fork-PR secret gap"),
                      state="INFO", detail=err)], None
    assert shape is not None
    if shape.parse_error:
        msg = f"could not parse: {shape.parse_error}"
        return [Check(label=_label_kind(rel, "workflow triggers"),
                      state="INFO", detail=msg),
                Check(label=_label_kind(rel, "fork-PR secret gap"),
                      state="INFO", detail=msg)], None
    return [], (raw, shape)


def _label_kind(rel: str, kind: str) -> str:
    return f"{kind}: {rel}"


def run_diagnostics(
    target: Path,
    source_repo: bool,
    expected_triggers: dict[str, frozenset[str]],
    workdir_files: tuple[str, ...],
    Check: Callable[..., "object"],
) -> list["object"]:
    """Walk the four shipped workflows; emit one Check per finding."""
    out: list["object"] = []
    for rel in workdir_files:
        path = target / rel
        if not path.is_file():
            continue
        base = path.name
        expected = expected_triggers.get(base, frozenset())
        info_rows, parsed = _safe_info_rows(path, Check)
        out.extend(info_rows)
        if parsed is None:
            continue
        raw, shape = parsed
        out.append(_trigger(base, raw, shape, expected, Check))
        out.append(_fork_pr_secret_gap(base, raw, shape, source_repo, Check))
        if (r := _paths_filter(base, shape, Check)) is not None:
            out.append(r)
        if (r := _branches_filter(base, shape, Check)) is not None:
            out.append(r)
        out.append(_concurrency(base, shape, Check))
        if base == "review.yml":
            out.extend(_job_if_rows(base, shape, Check))
            out.extend(_job_name_rows(base, shape, "INFO", Check))
        elif base == "auto-fix-pr.yml":
            out.extend(_job_name_rows(base, shape, "WARN", Check))
        if (r := _action_pin(base, shape, Check)) is not None:
            out.append(r)
    return out
