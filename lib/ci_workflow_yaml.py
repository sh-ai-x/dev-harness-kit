"""ci_workflow_yaml.py — hand-rolled GitHub Actions workflow YAML parser.

Pulled out of `lib/ci_doctor.py` so the per-workflow diagnostic checks
in `ci_doctor` could shrink to their orchestrator + one row-emitter
each. Pure stdlib; no PyYAML. We only need enough surface to emit
WARN/INFO diagnostics — never to execute the workflow. Out of scope:
`steps:` bodies, multi-line `|`/`>` scalars, anchors.

Public surface:
  JobShape         : per-job shape (key, name, if_expr)
  WorkflowShape    : per-workflow shape (triggers, pr_paths,
                     pr_branches, concurrency_cancel, jobs, uses,
                     parse_error)
  parse_workflow    : text -> WorkflowShape (never raises)
  read_workflow    : (target, rel) -> (raw, shape, error_msg)
  PR_FAMILY_TRIGGERS, FIRST_PARTY_ACTION_PREFIXES, FORK_GUARD_RE,
  has_fork_guard : small constants / helpers used by ci_doctor checks
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Triggers that would let the workflow see a PR event in some form.
PR_FAMILY_TRIGGERS: frozenset[str] = frozenset({
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "workflow_run",
})

# First-party `uses:` prefixes we never flag for SHA-pinning (they're
# owned by GitHub and expected to track tags).
FIRST_PARTY_ACTION_PREFIXES: tuple[str, ...] = (
    "actions/checkout",
    "actions/setup-",
    "actions/cache",
    "actions/upload-artifact",
    "actions/download-artifact",
)

# Fork-safety guard: a job/step `if:` that restricts execution to
# same-repo PRs (`head.repo.full_name == github.repository`). When
# present, fork PRs are skipped *before* any secret-consuming step runs,
# so `pull_request` (not `pull_request_target`) is safe — the "fork PRs
# lose secrets" concern never materializes because forks don't run at
# all. Matched in either operand order.
FORK_GUARD_RE = re.compile(
    r"head\.repo\.full_name\s*==\s*github\.repository"
    r"|github\.repository\s*==\s*[\w.]*head\.repo\.full_name"
)


@dataclass
class JobShape:
    """Per-job extracted shape from a workflow file."""
    key: str
    name: str | None = None
    if_expr: str | None = None


@dataclass
class WorkflowShape:
    """Per-workflow extracted shape. `parse_error` is set (non-empty)
    iff the scanner gave up on something — callers emit an INFO row
    rather than failing the audit."""
    triggers: set[str] = field(default_factory=set)
    pr_paths: list[str] = field(default_factory=list)
    pr_branches: list[str] = field(default_factory=list)
    concurrency_cancel: bool = False
    jobs: list[JobShape] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    parse_error: str = ""


def _next_top_level(lines: list[str], start: int) -> int:
    """Return the index of the next column-0 (top-level) line at or after
    `start`. If none, returns `len(lines)`. Used to delimit YAML blocks
    without an indent stack."""
    for i in range(start, len(lines)):
        ln = lines[i]
        if not ln:
            continue
        if ln.startswith(" ") or ln.startswith("\t"):
            continue
        return i
    return len(lines)


def _strip_yaml_scalar(raw: str) -> str:
    """Strip a YAML scalar (single-line) to its value. Handles
    unquoted, single-quoted, and double-quoted forms. Trailing
    comments are removed first.
    """
    s = raw.strip()
    # Drop trailing inline comment (cheap: only `#` outside quotes).
    if "#" in s:
        out, in_sq, in_dq = [], False, False
        for ch in s:
            if ch == "'" and not in_dq:
                in_sq = not in_sq
            elif ch == '"' and not in_sq:
                in_dq = not in_dq
            elif ch == "#" and not in_sq and not in_dq:
                break
            out.append(ch)
        s = "".join(out).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.strip()


def parse_workflow(text: str) -> WorkflowShape:
    """Hand-parse a GitHub Actions workflow YAML for diagnostic checks.

    Pure stdlib. On any anomaly (tab indent, folded scalars, anchors),
    sets `shape.parse_error` and returns what it has. Never raises.
    """
    shape = WorkflowShape()
    if not text or not text.strip():
        shape.parse_error = "empty file"
        return shape

    lines = text.splitlines()
    if any(ln.startswith("\t") for ln in lines):
        shape.parse_error = "tab indentation not supported by hand-parser"
        return shape

    # 1. Find the `on:` block (bare or quoted-key form).
    on_idx = -1
    on_inline_value = None
    on_flow_list: list[str] = []
    for i, ln in enumerate(lines):
        m = re.match(r"""^\s*("?on"?|on)\s*:\s*(.*)$""", ln)
        if not m:
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent != 0:
            continue
        on_idx = i
        rest = m.group(2).strip()
        if rest and not rest.startswith("#"):
            v = _strip_yaml_scalar(rest)
            if re.fullmatch(r"[a-z_][a-z0-9_-]*", v):
                on_inline_value = v
            elif v.startswith("[") and v.endswith("]"):
                inner = v[1:-1]
                for tok in inner.split(","):
                    tok = tok.strip().strip('"').strip("'")
                    if re.fullmatch(r"[a-z_][a-z0-9_-]*", tok):
                        on_flow_list.append(tok)
        break

    if on_idx == -1:
        shape.parse_error = "no top-level 'on:' / '\"on\":' block"

    on_block_end = _next_top_level(lines, on_idx + 1) if on_idx != -1 else 0

    # 2. Populate triggers from `on:` block body.
    if on_inline_value:
        shape.triggers.add(on_inline_value)
    for ft in on_flow_list:
        shape.triggers.add(ft)
    if on_idx != -1 and on_block_end > on_idx + 1:
        body = lines[on_idx + 1: on_block_end]
        body_indents = []
        for ln in body:
            stripped = ln.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            body_indents.append(len(ln) - len(stripped))
        base_indent = min(body_indents) if body_indents else 2
        list_re = re.compile(rf"^\s{{2,{base_indent + 4}}}-\s+([a-z_][a-z0-9_-]*)\s*(?:#.*)?$")
        for ln in body:
            m = list_re.match(ln)
            if m:
                shape.triggers.add(m.group(1))
        map_re = re.compile(
            rf"^\s{{2,{base_indent + 4}}}(pull_request(?:_target|_review)?|workflow_run|workflow_dispatch|push|schedule)\s*:",
        )
        for ln in body:
            m = map_re.match(ln)
            if m:
                shape.triggers.add(m.group(1).rstrip(":"))

    # 3. Pull `paths:` / `paths-ignore:` / `branches:` under pull_request*.
    if on_idx != -1:
        pr_starts: list[tuple[int, int]] = []
        for i in range(on_idx + 1, on_block_end):
            ln = lines[i]
            m = re.match(
                r"^(\s+)(pull_request(?:_target|_review)?)\s*:",
                ln,
            )
            if m:
                pr_starts.append((i, len(m.group(1))))
        for pr_start, pr_indent in pr_starts:
            j = pr_start + 1
            while j < on_block_end:
                ln = lines[j]
                stripped = ln.lstrip()
                if not stripped or stripped.startswith("#"):
                    j += 1
                    continue
                cur_indent = len(ln) - len(stripped)
                if cur_indent <= pr_indent:
                    break
                m_key = re.match(
                    r"^\s+(paths(?:-ignore)?|branches)\s*:\s*(.*)$",
                    ln,
                )
                if not m_key:
                    j += 1
                    continue
                key = m_key.group(1)
                inline = m_key.group(2).strip()
                if inline and not inline.startswith("#"):
                    val = _strip_yaml_scalar(inline)
                    if val:
                        if key.startswith("paths"):
                            shape.pr_paths.append(val)
                        else:
                            shape.pr_branches.append(val)
                    j += 1
                    continue
                child_indent = cur_indent
                k = j + 1
                child_vals: list[str] = []
                while k < on_block_end:
                    child_ln = lines[k]
                    child_stripped = child_ln.lstrip()
                    if not child_stripped or child_stripped.startswith("#"):
                        k += 1
                        continue
                    child_cur_indent = len(child_ln) - len(child_stripped)
                    if child_cur_indent <= child_indent:
                        break
                    m_flow = re.match(r"^\s+-\s+\[(.+?)\]\s*(?:#.*)?$", child_ln)
                    if m_flow:
                        inner = m_flow.group(1)
                        for tok in inner.split(","):
                            tok = _strip_yaml_scalar(tok)
                            if tok:
                                child_vals.append(tok)
                        k += 1
                        continue
                    m_item = re.match(
                        r"^\s+-\s+(.+?)\s*(?:#.*)?$", child_ln,
                    )
                    if m_item:
                        child_vals.append(_strip_yaml_scalar(m_item.group(1)))
                    k += 1
                for v in child_vals:
                    if key.startswith("paths"):
                        shape.pr_paths.append(v)
                    else:
                        shape.pr_branches.append(v)
                j = k

    # 4. Top-level `concurrency:` `cancel-in-progress`.
    conc_idx = -1
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*concurrency\s*:", ln)
        if m and (len(ln) - len(ln.lstrip())) == 0:
            conc_idx = i
            break
    if conc_idx != -1:
        conc_end = _next_top_level(lines, conc_idx + 1)
        for ln in lines[conc_idx + 1: conc_end]:
            m = re.match(r"^\s+cancel-in-progress\s*:\s*(true|false)\s*(?:#.*)?$", ln)
            if m and m.group(1) == "true":
                shape.concurrency_cancel = True
                break

    # 5. Jobs: key (indent 2) + name + if (indent 4+).
    jobs_idx = -1
    for i, ln in enumerate(lines):
        if re.match(r"^jobs\s*:\s*(?:#.*)?$", ln):
            jobs_idx = i
            break
    if jobs_idx != -1:
        job_starts: list[tuple[int, str]] = []
        for i in range(jobs_idx + 1, len(lines)):
            ln = lines[i]
            if not ln:
                continue
            stripped = ln.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            cur_indent = len(ln) - len(stripped)
            if cur_indent == 0:
                break
            m = re.match(r"^\s{2}([a-z][a-z0-9_-]*)\s*:\s*(?:#.*)?$", ln)
            if m:
                job_starts.append((i, m.group(1)))
        for idx, (start, key) in enumerate(job_starts):
            end = job_starts[idx + 1][0] if idx + 1 < len(job_starts) else len(lines)
            j = JobShape(key=key)
            for ln in lines[start: end]:
                mn = re.match(r"^\s{4,}name\s*:\s*(.+?)\s*(?:#.*)?$", ln)
                if mn and not j.name:
                    j.name = _strip_yaml_scalar(mn.group(1))
                mi = re.match(r"^\s{4,}if\s*:\s*(.+?)\s*(?:#.*)?$", ln)
                if mi and not j.if_expr:
                    j.if_expr = _strip_yaml_scalar(mi.group(1))
            shape.jobs.append(j)

    # 6. `uses:` refs (for SHA-pin check).
    for ln in lines:
        mu = re.match(r"^\s*-?\s*uses\s*:\s*(\S+)\s*(?:#.*)?$", ln)
        if mu:
            shape.uses.append(mu.group(1))

    return shape


def has_fork_guard(raw: str) -> bool:
    """True iff the workflow text contains a same-repo fork guard that
    skips fork PRs before any step executes."""
    return bool(FORK_GUARD_RE.search(raw))


def read_workflow(target: Path, rel: str) -> tuple[str | None, WorkflowShape | None, str]:
    """Read a workflow file relative to `target`. Returns
    `(raw_text, shape_or_None, error_msg)`. Caller decides state.
    """
    p = target / rel
    if not p.is_file():
        return None, None, "file missing"
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, None, f"read error: {e}"
    return text, parse_workflow(text), ""
