#!/usr/bin/env python3
"""harness_audit.py — Cross-harness quality audit (Phase 7, issue #387).

Reads the on-disk state of 4 dev-kit harnesses (hooks, eval,
research, interview) and reports per-harness health:

- alpha classification (SKILL.md frontmatter)
- L7 alignment (alpha ∈ state|enforcement|analysis)
- rubric completeness (eval/rubrics/*.yaml present)

Read-only by construction — never writes to .dev-kit/state.json, never
mutates state.json, never invokes network I/O. CLI flags:

  --json          emit machine-readable JSON to stdout (agent)
  --html-out PATH write HTML report to PATH (user)
  --project-root  project root (default: cwd)
  --text          brief text summary to stdout (log files)

Default (no flag) per #389: write HTML to
`.dev-kit/harness-audit-report.html`. Exit 0 when all harnesses shipped
AND no alpha_invalid; otherwise 1. No path returns 2 in practice
(the only file writes are the user's chosen --html-out PATH or the
default `.dev-kit/harness-audit-report.html` artifact).

Per #387, the audit covers 4 harnesses and is **strictly read-only**
(verified by `tests/test_harness_audit.py::test_audit_is_read_only`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Make sibling `lib/` importable so `from render_report_html import ...`
# works regardless of the cwd the script is invoked from.
_PROJECT_ROOT_HINT = Path(__file__).resolve().parent.parent
_LIB_PATH = _PROJECT_ROOT_HINT / "lib"
if _LIB_PATH.is_dir() and str(_LIB_PATH) not in sys.path:
    sys.path.insert(0, str(_LIB_PATH))

HARNESSES = ("hooks", "eval", "research", "interview")

VALID_ALPHA = ("state", "enforcement", "analysis")

EVAL_EXPECTED_RUBRICS = frozenset({"harness-quality.yaml", "os-quality.yaml"})

HOOKS_EXPECTED = frozenset({
    "worktree-guard.sh", "git-guard.sh", "bash-guard.sh",
    "tdd-guard.sh", "secret-scan.sh", "slop-detector.sh", "stop-verify.sh",
})


@dataclass
class HarnessAudit:
    name: str
    shipped: bool
    alpha: str
    alpha_valid: bool
    resource_count: int
    resource_expected: int
    rubric_count: int
    rubric_expected: int
    findings: List[str] = field(default_factory=list)


def _read_alpha(project_root: Path, skill_name: str) -> tuple[Optional[str], bool]:
    path = project_root / "skills" / skill_name / "SKILL.md"
    if not path.exists():
        return (None, False)
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return (None, False)
    try:
        import yaml  # local import: keeps harness_audit import-cheap
        fm = yaml.safe_load(m.group(1))
    except ImportError:
        # PyYAML is not installed; `yaml` is therefore not bound, so the
        # follow-up `yaml.YAMLError` reference would raise NameError. Catch
        # the missing-import case first and return the same fallback.
        return (None, False)
    except yaml.YAMLError:
        return (None, False)
    alpha = fm.get("alpha") if isinstance(fm, dict) else None
    if not isinstance(alpha, str):
        return (None, False)
    return (alpha, alpha in VALID_ALPHA)


def _py_modules(dirpath: Path) -> set:
    if not dirpath.is_dir():
        return set()
    return {p.stem for p in dirpath.glob("*.py")} - {"__init__"}


def audit_hooks(project_root: Path) -> HarnessAudit:
    hooks_dir = project_root / "hooks"
    found_hooks = {p.name for p in hooks_dir.glob("*.sh")} if hooks_dir.is_dir() else set()
    matched = found_hooks & HOOKS_EXPECTED
    claude_settings = project_root / ".claude" / "settings.json"
    codex_hooks = project_root / ".codex" / "hooks.json"
    findings: List[str] = []
    if not (claude_settings.exists() or codex_hooks.exists()):
        findings.append("no runtime hook wiring (.claude/settings.json or .codex/hooks.json)")
    missing_hooks = sorted(HOOKS_EXPECTED - found_hooks)
    if missing_hooks:
        findings.append(f"missing hook scripts: {missing_hooks}")
    # Symmetric with audit_eval: shipped iff
    # ALL expected scripts present AND at least one runtime hook wiring.
    return HarnessAudit(
        name="hooks",
        shipped=not missing_hooks and bool(claude_settings.exists() or codex_hooks.exists()),
        alpha="enforcement",
        alpha_valid=True,
        resource_count=len(matched),
        resource_expected=len(HOOKS_EXPECTED),
        rubric_count=0, rubric_expected=0,
        findings=findings,
    )


def audit_eval(project_root: Path) -> HarnessAudit:
    engine = (project_root / "lib" / "eval_runner.py").exists()
    judge = (project_root / "lib" / "llm_judge.py").exists()
    rubric_dir = project_root / "eval" / "rubrics"
    found_rubrics = {p.name for p in rubric_dir.glob("*.yaml")} if rubric_dir.is_dir() else set()
    matched_rubrics = found_rubrics & EVAL_EXPECTED_RUBRICS
    alpha, alpha_valid = _read_alpha(project_root, "evaluate")
    findings: List[str] = []
    if not engine:
        findings.append("missing lib/eval_runner.py")
    if not judge:
        findings.append("missing lib/llm_judge.py")
    missing_rubrics = sorted(EVAL_EXPECTED_RUBRICS - found_rubrics)
    if missing_rubrics:
        findings.append(f"missing eval rubrics: {missing_rubrics}")
    if not alpha:
        findings.append("skills/evaluate/SKILL.md missing alpha frontmatter")
    elif not alpha_valid:
        findings.append(f"skills/evaluate alpha={alpha!r} not in {VALID_ALPHA}")
    return HarnessAudit(
        name="eval",
        shipped=engine and judge and not missing_rubrics and alpha_valid,
        alpha=alpha or "",
        alpha_valid=alpha_valid,
        resource_count=0, resource_expected=0,
        rubric_count=len(matched_rubrics),
        rubric_expected=len(EVAL_EXPECTED_RUBRICS),
        findings=findings,
    )


def audit_research(project_root: Path) -> HarnessAudit:
    engine = (project_root / "lib" / "research_engine.py").exists()
    skill = (project_root / "skills" / "research" / "SKILL.md").exists()
    alpha, alpha_valid = _read_alpha(project_root, "research")
    findings: List[str] = []
    if not engine:
        findings.append("missing lib/research_engine.py")
    if not skill:
        findings.append("missing skills/research/SKILL.md")
    elif not alpha:
        findings.append("skills/research/SKILL.md missing alpha frontmatter")
    elif not alpha_valid:
        findings.append(f"skills/research alpha={alpha!r} not in {VALID_ALPHA}")
    return HarnessAudit(
        name="research",
        shipped=engine and skill and alpha_valid,
        alpha=alpha or "",
        alpha_valid=alpha_valid,
        resource_count=0, resource_expected=0,
        rubric_count=0, rubric_expected=0,
        findings=findings,
    )


def audit_interview(project_root: Path) -> HarnessAudit:
    engine = (project_root / "lib" / "interview_engine.py").exists()
    skill = (project_root / "skills" / "interview" / "SKILL.md").exists()
    alpha, alpha_valid = _read_alpha(project_root, "interview")
    findings: List[str] = []
    if not engine:
        findings.append("missing lib/interview_engine.py")
    if not skill:
        findings.append("missing skills/interview/SKILL.md")
    elif not alpha:
        findings.append("skills/interview/SKILL.md missing alpha frontmatter")
    elif not alpha_valid:
        findings.append(f"skills/interview alpha={alpha!r} not in {VALID_ALPHA}")
    return HarnessAudit(
        name="interview",
        shipped=engine and skill and alpha_valid,
        alpha=alpha or "",
        alpha_valid=alpha_valid,
        resource_count=0, resource_expected=0,
        rubric_count=0, rubric_expected=0,
        findings=findings,
    )


AUDITORS = {
    "hooks": audit_hooks,
    "eval": audit_eval,
    "research": audit_research,
    "interview": audit_interview,
}


def run_audit(project_root: Path) -> Dict:
    audits = [AUDITORS[name](project_root) for name in HARNESSES]
    shipped_count = sum(1 for a in audits if a.shipped)
    findings_total = sum(len(a.findings) for a in audits)
    alpha_invalid = [a.name for a in audits if a.alpha and not a.alpha_valid]
    return {
        "harnesses": [asdict(a) for a in audits],
        "summary": {
            "total": len(audits),
            "shipped": shipped_count,
            "findings": findings_total,
            "alpha_invalid": alpha_invalid,
            "all_shipped": shipped_count == len(audits),
        },
        "read_only": True,
    }


def to_json(audit: Dict) -> str:
    return json.dumps(audit, indent=2, sort_keys=False)


KST = timezone(timedelta(hours=9))


def _now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")


def to_html(audit: Dict, *, now: Optional[str] = None) -> str:
    try:
        from render_report_html import _esc, _now_iso  # type: ignore
        esc = _esc
        fmt_now = now or _now_iso()
        title = "dev-harness-kit — Cross-Harness Audit (Phase 7)"
        parts: List[str] = [
            '<!DOCTYPE html>\n<html lang="en"><head>',
            '<meta charset="utf-8">',
            f'<title>{esc(title)}</title>',
            f'<style>{_SHARED_CSS}</style>',
            '</head><body>',
            f'<h1>{esc(title)}</h1>',
            f'<p class="meta">Generated {esc(fmt_now)}</p>',
        ]
        s = audit.get("summary", {})
        parts.append(
            f'<div class="cards">'
            f'<div class="card"><div class="label">Shipped</div>'
            f'<div class="value verdict-ok">{esc(s.get("shipped", 0))}/{esc(s.get("total", 0))}</div></div>'
            f'<div class="card"><div class="label">Findings</div>'
            f'<div class="value verdict-warn">{esc(s.get("findings", 0))}</div></div>'
            f'<div class="card"><div class="label">Alpha invalid</div>'
            f'<div class="value verdict-bad">{esc(len(s.get("alpha_invalid", [])))}</div></div>'
            f'</div>'
        )
        parts.append('<h2>Harnesses</h2>')
        parts.append('<table><tr><th>Harness</th><th>Shipped</th><th>Alpha</th>'
                     '<th>Resources</th><th>Rubrics</th><th>Findings</th></tr>')
        for h in audit.get("harnesses", []):
            shipped = "✓" if h.get("shipped") else "✗"
            cls = "verdict-ok" if h.get("shipped") else "verdict-bad"
            res = f'{h.get("resource_count", 0)}/{h.get("resource_expected", 0)}'
            rub = f'{h.get("rubric_count", 0)}/{h.get("rubric_expected", 0)}'
            findings_html = "<br>".join(esc(f) for f in h.get("findings", [])) or "—"
            alpha = h.get("alpha", "") or "—"
            if h.get("alpha") and not h.get("alpha_valid"):
                alpha = f'<span class="verdict-bad">{esc(alpha)}</span>'
            parts.append(
                f'<tr><td><b>{esc(h.get("name", ""))}</b></td>'
                f'<td class="{esc(cls)}">{esc(shipped)}</td>'
                f'<td>{alpha}</td>'
                f'<td>{esc(res)}</td><td>{esc(rub)}</td>'
                f'<td>{findings_html}</td></tr>'
            )
        parts.append('</table>')
        parts.append('<footer>Generated by <code>tools/harness_audit.py</code>. '
                     'No external assets, no JavaScript.</footer>')
        parts.append('</body></html>\n')
        return "".join(parts)
    except ImportError:
        return _minimal_html(audit, now or _now_iso())


_SHARED_CSS = """
:root {
  color-scheme: light dark;
  --fg: #1a1a1a; --bg: #fafafa; --muted: #3a3a3a;
  --border: #c8c8c8; --card-bg: #ffffff;
  --th-bg: #ededed; --row-alt: #f5f5f5; --code-bg: #f1f1f1;
}
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececec; --bg: #1a1a1a; --muted: #c8c8c8;
    --border: #444444; --card-bg: #232323;
    --th-bg: #2e2e2e; --row-alt: #1e1e1e; --code-bg: #2a2a2a; }
}
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       max-width: 1100px; margin: 2rem auto; padding: 0 1.5rem;
       line-height: 1.5; color: var(--fg); background: var(--bg); }
