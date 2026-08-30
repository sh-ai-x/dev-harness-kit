"""runner.py — orchestrator that ties dimensions, evidence, and fp_filter.

The engine has one public entrypoint: `run_analysis`. Each SKILL.md
body calls it with:

  dimensions:  list of Dimension objects (or names) to engage
  mode:        one of "read-only" | "delete" | "rewrite"
  paths:       list of root paths to consider (used for scope display)
  candidates:  {dim_name: [raw json findings]} gathered by the parent
               skill's parallel Agent fan-out. Optional — when omitted,
               the engine returns an empty AnalysisResult.

The pipeline is:
  parse_candidate      → coerce raw JSON to Evidence
  deterministic_filter → drop empty-scenario / low-confidence-minor
  dedupe               → collapse identical anchors
  threshold_by_mode    → drop below floor for delete/rewrite

If `verdicts` is provided, REJECTED items are dropped before rendering.

The renderer is intentionally simple markdown — no emojis, no colors —
because the SKILL.md body wraps it in the family-specific summary the
parent skill expects.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .dimensions import Dimension, resolve
from .evidence import (
    SEVERITY_ORDER,
    Evidence,
    Severity,
    Verdict,  # noqa: F401  (re-export for callers)
    parse_candidate,
)
from .fp_filter import (
    apply_verifier,
    dedupe,
    deterministic_filter,
    threshold_by_mode,
)

# ---- Bucket SSOT -----------------------------------------------------------
# The HIGH/MED/LOW bucket assigned to a single Evidence is computed in one
# place only: `_bucket_for(sev)`. Both the per-dim summary table and the
# section header dispatcher call this so the two render paths agree on
# classification for the same evidence.
#
# Mapping (locked):
#   CRITICAL → HIGH
#   MAJOR    → HIGH
#   MINOR    → MED
#   NIT      → LOW
_BUCKET_BY_SEVERITY = {
    Severity.CRITICAL: "HIGH",
    Severity.MAJOR: "HIGH",
    Severity.MINOR: "MED",
    Severity.NIT: "LOW",
}


def _bucket_for(sev: Severity) -> str:
    """Return the bucket label (HIGH/MED/LOW) for an evidence severity.

    Single source of truth — both `render_markdown`'s section header
    dispatcher and its per-dim table aggregator route through here so
    a MAJOR finding always lands in HIGH (not in the table's MED column)
    regardless of which renderer wrote it.
    """
    return _BUCKET_BY_SEVERITY[sev]


# Well-known secret patterns that must be masked at the engine boundary
# before any expert free-text hits the report or the suggested diff.
# Pattern set is the SSOT required by the `secret` dimension charter
# (lib/analysis_core/dimensions.py + skills/inspect/SKILL.md). Adding a
# new credential family means appending here, not editing call sites.
_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),                  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),            # GCP API key
    re.compile(r"ghp_[0-9A-Za-z]{36}"),               # GitHub personal access token
    re.compile(r"gho_[0-9A-Za-z]{36}"),               # GitHub OAuth token
    re.compile(r"sk-[0-9A-Za-z]{32,}"),               # OpenAI-style secret key
    re.compile(r"sk-ant-[0-9A-Za-z\-]{32,}"),         # Anthropic admin key
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),     # Slack token
    re.compile(
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"
    ),                                                # PEM private key block
    re.compile(
        r"postgres(?:ql)?://[^\s:@]+:[^\s@]+@[^\s]+"
    ),                                                # postgres credential URI
    re.compile(
        r"mongodb(?:\+srv)?://[^\s:@]+:[^\s@]+@[^\s]+"
    ),                                                # mongodb credential URI
)
_SECRET_PLACEHOLDER = "[REDACTED]"


def _mask_secrets(text: str) -> str:
    """Mask any well-known secret-shape strings in free-text fields.

    Applied at the engine boundary (render_markdown + emit_suggested_diffs)
    so the secret-dimension charter's "never echo a real key" invariant
    survives regardless of how the parent skill uses the output.
    """
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_SECRET_PLACEHOLDER, out)
    return out


def _is_in_scope(file: str, scope_paths: Sequence[Path]) -> bool:
    """True iff file resolves under any of the scope paths.

    When `scope_paths` is empty, accept everything (no scope filter).
    Files that fail to resolve are treated as out-of-scope.
    """
    if not scope_paths:
        return True
    try:
        f = Path(file).resolve()
    except (OSError, ValueError):
        return False
    for sp in scope_paths:
        try:
            sp_resolved = sp.resolve()
        except (OSError, ValueError):
            continue
        try:
            f.relative_to(sp_resolved)
            return True
        except ValueError:
            continue
    return False


@dataclass
class SuggestedDiff:
    """One mutation the engine is willing to suggest for a finding."""

    file: str
    line: int
    dim: str
    command: str  # rm path / git rm path / "# rewrite: <hint>"
    reason: str


@dataclass
class AnalysisResult:
    """Immutable bag of kept findings + scope metadata."""

    dimensions: Tuple[Dimension, ...]
    mode: str
    paths: Tuple[Path, ...]
    findings: Tuple[Evidence, ...]
    kept_count: int
    filtered_count: int

    @property
    def verdict(self) -> str:
        """Inspect verdict per parent skill convention.

        Rules:
          - empty       → Healthy
          - >= 1 HIGH   → Critical (any CRITICAL/MAJOR present)
          - >= 3 MED    → Major drift
          - else        → Minor drift

        The HIGH/MED/LOW buckets come from `_bucket_for` so the verdict
        matches the per-dim table and the section header dispatcher.
        """
        if not self.findings:
            return "Healthy"
        high = sum(1 for f in self.findings if _bucket_for(f.severity) == "HIGH")
        med = sum(1 for f in self.findings if _bucket_for(f.severity) == "MED")
        if high >= 1:
            return "Critical"
        if med >= 3:
            return "Major drift"
        return "Minor drift"


def run_analysis(
    dimensions: Sequence[Dimension | str],
    mode: str,
    paths: Iterable[Path | str],
    *,
    candidates: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
    verdicts: Optional[Sequence[Tuple[Any, Any, str]]] = None,
) -> AnalysisResult:
    """Deterministic engine. See module docstring for full semantics.

    Returns an AnalysisResult; never raises on empty input.
    """
    if mode not in {"read-only", "delete", "rewrite"}:
        raise ValueError(
            f"analysis mode must be one of read-only/delete/rewrite, got {mode!r}"
        )
    dims = tuple(resolve(dimensions))
    paths_t = tuple(Path(p) for p in paths)
    raw = candidates or {}

    parsed: List[Evidence] = []
    for d in dims:
        for raw_item in raw.get(d.name, []) or []:
            try:
                payload = dict(raw_item)
                ev = parse_candidate(payload, dim_fallback=d.name)
            except (ValueError, TypeError):
                # Malformed item from a per-dim expert is dropped silently.
                # Verifier pass would catch this; deterministic mode skips it.
                continue
            # The candidate-map dimension is AUTHORITATIVE: a non-empty
            # inner `dim` that disagrees with the outer map key is
            # rewritten to the outer name so a `smell` expert returning
            # `dim: dead` cannot route a rewrite suggestion through the
            # delete-only dimension and surface a `git rm` for a live
            # module.
            if ev.dim != d.name:
                ev = dataclasses.replace(ev, dim=d.name)
            # Enforce path scope at parse time so out-of-scope findings
            # never reach the filter pipeline (or downstream mutation).
            if not _is_in_scope(ev.file, paths_t):
                continue
            parsed.append(ev)

    pre_filter_count = len(parsed)
    filtered = deterministic_filter(parsed)
    deduped = dedupe(filtered)
    floor_by_dim = {d.name: d.severity_floor for d in dims}
    thresholded = threshold_by_mode(deduped, mode, floor_by_dim=floor_by_dim)
    if verdicts is not None:
        thresholded = apply_verifier(thresholded, verdicts)
    thresholded = _sort(thresholded)

    return AnalysisResult(
        dimensions=dims,
        mode=mode,
        paths=paths_t,
        findings=tuple(thresholded),
        kept_count=len(thresholded),
        filtered_count=pre_filter_count - len(thresholded),
    )


def _sort(items: Sequence[Evidence]) -> List[Evidence]:
    sev_index = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    return sorted(
        items,
        key=lambda e: (sev_index[e.severity], e.file, e.line, e.dim),
    )


def render_markdown(result: AnalysisResult) -> str:
    """Markdown report for the kept findings. Deterministic + parent-friendly.

    Every rendered/exported field — including the scope header — is
    passed through `_mask_secrets` so a secret-shaped path the user
    explicitly audited can never echo back into the report header.
    """
    raw_scope = ", ".join(str(p) for p in result.paths) or "(no paths)"
    scope = _mask_secrets(raw_scope) if raw_scope else raw_scope
    out: List[str] = [
        "# Analysis Report",
        "",
        f"**Verdict:** {result.verdict}",
        f"**Mode:** {result.mode}",
        f"**Scope:** {scope}",
        f"**Dimensions:** {', '.join(d.name for d in result.dimensions)}",
        f"**Coverage:** {result.kept_count} findings kept, "
        f"{result.filtered_count} filtered as false positive / low-signal",
        "",
        "## Per-dimension summary",
        "",
        "| dim | HIGH | MED | LOW |",
        "|-----|------|-----|-----|",
    ]
    by_dim: Dict[str, List[Evidence]] = {}
    for f in result.findings:
        by_dim.setdefault(f.dim, []).append(f)

    for d in result.dimensions:
        items = by_dim.get(d.name, [])
        counts = {"HIGH": 0, "MED": 0, "LOW": 0}
        for f in items:
            counts[_bucket_for(f.severity)] += 1
        out.append(
            f"| {d.name} | {counts['HIGH']} | {counts['MED']} | {counts['LOW']} |"
        )

    out.append("")

    def _render_finding(f: Evidence) -> List[str]:
        # Mask secrets at the engine boundary so secret-dim findings
        # never echo a real key in the rendered report. Apply to every
        # rendered/exported field, including the file path itself, so
        # a secret-shaped path cannot leak even when the same token
        # would be masked in title/scenario fields.
        title = _mask_secrets(f.title)
        tldr = _mask_secrets(f.tldr)
        scenario = _mask_secrets(f.failure_scenario)
        fix_hint = _mask_secrets(f.fix_hint) if f.fix_hint else None
        safe_file = _mask_secrets(f.file)
        # Bullet shape is locked to match `lib/render_report_html.py`'s
        # _parse_inspect_findings regex so the HTML report consumer keeps
        # working. Field keys must stay in {Dim, TL;DR, Scenario, Fix}.
        block = [
            f"- [{f.severity.value.upper()} | {f.confidence.upper()}] "
            f"{title} -- {safe_file}:{f.line}",
            f"  Dim: {f.dim}",
            f"  TL;DR: {tldr}",
            f"  Scenario: {scenario}",
        ]
        if fix_hint:
            block.append(f"  Fix: {fix_hint}")
        block.append("")
        return block

    if not result.findings:
        out.append("## HIGH (0)")
        out.append("")
        out.append("(no findings)")
        out.append("")
        return "\n".join(out)

    # Bucket findings by HIGH/MED/LOW so the HTML consumer's dispatch
    # in `lib/render_report_html.py:387-393` can reach every block.
    # The per-dim table above uses the SAME `_bucket_for` so the table
    # and the section headers agree on classification for each evidence.
    by_bucket: Dict[str, List[Evidence]] = {"HIGH": [], "MED": [], "LOW": []}
    for f in result.findings:
        by_bucket[_bucket_for(f.severity)].append(f)
    for header, items in by_bucket.items():
        out.append(f"## {header} ({len(items)})")
        out.append("")
        for f in items:
            out.extend(_render_finding(f))
    return "\n".join(out)


def emit_suggested_diffs(result: AnalysisResult) -> List[SuggestedDiff]:
    """Translate kept findings into mutation commands per the mode.

    Thin dispatcher (issue #310): delegates per-mode logic to
    `_diffs_for_delete` and `_diffs_for_rewrite` so the gate logic for
    each mutation mode is readable in isolation. Per-dim mode gating
    still happens here so a single dim cannot request both `git rm`
    AND a `# rewrite:` for the same finding.

    - delete:    `rm <file>` / `git rm <file>` for each non-zero finding
                 whose Dimension.mode == "delete". Rewrite-only dims
                 never get a `git rm`, even when the engine is asked
                 to surface suggestions in delete mode.
    - rewrite:   `# rewrite: <fix_hint>` per finding whose
                 Dimension.mode == "rewrite". Delete-only dims are
                 skipped — their mutations belong to `delete` mode.
    - read-only: empty list (no mutation surfaced)

    Rationale: group("inspect") mixes delete-only dims (dead) with
    rewrite-only dims (dup, smell, overeng, overarch, cleancode).
    Letting delete-mode emit `git rm` for a refactoring smell would
    destroy a valid source file. The per-dim mode is the gate.
    """
    if result.mode == "read-only":
        return []
    dim_by_name = {d.name: d for d in result.dimensions}
    out: List[SuggestedDiff] = []
    for f in result.findings:
        d = dim_by_name.get(f.dim)
        if d is None or d.mode != result.mode:
            continue  # dim does not support this mutation mode
        if result.mode == "delete":
            out.extend(_diffs_for_delete(f))
        elif result.mode == "rewrite":
            out.append(_diff_for_rewrite(f))
    return out


# The four safety booleans required on a whole-file deletion proof.
# Each must be a True value for `git rm` to fire; a missing or False
# key means the proof is incomplete and the engine surfaces a
# `# delete-blocked:` advisory instead.
_WHOLE_FILE_PROOF_KEYS = (
    "no_importers",
    "no_callers",
    "no_references",
    "no_runtime_calls",
)


def _whole_file_proof_complete(proof: Dict[str, bool]) -> bool:
    """Strict gate: every required safety key is present AND truthy.

    Strict mode (issue #310): a proof missing any of the four required
    keys is incomplete. Truthy values only — `None`/missing/falsey
    blocks the deletion. The parser already coerces values to bool so
    by this point every key that survives IS a real bool.
    """
    return all(bool(proof.get(k)) for k in _WHOLE_FILE_PROOF_KEYS)


def _diff_for_line_delete(f: Evidence) -> SuggestedDiff:
    """Line-level delete suggestion: emit `# delete-line N` patch.

    Used when `deletion_scope != "whole-file"` — a single line, a set
    of lines, or a spans tuple. The human reviewer can tell the
    suggestion is a removal of that specific line, not the file.
    """
    return SuggestedDiff(
        file=_mask_secrets(f.file),
        line=f.line,
        dim=f.dim,
        command=f"# delete-line {f.line} in {_mask_secrets(f.file)}",
        reason=_mask_secrets(f.failure_scenario),
    )


def _diff_for_delete_blocked(f: Evidence, proof: Dict[str, bool]) -> SuggestedDiff:
    """Whole-file proof is incomplete: emit `# delete-blocked:` advisory.

    Reasons this fires:
      - `deletion_scope != "whole-file"` (already filtered upstream)
      - any of the four required proof keys is missing or False
    """
    missing = [k for k in _WHOLE_FILE_PROOF_KEYS if not proof.get(k)]
    return SuggestedDiff(
        file=_mask_secrets(f.file),
        line=None,
        dim=f.dim,
        command=(
            "# delete-blocked: requires "
            + " AND ".join(_WHOLE_FILE_PROOF_KEYS)
            + f" proof (missing/false: {missing or '[]'})"
        ),
        reason=_mask_secrets(f.failure_scenario),
    )


def _diff_for_git_rm(f: Evidence) -> SuggestedDiff:
    """Whole-file deletion with complete proof: emit `git rm`."""
    return SuggestedDiff(
        file=_mask_secrets(f.file),
        line=None,
        dim=f.dim,
        command=f"git rm {_mask_secrets(f.file)}",
        reason=_mask_secrets(f.failure_scenario),
    )


def _diffs_for_delete(f: Evidence) -> List[SuggestedDiff]:
    """Per-finding delete-mode dispatch (split helper for issue #310).

    Sequence:
      1. Line-level evidence → `# delete-line N` (no proof required).
      2. Whole-file evidence with incomplete proof → `# delete-blocked`.
      3. Whole-file evidence with complete proof → `git rm`. Each
         file appears at most once in the output even if multiple
         findings point at it.
    """
    proof = f.deletion_proof or {}
    if f.deletion_scope != "whole-file":
        return [_diff_for_line_delete(f)]
    if not _whole_file_proof_complete(proof):
        return [_diff_for_delete_blocked(f, proof)]
    return [_diff_for_git_rm(f)]


def _diff_for_rewrite(f: Evidence) -> SuggestedDiff:
    """Rewrite-mode suggestion: emit `# rewrite: <fix>` patch.

    Schema boundary: emit `f.fix` (verbatim code/patch) into the diff
    stream. NEVER emit `f.fix_hint` into a diff — `fix_hint` is for
    reports only. Fall back to `fix_hint` only when `f.fix` is absent
    (legacy data path).
    """
    patch_text = f.fix if f.fix else (f.fix_hint or "see scenario")
    return SuggestedDiff(
        file=_mask_secrets(f.file),
        line=f.line,
        dim=f.dim,
        command=f"# rewrite: {_mask_secrets(patch_text)}",
        reason=_mask_secrets(f.failure_scenario),
    )


# inspect 2026-08-27 overarch-1: promote `_mask_secrets` to a public
# `mask_secrets` so external callers (e.g. `lib.verify_harness`) do not
# have to reach into the analysis_core subpackage's private namespace.
# Both names point to the same function; the underscore form is kept as
# a back-compat alias for any in-package call sites that haven't
# migrated yet.
mask_secrets = _mask_secrets
