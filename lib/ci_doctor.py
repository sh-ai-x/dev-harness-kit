"""ci_doctor.py — Read-only CI readiness audit for `/dev-kit:ci-doctor`.

Issue #212-D1: after `/dev-kit:bootstrap + ci-setup`, a consumer has no way to
answer "given my current secrets + files + workflow templates, would the
CI on my next PR succeed?" This module answers that question with a
deterministic, read-only check suite. Every probe is side-effect-free:
no files mutated, no secrets written, no PRs opened.

Engine for the `/dev-kit:ci-doctor` skill. Pure stdlib, no external
deps. Returns a `DoctorReport` dataclass; the skill body renders the
PASS/FAIL summary.

Usage:
    from lib.ci_doctor import audit
    report = audit(target_dir=Path('/path/to/repo'))
    print(report.summary_lines())

Public surface:
    audit(target_dir, *, provider=None) -> DoctorReport
    DoctorReport          # dataclass with `checks` + `summary_lines()`
    Check                  # one row of the audit table
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Dual-mode import for sibling ci_setup.py:
#   * Source repo (this module loaded as `lib.ci_doctor`): the relative
#     `from .ci_setup import` resolves inside the `lib` package.
#   * Test harness / consumer invocation (this module loaded as a top-level
#     module via importlib.util.spec_from_file_location, with `lib/` on
#     sys.path): the absolute `from ci_setup import` resolves directly.
try:
    from .ci_setup import (  # type: ignore  # noqa: E402
        PROVIDER_SECRETS,
        detect_owner_repo,
        gh_secret_set_command,
        read_env_key,
        read_provider,
        required_secrets_for_provider,
    )
except ImportError:
    from ci_setup import (  # type: ignore  # noqa: E402
        PROVIDER_SECRETS,
        detect_owner_repo,
        gh_secret_set_command,
        read_env_key,
        read_provider,
        required_secrets_for_provider,
    )

try:
    from .ci_update import diff_ci_install  # type: ignore  # noqa: E402
except ImportError:
    try:
        from ci_update import diff_ci_install  # type: ignore  # noqa: E402
    except ImportError:
        # ci_update may not be installed in the source-repo checkout
        # (the plugin is its own dev environment; tests still run). The
        # check returns SKIP in that case so ci-doctor stays usable.
        diff_ci_install = None  # type: ignore


@dataclass
class Check:
    """One row of the audit table.

    `state` is one of:
      - PASS : check passed (marker present, secret configured, etc.)
      - FAIL : check failed (file missing, secret absent, gh not authed)
      - SKIP : check could not run (gh absent, repo context missing)
      - INFO : informational only (e.g. opt-in secret absent)
      - WARN : visible root-cause diagnostic (workflow trigger gap,
              fork-PR secret leak, cancel-in-progress, branch-policy
              required-check mismatch) — never flips the verdict
    """

    label: str
    state: str
    detail: str = ""

    def row(self) -> str:
        tag = self.state
        return f"[{tag:<4}] {self.label}: {self.detail}".rstrip()


@dataclass
class DoctorReport:
    """Aggregate audit result.

    `checks` is the ordered list of `Check` rows. `ok` is True iff no
    FAIL row was recorded. SKIP, INFO, and WARN rows are advisory and
    do NOT flip `ok` — they surface root causes (missing PR triggers,
    unparseable YAML, branch-policy mismatches) without turning a
    partially-installed repo into a hard fail.
    """

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
        lines: list[str] = []
        verdict = "PASS" if self.ok else "FAIL"
        fail_count = len(self.failing())
        skip_count = sum(1 for c in self.checks if c.state == "SKIP")
        warn_count = len(self.warnings())
        lines.append(f"ci-doctor verdict: {verdict}")
        lines.append(
            f"  checks: {len(self.checks)}  "
            f"failing: {fail_count}  "
            f"skipped: {skip_count}  "
            f"warnings: {warn_count}"
        )
        for c in self.checks:
            lines.append(f"  {c.row()}")
        return lines


# Files the install MUST leave behind (subset of ci_setup.EXPECTED_PATHS
# that gates next-PR viability). Workflows + marker (provider selector is
# env-based, not a file — see `_check_provider_declared`).
REQUIRED_FILES: tuple[str, ...] = (
    ".github/workflows/ci.yml",
    ".github/workflows/review.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".dev-kit/ci-config.json",
)


# Consumer-only artifacts: present after a `ci-setup` install into a
# downstream repo, but intentionally absent (or gitignored) in the dev-kit
# plugin authoring source itself. In the source repo these are SKIP, not FAIL.
CONSUMER_ONLY_FILES: frozenset[str] = frozenset({".dev-kit/ci-config.json"})
CONSUMER_ONLY_SECRETS: frozenset[str] = frozenset({"DEV_KIT_GITHUB_TOKEN"})


# Diagnostic trigger expectations per workflow. The audit emits a WARN
# row when a workflow that should run on PRs lacks any PR-family trigger
# (`pull_request`, `pull_request_target`, `workflow_run`,
# `pull_request_review`).
EXPECTED_PR_TRIGGERS: dict[str, frozenset[str]] = {
    "review.yml": frozenset({"pull_request", "pull_request_target", "workflow_run"}),
    "auto-fix-pr.yml": frozenset({"pull_request_review"}),
    "ci.yml": frozenset({"pull_request", "push"}),
}

# Workflow files we hand-parse. Order matches the install manifest.
WORKFLOW_FILES: tuple[str, ...] = (
    ".github/workflows/review.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/ci.yml",
)


# ---- Workflow YAML hand-parser ------------------------------------------
# Pure stdlib; no PyYAML. We only need enough surface to emit WARN/INFO
# diagnostics — never to execute the workflow. Out of scope: `steps:`,
# `with:`/`run:` bodies, multi-line `|`/`>` scalars, anchors.

@dataclass
class _JobShape:
    """Per-job extracted shape from a workflow file."""
    key: str
    name: str | None = None
    if_expr: str | None = None


@dataclass
class _WorkflowShape:
    """Per-workflow extracted shape. `parse_error` is set (non-empty)
    iff the scanner gave up on something — callers emit an INFO row
    rather than failing the audit."""
    triggers: set[str] = field(default_factory=set)
    pr_paths: list[str] = field(default_factory=list)
    pr_branches: list[str] = field(default_factory=list)
    concurrency_cancel: bool = False
    jobs: list[_JobShape] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    parse_error: str = ""


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
        # Walk, skip quoted regions.
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


def _parse_workflow_yaml(text: str) -> _WorkflowShape:
    """Hand-parse a GitHub Actions workflow YAML for diagnostic checks.

    Pure stdlib. On any anomaly (tab indent, folded scalars, anchors),
    sets `shape.parse_error` and returns what it has. Never raises.
    """
    shape = _WorkflowShape()
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
        # Skip nested matches like `permission: read` — `on:` is a
        # top-level key, but we tolerate indented `on:` only as a
        # top-level check.
        indent = len(ln) - len(ln.lstrip())
        if indent != 0:
            continue
        on_idx = i
        rest = m.group(2).strip()
        if rest and not rest.startswith("#"):
            v = _strip_yaml_scalar(rest)
            # Single-string trigger: `on: push`
            if re.fullmatch(r"[a-z_][a-z0-9_-]*", v):
                on_inline_value = v
            # Flow-style list: `on: [push, pull_request]`
            elif v.startswith("[") and v.endswith("]"):
                inner = v[1:-1]
                for tok in inner.split(","):
                    tok = tok.strip().strip('"').strip("'")
                    if re.fullmatch(r"[a-z_][a-z0-9_-]*", tok):
                        on_flow_list.append(tok)
        break

    if on_idx == -1:
        shape.parse_error = "no top-level 'on:' / '\"on\":' block"
        # Fall through so we can still try to pick up jobs / uses / etc.

    on_block_end = _next_top_level(lines, on_idx + 1) if on_idx != -1 else 0

    # 2. Populate triggers from `on:` block body.
    if on_inline_value:
        shape.triggers.add(on_inline_value)
    for ft in on_flow_list:
        shape.triggers.add(ft)
    if on_idx != -1 and on_block_end > on_idx + 1:
        body = lines[on_idx + 1: on_block_end]
        # Determine the immediate-child indent (the indent at which
        # `on:`'s direct children sit). Restrict list-item / mapping-key
        # scans to that indent so we don't pick up deeply-nested values
        # (e.g. `workflow_dispatch.inputs.review_provider.options: -
        # minimax`) as bogus triggers.
        body_indents = []
        for ln in body:
            stripped = ln.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            body_indents.append(len(ln) - len(stripped))
        base_indent = min(body_indents) if body_indents else 2
        # Allow up to +4 deeper than the immediate child for nested
        # triggers like `pull_request:` mapping children, but NOT for
        # list-style trigger detection (which must be the direct child).
        direct_child_max = base_indent  # exactly the base
        # List-style: `  - push` (each on its own line, at the direct
        # child indent). Indented-4 limit avoids nested options lists.
        list_re = re.compile(rf"^\s{{2,{direct_child_max + 4}}}-\s+([a-z_][a-z0-9_-]*)\s*(?:#.*)?$")
        for ln in body:
            m = list_re.match(ln)
            if m:
                shape.triggers.add(m.group(1))
        # Mapping-style: `  pull_request:` at the direct child indent.
        map_re = re.compile(
            rf"^\s{{2,{direct_child_max + 4}}}(pull_request(?:_target|_review)?|workflow_run|workflow_dispatch|push|schedule)\s*:",
        )
        for ln in body:
            m = map_re.match(ln)
            if m:
                shape.triggers.add(m.group(1).rstrip(":"))

    # 3. Pull `paths:` / `paths-ignore:` / `branches:` under pull_request*.
    if on_idx != -1:
        # Find PR block boundaries within the trigger block.
        pr_starts: list[tuple[int, int]] = []  # (start_line, indent)
        for i in range(on_idx + 1, on_block_end):
            ln = lines[i]
            m = re.match(
                r"^(\s+)(pull_request(?:_target|_review)?)\s*:",
                ln,
            )
            if m:
                pr_starts.append((i, len(m.group(1))))
        # For each PR block, walk children to find `paths:` / `branches:`.
        # Handle both inline (`branches: [main]`) and block-style
        # (`branches:\n  - main`).
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
                    break  # Exited the PR block.
                # Match the filter key (inline value OR block header).
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
                # Block-style: walk child list items until we exit this
                # filter key's indent.
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
                        break  # Exited the filter block.
                    # Inline flow list on a single child line: `- [a, b]`
                    m_flow = re.match(r"^\s+-\s+\[(.+?)\]\s*(?:#.*)?$", child_ln)
                    if m_flow:
                        inner = m_flow.group(1)
                        for tok in inner.split(","):
                            tok = _strip_yaml_scalar(tok)
                            if tok:
                                child_vals.append(tok)
                        k += 1
                        continue
                    # Standard list item: `- foo`
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
                break  # Exited `jobs:` block.
            m = re.match(r"^\s{2}([a-z][a-z0-9_-]*)\s*:\s*(?:#.*)?$", ln)
            if m:
                job_starts.append((i, m.group(1)))
        for idx, (start, key) in enumerate(job_starts):
            end = job_starts[idx + 1][0] if idx + 1 < len(job_starts) else len(lines)
            j = _JobShape(key=key)
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


def _is_source_repo(target_dir: Path) -> bool:
    """True iff `target_dir` is the dev-kit plugin authoring source.

    Signal: `.claude-plugin/plugin.json` whose `name` is `dev-kit`. A
    consumer that installed CI via `/dev-kit:bootstrap + ci-setup` never authors
    this manifest, so its presence uniquely identifies the source repo.
    Consumer-only artifacts (the `.dev-kit/ci-config.json` build marker,
    the `DEV_KIT_GITHUB_TOKEN` PAT) are not applicable here — the source
    repo's own CI uses the default `GITHUB_TOKEN` and needs no marker.
    """
    manifest = target_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("name") == "dev-kit"


def _detect_owner_repo(target_dir: Path) -> str:
    """Re-export ci_setup.detect_owner_repo for backwards compat (issue #238).

    ci_setup.detect_owner_repo returns a `<OWNER>/<REPO>` placeholder with
    an error suffix when auto-detect fails; ci_doctor callers feed this
    into `gh secret set --repo <OWNER>/<REPO>` which would emit a broken
    command, so strip the placeholder back to empty.
    """
    result = detect_owner_repo(target_dir)
    return "" if result.startswith("<OWNER>/<REPO>") else result


def _list_repo_secrets(repo: str) -> tuple[set[str], str]:
    """Return (set-of-secret-names, degraded-message).

    When gh is absent or unauthenticated, returns an empty set and a
    non-empty degraded message; the caller should surface that as a SKIP
    rather than a FAIL (the user might just not be running this locally
    with gh auth).
    """
    gh = shutil.which("gh")
    if not gh:
        return set(), "gh not on PATH"
    try:
        cp = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return set(), "gh not authenticated"
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return set(), f"gh auth error: {e}"
    try:
        cp = subprocess.run(
            [gh, "secret", "list", "--repo", repo, "--json", "name"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            err = (cp.stderr or "").strip().splitlines()[-1] if cp.stderr else ""
            return set(), f"gh secret list failed: {err or 'unknown'}"
        names = {row.get("name", "") for row in json.loads(cp.stdout)}
        return names, ""
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        return set(), f"gh secret list error: {e}"


def _check_required_files(target: Path, source_repo: bool = False) -> list[Check]:
    out: list[Check] = []
    for rel in REQUIRED_FILES:
        p = target / rel
        if p.is_file():
            size = p.stat().st_size
            out.append(Check(f"file present: {rel}", "PASS", f"{size} bytes"))
        elif source_repo and rel in CONSUMER_ONLY_FILES:
            out.append(Check(f"file present: {rel}", "SKIP",
                             "source repo: consumer marker not applicable"))
        else:
            out.append(Check(f"file present: {rel}", "FAIL", "missing"))
    return out


def _check_marker_payload(target: Path, source_repo: bool = False) -> list[Check]:
    marker = target / ".dev-kit" / "ci-config.json"
    if not marker.is_file():
        if source_repo:
            return [Check("marker parseable", "SKIP",
                          "source repo: consumer marker not applicable")]
        return [Check("marker parseable", "FAIL", ".dev-kit/ci-config.json missing")]
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [Check("marker parseable", "FAIL", f"parse error: {e}")]
    out = [Check("marker parseable", "PASS", "JSON ok")]
    if not isinstance(payload, dict) or not payload:
        out.append(Check("marker non-empty", "FAIL", "empty payload"))
    else:
        out.append(Check("marker non-empty", "PASS", f"{len(payload)} keys"))
    if payload.get("provider_env_key") != "CI_REVIEW_PROVIDER":
        out.append(Check(
            "marker records provider key", "FAIL",
            "expected `provider_env_key: CI_REVIEW_PROVIDER`",
        ))
    else:
        out.append(Check("marker records provider key", "PASS", ""))
    return out


def _check_templates_current(target: Path, source_repo: bool = False) -> list[Check]:
    """Compare the consumer's installed CI templates against the live dev-kit source.

    Closes the dev-kit ⇄ consumer drift gap that motivated `/dev-kit:ci-update`:
    a consumer who ran ci-setup at an older dev-kit version gets a single
    PASS/INFO/WARN/SKIP line that summarizes how their installed templates
    have diverged from the plugin's current source.

    State mapping:
      - PASS  — every EXPECTED_PATHS file at target matches the live dev-kit
                source AND the marker's recorded version is current.
      - INFO  — drift exists but only NEW files (dev-kit added templates
                the consumer never installed). Actionable, not blocking.
      - WARN  — consumer has modified installed files locally, OR a file
                that dev-kit changed is at the consumer unchanged (consumer
                is stale). Actionable; surface in the report.
      - SKIP  — marker lacks `installed_dev_kit_version` (predates schema),
                ci_update module not available, or source_repo mode.
      - FAIL  — diff engine raised (template source unreadable).

    In source-repo mode (dev-kit running on itself) the check is a no-op:
    the source-of-truth and consumer-side are the same tree, so any diff
    is a self-comparison artefact.
    """
    if source_repo:
        return [Check(
            "templates current", "SKIP",
            "source repo: self-comparison not meaningful",
        )]
    if diff_ci_install is None:
        return [Check(
            "templates current", "SKIP",
            "ci_update module not importable from this environment",
        )]
    marker_path = target / ".dev-kit" / "ci-config.json"
    if not marker_path.is_file():
        return [Check(
            "templates current", "SKIP",
            "no .dev-kit/ci-config.json marker",
        )]
    try:
        import json as _json
        marker = _json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        return [Check("templates current", "FAIL", f"marker parse error: {e}")]
    installed_version = marker.get("installed_dev_kit_version", "")
    if not installed_version or installed_version == "unknown":
        return [Check(
            "templates current", "SKIP",
            "marker lacks installed_dev_kit_version; run /dev-kit:ci-setup to backfill",
        )]
    try:
        # `diff_ci_install` reads `plugin_version` from `_PLUGIN_ROOT` by
        # default. In source-repo mode that resolves to the same tree
        # we're comparing against, so we already returned above.
        report = diff_ci_install(target)
    except Exception as e:
        return [Check("templates current", "FAIL", f"diff engine error: {e}")]
    n_new = len(report.new)
    n_updated = len(report.updated)
    n_consumer_modified = len(report.consumer_modified)
    n_diverged = len(report.diverged)
    detail = (
        f"installed={installed_version}; "
        f"new={n_new} updated={n_updated} "
        f"consumer_modified={n_consumer_modified} diverged={n_diverged}"
    )
    if n_consumer_modified == 0 and n_diverged == 0 and n_updated == 0 and n_new == 0:
        return [Check("templates current", "PASS", detail)]
    if n_consumer_modified == 0 and n_diverged == 0:
        # Drift exists but only NEW + UPDATED — no consumer-side friction.
        # Informational; /dev-kit:ci-update can refresh.
        return [Check("templates current", "INFO", detail)]
    return [Check("templates current", "WARN", detail)]


def _check_provider_declared(target: Path) -> list[Check]:
    """Confirm the provider is declared in env, .env, or .env.example.

    Provider selection is now env-based (no committed file). The check
    reports where the value came from so ci-doctor output is actionable
    for operators on different sides of the local/CI boundary.
    """
    env_val = os.environ.get("CI_REVIEW_PROVIDER", "").strip().lower()
    env_file_val = ""
    if (target / ".env").is_file():
        env_file_val = read_env_key(target / ".env", "CI_REVIEW_PROVIDER").lower()
    example_val = ""
    if (target / ".env.example").is_file():
        example_val = read_env_key(target / ".env.example", "CI_REVIEW_PROVIDER").lower()

    resolved = next(
        (v for v in (env_val, env_file_val, example_val) if v in PROVIDER_SECRETS),
        "",
    )
    if not resolved:
        return [Check(
            "provider declared", "FAIL",
            "CI_REVIEW_PROVIDER not set in process env, .env, or .env.example",
        )]
    source = (
        "process env" if env_val == resolved
        else ".env" if env_file_val == resolved
        else ".env.example"
    )
    return [Check("provider declared", "PASS", f"{resolved} (via {source})")]


def _check_secrets(target: Path, provider: str | None,
                   source_repo: bool = False) -> list[Check]:
    repo = _detect_owner_repo(target)
    if not repo:
        return [Check("repo context", "SKIP", "no GitHub remote on origin")]
    secrets, degraded = _list_repo_secrets(repo)
    if degraded:
        return [Check("repo secrets", "SKIP", degraded)]
    provider = provider or read_provider(target)
    needed = required_secrets_for_provider(provider)
    out: list[Check] = []
    for name in needed:
        if source_repo and name in CONSUMER_ONLY_SECRETS:
            out.append(Check(f"secret set: {name}", "SKIP",
                             "source repo: PAT not required"))
        elif name in secrets:
            out.append(Check(f"secret set: {name}", "PASS", ""))
        else:
            out.append(Check(f"secret set: {name}", "FAIL",
                             f"run: {gh_secret_set_command(repo, name)}"))
    return out


def _check_gh_auth() -> Check:
    gh = shutil.which("gh")
    if not gh:
        return Check("gh CLI", "SKIP", "gh not on PATH")
    try:
        cp = subprocess.run(
            [gh, "auth", "status"], capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return Check("gh auth", "FAIL", f"gh auth error: {e}")
    return Check(
        "gh auth", "PASS" if cp.returncode == 0 else "FAIL",
        (cp.stderr or "").strip() if cp.returncode != 0 else "",
    )


# ---- Workflow diagnostics (WARN/INFO only — verdict-neutral) ----------
# These checks surface *why* `ci github action review` might fail to run
# even when the install is otherwise clean. Per the user's directive
# ("리뷰 과정은 유연하지만 그외에는 어디서 문제가 있는지 원인을 찾는것"):
# they never emit FAIL and never flip the verdict. A row only appears
# when its signal is present.

def _read_workflow(target: Path, rel: str) -> tuple[str | None, _WorkflowShape | None, str]:
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
    return text, _parse_workflow_yaml(text), ""


def _trigger_check(path: Path, expected: frozenset[str]) -> Check:
    """WARN if no PR-family trigger is present on a workflow that should
    see PR events. PASS if at least one PR-family trigger is present.
    INFO if the file is missing or unparseable (we can't introspect)."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None:
        return Check(f"workflow triggers: {rel}", "INFO", err)
    assert shape is not None
    if shape.parse_error:
        return Check(f"workflow triggers: {rel}", "INFO",
                     f"could not parse: {shape.parse_error}")
    pr_family = shape.triggers & PR_FAMILY_TRIGGERS
    if not pr_family:
        return Check(
            f"workflow triggers: {rel}", "WARN",
            f"no PR-family trigger; expected one of {sorted(expected)}; "
            f"got {sorted(shape.triggers) or 'none'}",
        )
    return Check(f"workflow triggers: {rel}", "PASS",
                 f"triggers={sorted(shape.triggers)}")


# Fork-safety guard: a job/step `if:` that restricts execution to
# same-repo PRs (`head.repo.full_name == github.repository`). When
# present, fork PRs are skipped *before* any secret-consuming step runs,
# so `pull_request` (not `pull_request_target`) is safe — the "fork PRs
# lose secrets" concern never materializes because forks don't run at
# all. The dev-kit consumer template (templates/ci/.github/workflows/
# review.yml) ships this guard precisely so it can keep `pull_request`
# and avoid the OIDC-401 failure `pull_request_target` causes in consumer
# repos without org-level OIDC trust. Matched in either operand order.
_FORK_GUARD_RE = re.compile(
    r"head\.repo\.full_name\s*==\s*github\.repository"
    r"|github\.repository\s*==\s*[\w.]*head\.repo\.full_name"
)


def _has_fork_guard(raw: str) -> bool:
    """True iff the workflow text contains a same-repo fork guard that
    skips fork PRs before any step executes."""
    return bool(_FORK_GUARD_RE.search(raw))


def _fork_pr_secret_gap(path: Path, source_repo: bool = False) -> Check:
    """Diagnose whether `pull_request`-only workflows leak secrets to fork
    PRs. Role- and guard-aware (issue: false-positive WARN on review.yml):

      - `pull_request_target:` / `workflow_run:` present  → PASS (secrets
        reach the run in a fork-safe context).
      - `pull_request`-only **with** a same-repo fork guard → PASS: forks
        are skipped before any step, so there is no gap. This is the
        intended shape of the consumer review.yml template.
      - `pull_request`-only, no guard, **source repo** → INFO: the dev-kit
        source repo takes internal-branch PRs only; fork gap is N/A.
      - `pull_request`-only, no guard, consumer repo → WARN (real gap).
    """
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None:
        return Check(f"fork-PR secret gap: {rel}", "INFO", err)
    assert shape is not None
    if shape.parse_error:
        return Check(f"fork-PR secret gap: {rel}", "INFO",
                     f"could not parse: {shape.parse_error}")
    pr = "pull_request" in shape.triggers
    tgt = "pull_request_target" in shape.triggers
    wr = "workflow_run" in shape.triggers
    # Not a pull_request-only workflow → the trigger itself is fork-safe.
    if not (pr and not (tgt or wr)):
        return Check(
            f"fork-PR secret gap: {rel}", "PASS",
            f"pull_request={'y' if pr else 'n'}  "
            f"pull_request_target={'y' if tgt else 'n'}  "
            f"workflow_run={'y' if wr else 'n'}",
        )
    # pull_request-only: a same-repo guard makes it fork-safe regardless.
    if _has_fork_guard(raw):
        return Check(
            f"fork-PR secret gap: {rel}", "PASS",
            "pull_request only, but fork PRs are skipped by a same-repo "
            "guard (head.repo.full_name == github.repository)",
        )
    # No guard: the source repo takes internal-branch PRs only, so the
    # gap is N/A there; a consumer repo has a real gap.
    if source_repo:
        return Check(
            f"fork-PR secret gap: {rel}", "INFO",
            "source repo: internal-branch PRs only — fork gap N/A",
        )
    return Check(
        f"fork-PR secret gap: {rel}", "WARN",
        "uses pull_request; no pull_request_target / workflow_run and no "
        "same-repo fork guard — fork PRs lose repo secrets",
    )


def _paths_filter_check(path: Path) -> Check | None:
    """INFO row listing any `paths:`/`paths-ignore:` filter on
    pull_request* so the user can verify it covers their changes."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None or shape is None or shape.parse_error or not shape.pr_paths:
        return None
    return Check(
        f"paths filter: {rel}", "INFO",
        f"pull_request paths={shape.pr_paths}  "
        "(verify it includes your changes)",
    )


def _branches_filter_check(path: Path) -> Check | None:
    """INFO row listing any `branches:` filter on pull_request* so the
    user can verify the PR's target branch matches."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None or shape is None or shape.parse_error or not shape.pr_branches:
        return None
    return Check(
        f"branches filter: {rel}", "INFO",
        f"pull_request branches={shape.pr_branches}  "
        "(verify your PR's target branch is in this list)",
    )


def _concurrency_cancel_check(path: Path) -> Check:
    """WARN if top-level `concurrency: cancel-in-progress: true` — a
    mid-run cancellation could drop a long review verdict."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None:
        return Check(f"concurrency: {rel}", "INFO", err)
    assert shape is not None
    if shape.parse_error:
        return Check(f"concurrency: {rel}", "INFO",
                     f"could not parse: {shape.parse_error}")
    if shape.concurrency_cancel:
        return Check(
            f"concurrency: {rel}", "WARN",
            "cancel-in-progress=true — mid-run cancellation could drop a review",
        )
    return Check(f"concurrency: {rel}", "PASS", "ok")


def _job_if_check(path: Path) -> list[Check]:
    """INFO row per job that has an `if:` expression, verbatim."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None or shape is None or shape.parse_error:
        return []
    return [
        Check(f"job if: {rel}/{j.key}", "INFO", j.if_expr)
        for j in shape.jobs if j.if_expr
    ]


def _job_name_check(path: Path, severity_when_missing: str) -> list[Check]:
    """INFO/WARN per job missing `name:`. The job surfaces as its bare
    key in the GitHub UI; for jobs referenced by branch-protection
    required-status checks (e.g. `auto-fix`), this is a WARN."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None or shape is None or shape.parse_error:
        return []
    out: list[Check] = []
    for j in shape.jobs:
        if not j.name:
            out.append(Check(
                f"job name: {rel}/{j.key}", severity_when_missing,
                "no `name:` — surfaces as bare key in GitHub UI",
            ))
    return out


def _action_pin_check(path: Path) -> Check | None:
    """INFO row listing any third-party `uses:` ref not pinned to a
    40-char SHA. First-party prefixes are skipped."""
    rel = path.name
    raw, shape, err = _read_workflow(path.parent, rel)  # type: ignore[arg-type]
    if raw is None or shape is None or shape.parse_error or not shape.uses:
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
        f"action ref mutable: {rel}", "INFO",
        f"non-SHA refs: {sample}{suffix}  "
        "(consider pinning 3rd-party actions to a 40-char SHA for "
        "supply-chain hardening)",
    )


def _check_workflow_diagnostics(target: Path, source_repo: bool) -> list[Check]:
    """Orchestrate the per-workflow diagnostic checks. Walks the three
    workflows the install shipped; emits one WARN/INFO row per finding.
    Never returns FAIL — unparseable workflows emit INFO instead so the
    file-present PASS row (from `_check_required_files`) is preserved."""
    out: list[Check] = []
    for rel in WORKFLOW_FILES:
        path = target / rel
        if not path.is_file():
            # If the workflow isn't present at all, the install-shape FAIL
            # already fired upstream. Don't double-report here.
            continue
        base = path.name
        expected = EXPECTED_PR_TRIGGERS.get(base, frozenset())
        out.append(_trigger_check(path, expected))
        out.append(_fork_pr_secret_gap(path, source_repo))
        p = _paths_filter_check(path)
        if p is not None:
            out.append(p)
        b = _branches_filter_check(path)
        if b is not None:
            out.append(b)
        out.append(_concurrency_cancel_check(path))
        if base == "review.yml":
            out.extend(_job_if_check(path))
            # review.yml jobs already have `name:` in the shipped
            # template; an absent name is INFO, not WARN.
            out.extend(_job_name_check(path, "INFO"))
        elif base == "auto-fix-pr.yml":
            # auto-fix-pr.yml's single job has no descriptive `name:` in
            # the shipped template — surfaces as bare key in the GH UI,
            # which complicates branch-protection required-status matching.
            out.extend(_job_name_check(path, "WARN"))
        a = _action_pin_check(path)
        if a is not None:
            out.append(a)
    return out


# ---- Branch protection (single-row check) -----------------------------
# Compares GitHub branch-protection required status checks against the
# `name:` values of jobs in review.yml. WARN on mismatch, SKIP on
# absence of `gh` or repo context, INFO in source-repo mode.

def _fetch_required_status_checks(repo: str) -> tuple[set[str], str]:
    """Return ({required-check-context names}, degraded-message).

    Tries the legacy `.contexts[]` first, then `.checks[].context` for
    newer GitHub responses. On either-or-both failure, returns an empty
    set and a degraded message so the caller can SKIP rather than FAIL.
    """
    gh = shutil.which("gh")
    if not gh:
        return set(), "gh not on PATH"

    def _run(jq_expr: str) -> tuple[set[str], bool]:
        try:
            cp = subprocess.run(
                [gh, "api",
                 f"repos/{repo}/branches/main/protection/required_status_checks",
                 "--jq", jq_expr],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError):
            return set(), True
        if cp.returncode != 0:
            err = (cp.stderr or "").strip().splitlines()[-1] if cp.stderr else ""
            return set(f"gh api failed: {err or cp.returncode}"), True
        names = {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}
        return names, False

    # Legacy: top-level `.contexts[]`
    names, hard_fail = _run(".contexts[]?")
    if hard_fail:
        # Treat hard failure as degraded for either query (likely same root cause).
        return set(), names.pop() if names else "gh api failed"
    if names:
        return names, ""
    # Newer: `.checks[].context`
    names2, hard_fail2 = _run(".checks[].context")
    if hard_fail2:
        return set(), names2.pop() if names2 else "gh api failed"
    if not names2:
        return set(), "no contexts found"
    return names2, ""


def _check_branch_protection(target: Path, source_repo: bool) -> Check:
    """Single-row check comparing branch-protection required status
    checks against review.yml job names. WARN on mismatch, SKIP on
    degraded `gh`/repo context, INFO in source-repo mode."""
    if source_repo:
        return Check("branch policy", "INFO",
                     "source repo: branch policy not audited")
    repo = _detect_owner_repo(target)
    if not repo:
        return Check("branch policy", "SKIP", "no GitHub remote on origin")
    required, degraded = _fetch_required_status_checks(repo)
    if degraded:
        return Check("branch policy", "SKIP", degraded)
    review = target / ".github" / "workflows" / "review.yml"
    if not review.is_file():
        return Check("branch policy", "INFO",
                     "review.yml not present; nothing to compare")
    raw, shape, err = _read_workflow(review.parent, review.name)
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
        return Check(
            "branch policy", "PASS",
            f"required={sorted(required)}  workflow={sorted(job_names)}",
        )
    return Check(
        "branch policy", "WARN",
        f"required-vs-workflow mismatch: "
        f"required but not emitted by any review job={missing}; "
        f"emitted by review but not required={extra}",
    )


def audit(target_dir: Path, *, provider: str | None = None) -> DoctorReport:
    """Run the full check suite. Side-effect free.

    Args:
        target_dir: repo root to audit (defaults to a fresh tmpdir would
            also work; pass the real consumer path for an honest answer).
        provider: override the provider selection (default = resolve via
            `read_provider(target)`: process env → `.env` → `.env.example`).
            Used by tests.

    Returns:
        DoctorReport with one row per check.
    """
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
    report.checks.extend(_check_templates_current(target, source_repo))
    report.checks.extend(_check_provider_declared(target))
    report.checks.append(_check_gh_auth())
    report.checks.extend(_check_secrets(target, provider, source_repo))
    report.checks.extend(_check_workflow_diagnostics(target, source_repo))
    report.checks.append(_check_branch_protection(target, source_repo))
    report.checks.extend(_check_open_pr(target))
    return report


# ---- Open PR state (issue #249) ---------------------------------------
# A PR in `mergeable: CONFLICTING` causes GitHub Actions to silently
# refuse ALL workflows on the PR — `gh pr checks <N>` returns `no
# checks reported` with no error. ci-doctor must surface this rather
# than reporting PASS. Also surfaces UNKNOWN (still computing) as WARN,
# draft state as INFO, and version-bump PRs as INFO so users don't ask
# why their CI didn't run.

def _fetch_open_pr_state(target: Path) -> tuple[dict, str]:
    """Return (pr_state_dict, degraded-message).

    Empty dict + non-empty msg means degraded (gh missing/unauth, no
    PR open for the current branch, JSON parse failed, or detached
    HEAD). Caller emits a single SKIP row in that case.
    """
    gh = shutil.which("gh")
    if not gh:
        return {}, "gh not on PATH"
    # Detect current branch via `git rev-parse --abbrev-ref HEAD`.
    try:
        cp = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return {}, f"git branch detect error: {e}"
    if cp.returncode != 0:
        return {}, f"git branch detect failed: {(cp.stderr or '').strip() or cp.returncode}"
    branch = cp.stdout.strip()
    if not branch or branch == "HEAD":
        return {}, "detached HEAD — no branch PR can target"
    # `gh pr view <branch> --json ...` — non-zero exit when no PR is
    # open for the branch. Treat as degraded (SKIP), not error.
    try:
        cp = subprocess.run(
            [gh, "pr", "view", branch, "--json",
             "mergeable,mergeStateStatus,isDraft,title"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return {}, f"gh pr view error: {e}"
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no pull requests found" in err.lower() or "not found" in err.lower():
            return {}, "no open PR for current branch"
        return {}, f"gh pr view failed: {err or cp.returncode}"
    try:
        return json.loads(cp.stdout), ""
    except json.JSONDecodeError as e:
        return {}, f"gh pr view JSON parse error: {e}"


def _check_open_pr(target: Path) -> list[Check]:
    """Diagnose the open PR's merge state for the current branch.

    Issue #249: when a PR is in `mergeable: CONFLICTING`, GitHub
    Actions silently refuses to run any workflow on the PR — neither
    `ci.yml` nor `review.yml` nor `auto-fix-pr.yml` fires. The
    previous ci-doctor implementation was purely local and could
    return PASS for a PR that would sit in a no-checks state for an
    arbitrary interval. This check makes the open-PR state part of
    the audit so users see the actual readiness signal.

    Severity matrix:
      - CONFLICTING → FAIL (CI will not run)
      - UNKNOWN     → WARN (GitHub still computing; re-run in 30s)
      - MERGEABLE   → PASS
      - other enum  → INFO (forward-compat: future schema change)
      - isDraft     → INFO (required checks gated until ready-for-review)
      - bump-title  → INFO (ci/review/security skip by design)
      - degraded    → SKIP (gh missing, no PR, etc.)
    """
    data, degraded = _fetch_open_pr_state(target)
    if degraded:
        return [Check("open PR state", "SKIP", degraded)]
    rows: list[Check] = []
    mergeable = data.get("mergeable", "")
    if mergeable == "CONFLICTING":
        rows.append(Check(
            "open PR mergeable", "FAIL",
            "open PR has merge conflicts with main — CI will not run. "
            "Run: git fetch origin main && git merge origin/main",
        ))
    elif mergeable == "UNKNOWN":
        rows.append(Check(
            "open PR mergeable", "WARN",
            "GitHub still computing merge state — re-run "
            "/dev-kit:ci-doctor in 30s",
        ))
    elif mergeable == "MERGEABLE":
        rows.append(Check("open PR mergeable", "PASS", "no conflicts"))
    else:
        # Unknown enum value (e.g. a new GitHub state) — surface as
        # INFO so future schema changes don't silently degrade the
        # check into a false PASS.
        rows.append(Check(
            "open PR mergeable", "INFO",
            f"unrecognized mergeable value: {mergeable!r}",
        ))
    if data.get("isDraft"):
        rows.append(Check(
            "open PR draft", "INFO",
            "PR is a draft — required checks gated until marked "
            "ready for review",
        ))
    title = data.get("title", "")
    if title.startswith("chore(release): bump dev-kit to v"):
        rows.append(Check(
            "open PR title", "INFO",
            "bump-PR — ci/review/security explicitly skip per "
            "templates/ci/.github/workflows/ci.yml",
        ))
    return rows


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    r = audit(target)
    for line in r.summary_lines():
        print(line)
    sys.exit(0 if r.ok else 1)