h1 { border-bottom: 2px solid var(--border); padding-bottom: 0.5rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid var(--border);
     padding-bottom: 0.3rem; }
.meta { color: var(--muted); font-size: 0.9em; }
.cards { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }
.card { background: var(--card-bg); border: 1px solid var(--border);
        border-radius: 6px; padding: 0.8rem 1.2rem; min-width: 140px; }
.card .label { font-size: 0.8em; color: var(--muted);
               text-transform: uppercase; letter-spacing: 0.05em; }
.card .value { font-size: 1.8em; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 0.8rem 0; }
th, td { border: 1px solid var(--border); padding: 0.5rem 0.7rem; text-align: left; }
th { background: var(--th-bg); font-weight: 600; }
tr:nth-child(even) td { background: var(--row-alt); }
.verdict-ok { color: #157a3a; font-weight: 600; }
.verdict-warn { color: #a06400; font-weight: 600; }
.verdict-bad { color: #b03030; font-weight: 600; }
footer { margin-top: 3rem; padding-top: 1rem;
         border-top: 1px solid var(--border);
         color: var(--muted); font-size: 0.85em; }
"""


def _minimal_html(audit: Dict, now: str) -> str:
    import html as _html
    esc = lambda v: _html.escape(str(v), quote=True)  # noqa: E731
    s = audit.get("summary", {})
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<title>dev-harness-kit — Cross-Harness Audit</title>',
        f'<style>{_SHARED_CSS}</style>',
        '</head><body>',
        '<h1>dev-harness-kit — Cross-Harness Audit</h1>',
        f'<p class="meta">Generated {esc(now)}</p>',
        f'<p><b>Shipped:</b> {esc(s.get("shipped", 0))}/{esc(s.get("total", 0))}; '
        f'<b>Findings:</b> {esc(s.get("findings", 0))}</p>',
        '<table><tr><th>Harness</th><th>Shipped</th><th>Alpha</th><th>Findings</th></tr>',
    ]
    for h in audit.get("harnesses", []):
        shipped = "yes" if h.get("shipped") else "no"
        findings = "<br>".join(esc(f) for f in h.get("findings", [])) or "—"
        parts.append(
            f'<tr><td>{esc(h.get("name", ""))}</td>'
            f'<td>{esc(shipped)}</td>'
            f'<td>{esc(h.get("alpha", ""))}</td>'
            f'<td>{findings}</td></tr>'
        )
    parts.append('</table></body></html>')
    return "".join(parts)


def _human_summary(audit: Dict) -> str:
    lines: List[str] = []
    for a in audit.get("harnesses", []):
        status = "✓" if a.get("shipped") else "✗"
        lines.append(
            f"  {status} {a.get('name', ''):<11} "
            f"alpha={(a.get('alpha') or '—'):<13} "
            f"resources={a.get('resource_count', 0)}/{a.get('resource_expected', 0)} "
            f"rubrics={a.get('rubric_count', 0)}/{a.get('rubric_expected', 0)}"
        )
        for f in a.get("findings", []):
            lines.append(f"      - {f}")
    s = audit.get("summary", {})
    lines.append(
        f"\nSummary: {s.get('shipped', 0)}/{s.get('total', 0)} harnesses shipped, "
        f"{s.get('findings', 0)} findings"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit dev-kit harnesses (Phase 7)")
    parser.add_argument("--project-root", default=".",
                        help="project root to audit (default: cwd)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true",
                      help="emit machine-readable JSON to stdout (agent)")
    mode.add_argument("--html-out", metavar="PATH",
                      help="write HTML report to PATH (overrides default HTML path)")
    mode.add_argument("--text", action="store_true",
                      help="print brief text summary to stdout (for log files)")
    return parser


DEFAULT_HTML_PATH = Path(".dev-kit") / "harness-audit-report.html"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    audit = run_audit(project_root)
    if args.json:
        print(to_json(audit))
    elif args.html_out:
        out = Path(args.html_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(to_html(audit), encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    elif args.text:
        print(_human_summary(audit))
    else:
        # Phase 7.3 (#389): default is HTML for humans. The artifact
        # creation (mkdir for .dev-kit/) is NOT a state mutation —
        # state.json, state codec, phase index are all untouched.
        out = project_root / DEFAULT_HTML_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(to_html(audit), encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    s = audit.get("summary", {})
    return 0 if s.get("all_shipped") and not s.get("alpha_invalid") else 1


if __name__ == "__main__":
    sys.exit(main())
