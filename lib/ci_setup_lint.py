"""ci_setup_lint.py — lint helpers for ci-setup's installed-workflows check.

Pulled out of `lib/ci_setup.py` so the install orchestrator stays
focused on the marker / drift / template lifecycle. Pure stdlib;
lazy-imports PyYAML only on the lint path (the install path must
not require it).
"""
from __future__ import annotations

from pathlib import Path
from typing import List

# Patterns of known-bad install artifacts that the lint pass surfaces.
# Each entry: (path, substring, explanation). The lint is best-effort and
# never raises; matches become `InstallReport.warnings` entries so the
# skill body can print them in the summary table and the user can act
# on them (typically by re-running with `--force` to refresh the
# template).
_KNOWN_STALE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        ".github/workflows/review.yml",
        # Pre-0.1.3 gate hard-failed in pull_request mode on missing
        # verdicts while defaulting to Approve in workflow_dispatch
        # mode. The patched gate defaults missing verdicts to Approve
        # with a ::warning:: in both event modes.
        "Re-run via workflow_dispatch if needed",
        "stale pull_request hard-fail gate in review.yml -- the gate used to exit 1 "
        "with 'Missing verdict' whenever the /dev-kit:* agents skipped posting a verdict "
        "comment, even though the gate's own documented intent (lines 354-358) tolerates "
        "missing verdicts and the workflow_dispatch branch already defaulted to Approve. "
        "Re-run with `--force` to refresh the template; the patched gate defaults "
        "missing verdicts to Approve with a ::warning:: in both event modes.",
    ),
    (
        ".github/workflows/review.yml",
        # Issue #726: pre-fix gate hard-failed whenever
        # verdict_source=needs-fallback-bootstrap-pr, contradicting its
        # own documented fallback contract.
        "Merge this PR's workflow changes to main first.",
        "stale bootstrap-PR hard-fail gate in review.yml (issue #726) -- the gate "
        "used to exit 1 with 'Merge this PR's workflow changes to main first' "
        "whenever the anthropics/claude-code-action@v1 anti-recursion guard "
        "skipped both review and security on a PR that modifies "
        ".github/workflows/*. The fallback contract posts a synthesized "
        "'Verdict: Approve' tagged verdict_source=needs-fallback-bootstrap-pr; "
        "the pre-fix gate contradicted this by hard-failing on agent_ran=false. "
        "Re-run with `--force` to refresh the template; the patched gate tolerates "
        "the BOTH-bootstrap case via an AND on R_SOURCE+S_SOURCE and falls "
        "through to the rank/case logic. Mixed or non-bootstrap signatures still "
        "hard-fail (issue #212-C1 install-broken protection preserved).",
    ),
)

# Workflow files checked for `#`-inside-block-scalar anti-pattern
# (issue #219 Bug 1). YAML literal block scalars (`if: |`) and folded
# block scalars (`if: >`) treat every indented line, including
# `#`-prefixed comments, as part of the expression string passed to
# GitHub's expression parser. The lint pass scans each file's parsed
# YAML for `if:` blocks whose values contain `#`-prefixed lines and
# reports the first such occurrence.
_IF_BLOCK_SCALAR_WORKFLOWS: tuple[str, ...] = (
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/review.yml",
    ".github/workflows/ci.yml",
)


def _lint_if_block_scalar_hashes(content: str, rel: str) -> List[str]:
    """Return one warning string per `#`-prefixed line inside any
    `if: |` / `if: >` block scalar in `content`, else [].

    The check is YAML-aware: only literal/folded block scalars under
    `if:` (or `if` at any depth — e.g. `jobs.<name>.if`) count.
    """
    # Lazy import: PyYAML is only needed for the lint path; the install
    # path must not require it.
    import yaml  # type: ignore
    out: List[str] = []
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        # A YAML syntax error in a workflow file is already surfaced by
        # GitHub's UI; the lint pass is for the more subtle
        # `#`-in-block pattern.
        return out

    def _scan(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else str(k)
                if k == "if" and isinstance(v, str) and "\n" in v:
                    bad = [ln for ln in v.splitlines() if ln.lstrip().startswith("#")]
                    if bad:
                        out.append(
                            f"{rel}: {child_path}: `#`-prefixed line inside "
                            f"`if:` block scalar breaks GitHub Actions "
                            f"expression parser (issue #219 Bug 1). "
                            f"Move the comment ABOVE `if:`. "
                            f"First offender: {bad[0]!r}"
                        )
                        return
                _scan(v, child_path)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _scan(v, f"{path}[{i}]")

    _scan(doc, "")
    return out


def lint_installed_workflows(target_dir: Path) -> List[str]:
    """Scan installed EXPECTED_PATHS for known-stale patterns + `#`-in-block bugs.

    Returns one human-readable finding per match. Lint output is
    advisory; the install itself never blocks on it.
    """
    out: List[str] = []
    target = Path(target_dir).resolve()
    for rel, needle, explain in _KNOWN_STALE_PATTERNS:
        p = target / rel
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle in content:
            out.append(f"{rel}: {explain}")
    for rel in _IF_BLOCK_SCALAR_WORKFLOWS:
        p = target / rel
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(_lint_if_block_scalar_hashes(content, rel))
    return out
