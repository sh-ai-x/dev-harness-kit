"""render_proposal_html.py -- Pure function: YAML proposal -> self-contained HTML.

The /dev-kit:proposal skill hands a YAML proposal file to this module
and writes the returned HTML to `docs/proposals/<main>/<sub>.html`.
The skill body itself stays read-only; the write is the CLI driver's job
(`bin/dev-kit-proposal.py`).

Layout: every proposal lives at `docs/proposals/<main>/<sub>.{yaml,html}`
where:

- `<main>` is the umbrella (e.g. `harness-architecture` -- one umbrella
  groups N related sub-proposals; for issue #280 the umbrella holds 12
  sub-topics + the 00-index navigation page).
- `<sub>` is the sub-topic slug (e.g. `protocol-layer`,
  `live-context-server`, `00-index`). The file is named after the
  sub-topic -- not `index.{yaml,html}` -- so the leaf is recognisable
  on a flat directory listing and from a static-site host.

Cross-references from the 00-index page (`<main>/00-index.html`) to a
sibling are bare `<sub>.html` (no `../` needed, because all files live
in the same `<main>/` directory and resolve as siblings under `file://`
and on any static-site host).

Input shape (YAML)::

    title: Harness Architecture Proposal
    status: design-discussion
    issue: 280
    date: 2026-07-21
    tags: [mcp, harness, design]
    sections:
      - title: TL;DR
        body: |
          MCP harness wins over document harness when the loop needs
          **live tool integration** or *multi-actor coordination*.

      - title: When MCP harness is needed
        body: |
          | Loop | What it does | MCP fit |
          |------|--------------|---------|
          | Validation | judge loop | High |

Body markdown-lite (intentionally narrow):

- headings (# ## ###)
- paragraphs (blank-line separated)
- unordered (-) and ordered (1.) lists
- GFM tables (pipe-delimited)
- fenced code blocks (```)
- inline: **bold**, *italic*, `code`, [text](url)
- horizontal rule (---)
- blockquote (>)

Output invariants:

- No `<script>` tag. No `<link rel="stylesheet">`. No remote `<img>`.
- Inline CSS only. Dark-mode aware.
- Defensive HTML escape on every interpolated value.
- Pure function: no I/O, no filesystem, no network. Deterministic.

Mirror patterns: `lib/render_report_html.py` (eval+inspect reports),
`bin/dev-kit-report.py` (skill + CLI driver separation).
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import yaml

from lib.atomic import atomic_write_text

KST = timezone(timedelta(hours=9))

STATUS_TAG_CLASS = {
    "draft": "tag-warn",
    "design-discussion": "tag-info",
    "ready-for-review": "tag-info",
    "accepted": "tag-ok",
    "rejected": "tag-bad",
    "superseded": "tag-warn",
}

# Status-routed layout (see module docstring). The bucket set is a tight
# whitelist; adding a fourth name is a deliberate design choice (tests
# pin `BUCKETS == {"review", "accepted", "rejected"}`).
BUCKETS = ("review", "accepted", "rejected")

STATUS_TO_BUCKET = {
    "draft": "review",
    "design-discussion": "review",
    "ready-for-review": "review",
    "accepted": "accepted",
    "rejected": "rejected",
    "superseded": "rejected",
}


def bucket_for_status(status: str) -> str:
    """Return the filesystem bucket for a proposal status. Unknown
    statuses fall back to `review` so a typo in the YAML still produces
    a routable path rather than crashing the renderer."""
    return STATUS_TO_BUCKET.get(status, "review")


# Reserved file stems that previous refactors used as canonical
# names; they must not surface as a sub-topic slug in any bucket dir
# or in the legacy flat layout. Hoisted to a single source of truth
# (CC-8 review) so `_list_proposals` and `_migrate` stay in sync.
RESERVED_SLUGS = frozenset({"proposal", "index"})

INLINE_CSS = """
:root {
  color-scheme: light dark;
  --fg: #1d1d1f;
  --bg: #fbfbfd;
  --muted: #5b5b62;
  --border: #d2d2d7;
  --card-bg: #ffffff;
  --th-bg: #f5f5f7;
  --row-alt: #fafafa;
  --code-bg: #f5f5f7;
  --accent: #0a84ff;
  --accent-soft: rgba(10, 132, 255, 0.08);
  --ok: #1f8a3b;
  --warn: #a06400;
  --bad: #b03030;
  --callout-bg: rgba(10, 132, 255, 0.06);
  --callout-border: #0a84ff;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 4px 12px rgba(0, 0, 0, 0.04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #f5f5f7;
    --bg: #1c1c1e;
    --muted: #aeaeb2;
    --border: #38383a;
    --card-bg: #2c2c2e;
    --th-bg: #3a3a3c;
    --row-alt: #232325;
    --code-bg: #2c2c2e;
    --accent: #0a84ff;
    --accent-soft: rgba(10, 132, 255, 0.18);
    --ok: #4cd964;
    --warn: #ff9f0a;
    --bad: #ff453a;
    --callout-bg: rgba(10, 132, 255, 0.14);
    --callout-border: #0a84ff;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 4px 12px rgba(0, 0, 0, 0.3);
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, sans-serif;
  max-width: 980px;
  margin: 0 auto;
  padding: 3rem 1.5rem 5rem;
  line-height: 1.6;
  color: var(--fg);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
h1 { font-size: 2.4rem; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 0.4rem; }
h2 { font-size: 1.6rem; font-weight: 600; letter-spacing: -0.01em; margin: 3.5rem 0 1rem; }
h3 { font-size: 1.15rem; font-weight: 600; margin: 2rem 0 0.6rem; }
p { margin: 0.7rem 0; }
.meta { color: var(--muted); font-size: 0.92rem; margin: 0 0 2.5rem; }
.tags { margin: 0 0 1.5rem; }
.tag {
  display: inline-block;
  padding: 0.18rem 0.6rem;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  margin-right: 0.4rem;
}
.tag-ok { background: rgba(31, 138, 59, 0.12); color: var(--ok); }
.tag-warn { background: rgba(160, 100, 0, 0.12); color: var(--warn); }
.tag-bad { background: rgba(176, 48, 48, 0.12); color: var(--bad); }
.tag-info { background: var(--accent-soft); color: var(--accent); }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 1rem 0 1.5rem;
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  font-size: 0.95rem;
}
th, td { padding: 0.7rem 0.9rem; text-align: left; border-bottom: 1px solid var(--border); }
th { background: var(--th-bg); font-weight: 600; }
tr:last-child td { border-bottom: 0; }
tr:nth-child(even) td { background: var(--row-alt); }
td code, th code { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 0.88em; }
.callout {
  background: var(--callout-bg);
  border-left: 4px solid var(--callout-border);
  border-radius: 6px;
  padding: 0.9rem 1.2rem;
  margin: 1.5rem 0;
}
.callout .label { font-weight: 600; margin-bottom: 0.3rem; display: block; }
ul, ol { padding-left: 1.4rem; }
li { margin: 0.3rem 0; }
li > strong { color: var(--accent); }
code { font-family: 'SF Mono', Menlo, Consolas, monospace; background: var(--code-bg); padding: 0.1rem 0.35rem; border-radius: 4px; font-size: 0.9em; }
pre { background: var(--code-bg); padding: 0.9rem 1.1rem; border-radius: 8px; overflow-x: auto; font-size: 0.88em; line-height: 1.5; }
pre code { background: transparent; padding: 0; }
blockquote {
  border-left: 3px solid var(--border);
  margin: 1rem 0;
  padding: 0.2rem 1rem;
  color: var(--muted);
}
.section-divider { border: 0; border-top: 1px solid var(--border); margin: 3rem 0; }
.back-link {
  margin: 0 0 1.5rem;
  font-size: 0.9rem;
  color: var(--muted);
}
.back-link a { color: var(--accent); }
.back-link a:hover { text-decoration: underline; }
.toc {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.2rem 1.5rem;
  margin: 0 0 3rem;
  font-size: 0.95rem;
}
.toc strong { display: block; margin-bottom: 0.5rem; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
.toc ol { margin: 0; padding-left: 1.2rem; }
footer { margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.85rem; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
/* ----- Before / After + Pros / Cons / Limitations ---------------------------
 * Structured sections emitted when the YAML declares
 * `before:`, `after:`, `pros:`, `cons:`, `limitations:`. See
 * `skills/proposal/SKILL.md` §Workflow and `lib/render_proposal_html.py`
 * `_render_before_after` / `_render_pros_cons_limitations`. Color cues
 * reuse the existing --ok / --warn / --bad tokens so dark-mode parity
 * is automatic. */
.ba-section { margin: 2.5rem 0 1rem; }
.ba-section h2 { margin-top: 0; }
.ba-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1.25rem;
  margin: 1.5rem 0;
}
@media (min-width: 880px) {
  .ba-grid { grid-template-columns: 1fr 1fr; }
}
.ba-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.2rem 1.4rem;
  box-shadow: var(--shadow);
}
.ba-card h3 {
  margin: 0 0 0.7rem;
  font-size: 1.05rem;
  letter-spacing: -0.01em;
}
.before-card { border-left: 4px solid var(--muted); }
.after-card  { border-left: 4px solid var(--accent); }
.before-card h3 { color: var(--muted); }
.after-card h3  { color: var(--accent); }
.evidence-list, .files-list { padding-left: 1.2rem; margin: 0.4rem 0 0.7rem; }
.evidence-list li { margin: 0.25rem 0; }
.files-list li { margin: 0.5rem 0; }
.file-path {
  display: inline-block;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 0.85em;
  background: var(--code-bg);
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  margin-right: 0.4rem;
}
.pcl-section { margin: 2rem 0 1rem; }
.pcl-section h3 {
  margin: 0 0 0.6rem;
  font-size: 1.05rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.pros-list, .cons-list, .limitations-list {
  list-style: none;
  padding-left: 0;
  margin: 0.4rem 0 0.6rem;
}
.pros-list li, .cons-list li, .limitations-list li {
  position: relative;
  padding: 0.35rem 0 0.35rem 1.6rem;
  margin: 0.2rem 0;
}
.pros-list li::before {
  content: "✓"; /* check */
  position: absolute; left: 0;
  color: var(--ok); font-weight: 700;
}
.cons-list li::before {
  content: "✗"; /* ballot x */
  position: absolute; left: 0;
  color: var(--bad); font-weight: 700;
}
.limitations-list li::before {
  content: "!";      /* warn glyph */
  position: absolute; left: 0;
  width: 1.1rem; height: 1.1rem;
  border-radius: 999px;
  background: var(--warn);
  color: var(--bg);
  font-weight: 700; font-size: 0.75rem;
  display: inline-flex; align-items: center; justify-content: center;
}
.pcl-pros h3    { color: var(--ok); }
.pcl-cons h3    { color: var(--bad); }
.pcl-limit h3   { color: var(--warn); }
/* ----- Timeline chips -------------------------------------------------------
 * Emitted by `_meta_line` when the proposal YAML declares
 * `started:` or `shipped:` (both YYYY-MM-DD). Visual cue mirrors the
 * existing --ok / --warn tokens so dark-mode parity is automatic. */
.timeline-chip {
  display: inline-block;
  padding: 0.18rem 0.6rem;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.timeline-started { background: rgba(160, 100, 0, 0.12); color: var(--warn); }
.timeline-shipped { background: rgba(31, 138, 59, 0.12);  color: var(--ok);  }
"""


@dataclass(frozen=True)
class ProposalSection:
    title: str
    body: str


@dataclass(frozen=True)
class BeforeState:
    """Optional structured description of the code's *current* state.

    `summary` is a markdown-lite body (same grammar as
    `ProposalSection.body`). `evidence` is a list of cited observations
    (strings). Each entry should point at a concrete file, log line, or
    commit so the "before" claim is checkable by the reviewer. The
    /dev-kit:proposal skill workflow requires this evidence be gathered
    by reading the existing code BEFORE the proposal YAML is authored;
    see `skills/proposal/SKILL.md` §Workflow.
    """

    summary: str
    evidence: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodeChange:
    """One file's intended modification under `AfterState.files`.

    `path` is repo-relative. `change` is a markdown-lite body describing
    what will change and why. The list is a reviewer commitment —
    anything not listed must NOT change.
    """

    path: str
    change: str


@dataclass(frozen=True)
class AfterState:
    """Optional structured description of the code's *proposed* state.

    `summary` is a markdown-lite body. `files` lists the files the
    proposal will touch (add/modify/delete). Keep the list narrow:
    every entry is a reviewer commitment. Anything not in `files` must
    NOT change.
    """

    summary: str
    files: List[CodeChange] = field(default_factory=list)


@dataclass(frozen=True)
class Proposal:
    title: str
    status: str
    issue: Optional[int]
    date: str
    tags: List[str]
    sections: List[ProposalSection] = field(default_factory=list)
    # Structured before/after + pros/cons/limitations. All optional.
    # When all five are absent the rendered HTML is identical to the
    # pre-extension shape, so existing proposals do not change.
    before: Optional[BeforeState] = None
    after: Optional[AfterState] = None
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    # Implementation timeline (additive, both YYYY-MM-DD or unset).
    # `started` is the date implementation began on an accepted
    # proposal. `shipped` is the date the implementation landed
    # (PR merged, or the proposal is otherwise considered done).
    # The renderer emits timeline chips when these are set; the
    # `--in-flight` CLI filter lists accepted proposals where
    # `started` is set and `shipped` is not. BUCKETS is unchanged --
    # timeline state is time-bound, not a categorical bucket.
    started: Optional[str] = None
    shipped: Optional[str] = None

    @property
    def status_class(self) -> str:
        return STATUS_TAG_CLASS.get(self.status, "tag-info")


def parse_proposal_yaml(text: str) -> Proposal:
    """Parse a YAML proposal document into a `Proposal` value object.

    Required: title, status. Optional: issue (int), date (str), tags
    (list[str]), sections (list of {title, body}), before
    ({summary, evidence}), after ({summary, files}), pros
    (list[str]), cons (list[str]), limitations (list[str]).
    """
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("proposal YAML must be a mapping at the top level")
    if "title" not in raw or not isinstance(raw["title"], str):
        raise ValueError("proposal YAML must include a string `title`")
    status = str(raw.get("status", "draft"))
    issue_val = raw.get("issue")
    issue = int(issue_val) if issue_val is not None else None
    date = str(raw.get("date", ""))
    tags_raw = raw.get("tags", [])
    if not isinstance(tags_raw, list):
        raise ValueError("`tags` must be a list of strings")
    tags = [str(t) for t in tags_raw]
    sections_raw = raw.get("sections", [])
    if not isinstance(sections_raw, list):
        raise ValueError("`sections` must be a list of {title, body} mappings")
    sections: List[ProposalSection] = []
    for i, sec in enumerate(sections_raw):
        if not isinstance(sec, dict):
            raise ValueError(f"sections[{i}] must be a mapping")
        if "title" not in sec or not isinstance(sec["title"], str):
            raise ValueError(f"sections[{i}] must include a string `title`")
        body = sec.get("body", "")
        if not isinstance(body, str):
            raise ValueError(f"sections[{i}].body must be a string")
        sections.append(ProposalSection(title=sec["title"], body=body))

    before = _parse_before(raw.get("before"))
    after = _parse_after(raw.get("after"))
    pros = _parse_string_list(raw.get("pros"), "pros")
    cons = _parse_string_list(raw.get("cons"), "cons")
    limitations = _parse_string_list(raw.get("limitations"), "limitations")
    started = _parse_optional_date(raw.get("started"), "started")
    shipped = _parse_optional_date(raw.get("shipped"), "shipped")

    return Proposal(
        title=raw["title"],
        status=status,
        issue=issue,
        date=date,
        tags=tags,
        sections=sections,
        before=before,
        after=after,
        pros=pros,
        cons=cons,
        limitations=limitations,
        started=started,
        shipped=shipped,
    )


def _parse_before(raw: object) -> Optional[BeforeState]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("`before` must be a mapping with `summary` (and optional `evidence`)")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise ValueError("`before.summary` must be a string")
    evidence_raw = raw.get("evidence", [])
    if not isinstance(evidence_raw, list):
        raise ValueError("`before.evidence` must be a list of strings")
    evidence = _parse_string_items(evidence_raw, "before.evidence")
    return BeforeState(summary=summary, evidence=evidence)


def _parse_after(raw: object) -> Optional[AfterState]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("`after` must be a mapping with `summary` (and optional `files`)")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise ValueError("`after.summary` must be a string")
    files_raw = raw.get("files", [])
    if not isinstance(files_raw, list):
        raise ValueError("`after.files` must be a list of {path, change} mappings")
    files: List[CodeChange] = []
    for i, f in enumerate(files_raw):
        if not isinstance(f, dict):
            raise ValueError(f"after.files[{i}] must be a mapping")
        if "path" not in f or not isinstance(f["path"], str):
            raise ValueError(f"after.files[{i}] must include a string `path`")
        if "change" not in f:
            # Maintenance reviewer (PR #595): a file entry without a
            # `change` body is malformed; reject instead of silently
            # defaulting to empty string.
            raise ValueError(f"after.files[{i}] must include a string `change`")
        change = f["change"]
        if not isinstance(change, str):
            raise ValueError(f"after.files[{i}].change must be a string")
        files.append(CodeChange(path=f["path"], change=change))
    return AfterState(summary=summary, files=files)


def _parse_string_items(raw: list, field_name: str) -> List[str]:
    """Strict list[str] parser. Maintenance reviewer (PR #595):
    silently coercing via `str(...)` accepts malformed inputs like
    `pros: [123]` and produces nonsense. Reject non-string items."""
    out: List[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise ValueError(
                f"`{field_name}[{i}]` must be a string "
                f"(got {type(item).__name__}: {item!r})"
            )
        out.append(item)
    return out


def _parse_string_list(raw: object, field_name: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"`{field_name}` must be a list of strings")
    return _parse_string_items(raw, field_name)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_optional_date(raw: object, field_name: str) -> Optional[str]:
    r"""Strict YYYY-MM-DD parser for the optional `started:` / `shipped:`
    timeline fields. Returns None when unset; raises ValueError for any
    non-string, non-date, or non-ISO value, OR a string that looks
    YYYY-MM-DD-shaped but isn't a real calendar date (M1 reviewer,
    PR #804: the prior regex \d{4}-\d{2}-\d{2} accepted
    `2026-13-01` and `2026-02-30`).

    YAML auto-parses bare ISO-8601 literals like `started: 2026-09-01`
    into `datetime.date` objects, so we accept those directly. Quoted
    strings (`started: "2026-13-01"`) bypass YAML's timestamp
    validation and reach us as plain text -- those go through
    `datetime.date.fromisoformat`, which rejects month-out-of-range
    and Feb-30-style calendar typos. Anything else is rejected so a
    typo can't silently produce a broken chip in the rendered HTML."""
    if raw is None:
        return None
    # The module-level `from datetime import datetime` binds only the
    # `datetime` *class*, not the `datetime` *module*. Reach for the
    # module so `_dt.date` (the type) and `_dt.datetime` (the
    # subclass) are both available.
    import datetime as _dt
    if isinstance(raw, _dt.datetime):
        # YAML only produces `date` for bare ISO dates; `datetime`
        # means the user wrote `started: 2026-09-01T10:00:00`, which
        # is too precise for a day-granularity timeline chip.
        raise ValueError(
            f"`{field_name}` must be a YYYY-MM-DD date "
            f"(got datetime {raw.isoformat()!r})"
        )
    if isinstance(raw, _dt.date):
        return raw.isoformat()
    if not isinstance(raw, str):
        raise ValueError(
            f"`{field_name}` must be a YYYY-MM-DD date string "
            f"(got {type(raw).__name__}: {raw!r})"
        )
    # Shape check first (cheap), then calendar validity (real check).
    # The shape branch catches obvious typos like `2026/09/01` without
    # spending the parser call on them.
    if not _ISO_DATE_RE.fullmatch(raw):
        raise ValueError(
            f"`{field_name}` must match YYYY-MM-DD (got {raw!r})"
        )
    # Calendar-validity: rejects `2026-13-01` (month 13), `2026-02-30`
    # (Feb 30), and `2025-02-29` (Feb 29 in a non-leap year).
    _dt.date.fromisoformat(raw)
    return raw


# ----- Markdown-lite renderer -----------------------------------------------

_INLINE_TOKEN_RE = re.compile(
    r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))"
)
# Allowlist for hyperlink href schemes. Two classes are accepted:
#   (a) Explicit safe schemes: `http://`, `https://`, `mailto:`.
#   (b) Safe relative paths: no scheme (no `:`), starting with `./`,
#       `../`, a relative segment, or `/`. These are how cross-document
#       links inside `docs/proposals/<main>/` work between sibling
#       files (`protocol-layer.html`, `../protocol-layer/index.html`,
#       etc.) and they resolve under `file://` exactly the way a
#       browser would resolve them for any other static HTML.
# Anything else (javascript:, data:, vbscript:, file:) is rendered as
# escaped text rather than an executable anchor. `file://` is
# rejected because the proposal HTML is meant to be safe-to-open
# from `file://`; allowing `file:` links inside would defeat that.
_SAFE_URL_SCHEMES = re.compile(
    r"^(?:https?|mailto):",
    re.IGNORECASE,
)
_SAFE_RELATIVE_HREF = re.compile(
    r"^(?:\.{0,2}/|[A-Za-z0-9_\-./?#=&%]+)$"
)


def _render_inline(text: str) -> str:
    """Render inline markdown (bold, italic, code, links) with HTML escape.

    Tokenizes first so escaping is applied to text-only segments; tokens
    are matched against the raw text and the result is escaped piece-wise.
    A literal `<script>` in the input renders as `&lt;script&gt;` because
    the raw text passes through `html.escape` before token replacement.
    """
    safe = html.escape(text, quote=False)
    pieces: List[str] = []
    cursor = 0
    for m in _INLINE_TOKEN_RE.finditer(safe):
        if m.start() > cursor:
            pieces.append(safe[cursor:m.start()])
        token = m.group(0)
        if token.startswith("**") and token.endswith("**"):
            pieces.append(f"<strong>{token[2:-2]}</strong>")
        elif token.startswith("*") and token.endswith("*"):
            pieces.append(f"<em>{token[1:-1]}</em>")
        elif token.startswith("`") and token.endswith("`"):
            pieces.append(f"<code>{token[1:-1]}</code>")
        elif token.startswith("["):
            link_m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
            if link_m:
                label, href = link_m.group(1), link_m.group(2)
                # Note: `label` and `href` come from the already-escaped
                # `safe` text, so they contain HTML entities (e.g. `&amp;`).
                # We must NOT re-escape them or `&amp;` becomes `&amp;amp;`.
                # Only `"` needs escaping to keep the attribute intact.
                href_attr = href.replace('"', "&quot;")
                href_stripped = href.strip()
                if _SAFE_URL_SCHEMES.match(href_stripped) or _SAFE_RELATIVE_HREF.match(href_stripped):
                    pieces.append(f'<a href="{href_attr}">{label}</a>')
                else:
                    # Disallowed scheme (javascript:, data:, vbscript:,
                    # file:, raw text with a colon-prefixed scheme we
                    # don't recognize). Render as plain text with parens
                    # so it reads naturally:
                    # `[click](javascript:alert(1))` -> `click (javascript:alert(1))`.
                    # Note: the regex consumes `[label](href` and the
                    # leftover `)` after the match stays in the
                    # surrounding text.
                    pieces.append(f"{label} ({href})")
            else:
                pieces.append(token)
        else:
            pieces.append(token)
        cursor = m.end()
    if cursor < len(safe):
        pieces.append(safe[cursor:])
    return "".join(pieces)


def _split_table_row(row: str) -> List[str]:
    """Split a GFM table row on `|`, trim, drop leading/trailing empty cells.

    Honours `\\|` as a literal pipe inside a cell. GFM escapes pipes in
    table cells by backslash; without this, a cell like
    ``PreToolUse Edit\\|Write\\|MultiEdit`` is mis-split into 3 cells.
    """
    stripped = row.strip().strip("|")
    # Split on unescaped pipes only.
    parts = re.split(r"(?<!\\)\|", stripped)
    # Unescape `\\|` -> `|` inside each cell; trim surrounding whitespace.
    return [p.replace("\\|", "|").strip() for p in parts]


def _render_table(lines: List[str]) -> str:
    """Render a GFM table block. Assumes `lines` is contiguous table lines
    (header, separator, then 0+ body rows)."""
    if len(lines) < 2:
        return _render_paragraphs(lines)
    header = _split_table_row(lines[0])
    body = [_split_table_row(r) for r in lines[2:]]
    out = ["<table>", "<thead><tr>"]
    for h in header:
        out.append(f"<th>{_render_inline(h)}</th>")
    out.append("</tr></thead>")
    if body:
        out.append("<tbody>")
        for row in body:
            out.append("<tr>")
            for i, cell in enumerate(row):
                tag = "td"
                out.append(f"<{tag}>{_render_inline(cell)}</{tag}>")
            out.append("</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def _render_paragraphs(lines: List[str]) -> str:
    text = " ".join(line.strip() for line in lines).strip()
    if not text:
        return ""
    return f"<p>{_render_inline(text)}</p>"


def _render_list(items: List[str], ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    out = [f"<{tag}>"]
    for item in items:
        out.append(f"<li>{_render_inline(item)}</li>")
    out.append(f"</{tag}>")
    return "".join(out)


def _render_blockquote(lines: List[str]) -> str:
    text = " ".join(line.lstrip(">").strip() for line in lines).strip()
    return f"<blockquote><p>{_render_inline(text)}</p></blockquote>"


def render_body(body: str) -> str:
    """Render a markdown-lite body string to safe HTML."""
    lines = body.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            j = i + 1
            while j < n and not lines[j].strip().startswith("```"):
                j += 1
            code_text = "\n".join(lines[i + 1:j])
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(code_text)}</code></pre>")
            i = j + 1
            continue

        # Horizontal rule
        if re.match(r"^-{3,}$", stripped):
            out.append('<hr class="section-divider">')
            i += 1
            continue

        # Heading
        h_m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if h_m:
            level = len(h_m.group(1))
            text = h_m.group(2).strip()
            out.append(f"<h{level}>{_render_inline(text)}</h{level}>")
            i += 1
            continue

        # Table (collect contiguous pipe-delimited lines)
        if "|" in stripped and i + 1 < n and re.match(r"^\s*\|?\s*:?-+:?(\s*\|\s*:?-+:?)+\s*\|?\s*$", lines[i + 1].strip()):
            j = i
            while j < n and "|" in lines[j]:
                j += 1
            out.append(_render_table(lines[i:j]))
            i = j
            continue

        # Blockquote
        if stripped.startswith(">"):
            j = i
            while j < n and lines[j].strip().startswith(">"):
                j += 1
            out.append(_render_blockquote(lines[i:j]))
            i = j
            continue

        # Unordered list
        if re.match(r"^[-*]\s+", stripped):
            j = i
            items: List[str] = []
            current: List[str] | None = None

            def _flush_current() -> None:
                nonlocal current
                if current is not None:
                    items.append(" ".join(current).strip())
                    current = None

            while j < n:
                line_j = lines[j]
                stripped_j = line_j.strip()
                if not stripped_j:
                    # A blank line ENDS this list — start the new list
                    # or paragraph collector from the next iteration.
                    # (Markdown allows lazy continuation across one
                    # blank line, but that requires a paragraph block
                    # detector inside the indented context; the bug
                    # this fixes was specifically when the YAML block
                    # scalar wraps bullets without intervening blanks,
                    # so "blank = end" is enough.)
                    break
                if re.match(r"^[-*]\s+", stripped_j):
                    _flush_current()
                    current = [re.sub(r"^[-*]\s+", "", stripped_j)]
                elif line_j.startswith((" ", "\t")):
                    # Indented continuation of the previous item. Append
                    # the stripped text so the bullet's text becomes
                    # `<first line> <continuation>` (PR #494 review 🟡 #5).
                    if current is None:
                        break  # defensive
                    current.append(stripped_j)
                else:
                    # Non-bullet, non-indented line: end of this list.
                    break
                j += 1
            _flush_current()
            out.append(_render_list(items, ordered=False))
            i = j
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", stripped):
            j = i
            items: List[str] = []
            current: List[str] | None = None

            def _flush_current_ordered() -> None:
                nonlocal current
                if current is not None:
                    items.append(" ".join(current).strip())
                    current = None

            while j < n:
                line_j = lines[j]
                stripped_j = line_j.strip()
                if not stripped_j:
                    break
                if re.match(r"^\d+\.\s+", stripped_j):
                    _flush_current_ordered()
                    current = [re.sub(r"^\d+\.\s+", "", stripped_j)]
                elif line_j.startswith((" ", "\t")):
                    if current is None:
                        break
                    current.append(stripped_j)
                else:
                    break
                j += 1
            _flush_current_ordered()
            out.append(_render_list(items, ordered=True))
            i = j
            continue

        # Blank line: skip
        if not stripped:
            i += 1
            continue

        # Paragraph (collect until blank or block transition)
        j = i
        while j < n and lines[j].strip() and not _is_block_start(lines[j]):
            j += 1
        if j == i:
            # Safety: if the line is non-blank AND a block-start but no
            # branch matched (e.g. future block types), force forward progress
            # by rendering it as a single-line paragraph rather than looping.
            j = i + 1
        out.append(_render_paragraphs(lines[i:j]))
        i = j

    return "\n".join(s for s in out if s)


def _is_block_start(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    # `*` is NOT a block-start marker here -- `**bold**` or `*italic*` at the
    # start of a line is just inline formatting inside a paragraph, not a
    # bullet (we use `-` for unordered lists). Including `*` would mis-route
    # paragraph lines starting with bold into an unhandled branch and loop.
    if s.startswith(("#", ">", "```", "-")) or re.match(r"^\d+\.\s+", s):
        return True
    if re.match(r"^-{3,}$", s):
        return True
    if "|" in s:
        return True
    return False


# ----- Top-level render -----------------------------------------------------


def _meta_line(p: Proposal) -> str:
    parts: List[str] = []
    if p.date:
        parts.append(html.escape(p.date))
    if p.issue is not None:
        parts.append(
            f'<a href="https://github.com/sh-ai-x/dev-harness-kit/issues/{p.issue}">'
            f"issue #{p.issue}</a>"
        )
    if p.status:
        parts.append(f'<span class="tag {p.status_class}">{html.escape(p.status)}</span>')
    # Timeline chips appear AFTER the status chip so the visual
    # reading order is `when proposed · status · implementing since ·
    # shipped`. Both are html.escape'd twice-once-via-tag-wrap plus the
    # attr-quote escape below.
    if p.started:
        parts.append(
            f'<span class="timeline-chip timeline-started">started '
            f'{html.escape(p.started)}</span>'
        )
    if p.shipped:
        parts.append(
            f'<span class="timeline-chip timeline-shipped">shipped '
            f'{html.escape(p.shipped)}</span>'
        )
    return " · ".join(parts)


def _toc(p: Proposal) -> str:
    """Contents list.

    Includes the structured sections (`Before / After`, `Pros`, `Cons`,
    `Limitations`) when they are populated so reviewers can jump to them
    directly. The anchor names are stable: `ba-section`, `pcl-pros`,
    `pcl-cons`, `pcl-limit`.
    """
    items: List[str] = []
    for i, s in enumerate(p.sections):
        items.append(f'<li><a href="#sec-{i}">{html.escape(s.title)}</a></li>')
    if p.before or p.after:
        items.append('<li><a href="#ba-section">Before / After</a></li>')
    if p.pros:
        items.append('<li><a href="#pcl-pros">Pros</a></li>')
    if p.cons:
        items.append('<li><a href="#pcl-cons">Cons</a></li>')
    if p.limitations:
        items.append('<li><a href="#pcl-limit">Limitations</a></li>')
    if not items:
        return ""
    return (
        '<div class="toc"><strong>Contents</strong>'
        f'<ol>{"".join(items)}</ol></div>'
    )


def _render_before_after(p: Proposal) -> str:
    """Render the structured `before` / `after` blocks.

    When both are present, they share a 2-column grid. When only one is
    present it renders alone (no grid wrapper). The card border colour
    encodes intent (muted = state-now, accent = state-after).
    """
    if not p.before and not p.after:
        return ""
    cards: List[str] = []
    if p.before:
        evidence_html = ""
        if p.before.evidence:
            evidence_items = "".join(
                f"<li>{_render_inline(e)}</li>" for e in p.before.evidence
            )
            evidence_html = (
                f'<h4 style="font-size:0.85rem;margin:0.8rem 0 0.3rem;'
                f'color:var(--muted);">Evidence</h4>'
                f'<ul class="evidence-list">{evidence_items}</ul>'
            )
        cards.append(
            '<div class="ba-card before-card">'
            f'<h3>Before (current state)</h3>'
            f'{render_body(p.before.summary)}'
            f'{evidence_html}'
            '</div>'
        )
    if p.after:
        files_html = ""
        if p.after.files:
            file_items = "".join(
                f'<li><span class="file-path">{html.escape(f.path)}</span>'
                f'{render_body(f.change)}</li>'
                for f in p.after.files
            )
            files_html = (
                '<h4 style="font-size:0.85rem;margin:0.8rem 0 0.3rem;'
                'color:var(--accent);">Files</h4>'
                f'<ul class="files-list">{file_items}</ul>'
            )
        cards.append(
            '<div class="ba-card after-card">'
            f'<h3>After (proposed state)</h3>'
            f'{render_body(p.after.summary)}'
            f'{files_html}'
            '</div>'
        )
    grid_class = "ba-grid" if len(cards) == 2 else ""
    inner = "".join(cards)
    grid_html = f'<div class="{grid_class}">{inner}</div>' if grid_class else inner
    return (
        '<section id="ba-section" class="ba-section">'
        '<h2>Before / After</h2>'
        f'{grid_html}'
        '</section>'
    )


def _render_pros_cons_limitations(p: Proposal) -> str:
    """Render the three flat lists when present.

    Each list gets its own anchor + h3 so the TOC and direct links work.
    Order: Pros → Cons → Limitations (reviewer convention: strengths
    first, weaknesses second, then what's known-not-solved).
    """
    parts: List[str] = []
    if p.pros:
        items = "".join(f"<li>{_render_inline(s)}</li>" for s in p.pros)
        parts.append(
            '<section id="pcl-pros" class="pcl-section pcl-pros">'
            '<h3>Pros</h3>'
            f'<ul class="pros-list">{items}</ul>'
            '</section>'
        )
    if p.cons:
        items = "".join(f"<li>{_render_inline(s)}</li>" for s in p.cons)
        parts.append(
            '<section id="pcl-cons" class="pcl-section pcl-cons">'
            '<h3>Cons</h3>'
            f'<ul class="cons-list">{items}</ul>'
            '</section>'
        )
    if p.limitations:
        items = "".join(f"<li>{_render_inline(s)}</li>" for s in p.limitations)
        parts.append(
            '<section id="pcl-limit" class="pcl-section pcl-limit">'
            '<h3>Limitations</h3>'
            f'<ul class="limitations-list">{items}</ul>'
            '</section>'
        )
    return "".join(parts)


def render(
    p: Proposal,
    now: Optional[str] = None,
    back_to_href: Optional[str] = None,
    back_to_label: Optional[str] = None,
) -> str:
    """Render a `Proposal` to a self-contained HTML document.

    Pure function (no I/O). `now` defaults to today's date in KST;
    pass a fixed string for deterministic tests. `back_to_href` /
    `back_to_label` optionally emit a `.back-link` nav bar before `<h1>`.
    """
    sections_html: List[str] = []
    for i, sec in enumerate(p.sections):
        sections_html.append(
            f'<h2 id="sec-{i}">{html.escape(sec.title)}</h2>\n'
            f'{render_body(sec.body)}'
        )

    tags_html = ""
    if p.tags:
        tag_chips = "".join(
            f'<span class="tag tag-info">{html.escape(t)}</span>' for t in p.tags
        )
        tags_html = f'<div class="tags">{tag_chips}</div>'

    back_link_html = ""
    if back_to_href:
        # Default label = the href's basename without extension
        # (`00-index.html` -> `00-index`, `index.html` -> `index`).
        href_only = back_to_href.split("?")[0].split("#")[0]
        basename = href_only.rsplit("/", 1)[-1]
        if basename.endswith(".html"):
            basename = basename[: -len(".html")]
        label = back_to_label if back_to_label is not None else basename
        # href_attr escapes only the attribute-internal `"` so the
        # `href` is preserved across HTML attribute parse.
        href_attr = back_to_href.replace('"', "&quot;")
        back_link_html = (
            f'<nav class="back-link">'
            f'<a href="{href_attr}">← {html.escape(label)}</a>'
            f'</nav>\n'
        )

    if now is None:
        now = datetime.now(KST).strftime("%Y-%m-%d")
    footer_issue = (
        f' · <a href="https://github.com/sh-ai-x/dev-harness-kit/issues/{p.issue}">'
        f'issue #{p.issue}</a>'
        if p.issue is not None
        else ""
    )

    # Structured before/after + pros/cons/limitations are emitted as
    # one block, prefixed by exactly one divider. When both renderers
    # return empty (legacy proposal with no new fields), NO divider is
    # added here -- the trailing divider before the footer is the
    # only one, matching the pre-extension byte shape. 3-dim reviewer
    # (PR #595): the prior version emitted two consecutive dividers in
    # the empty case, breaking byte-level backward compatibility.
    structured_html = _render_before_after(p) + _render_pros_cons_limitations(p)
    structured_prefix = (
        "\n<hr class=\"section-divider\">\n\n" + structured_html
        if structured_html
        else ""
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(p.title)}</title>\n"
        f"<style>{INLINE_CSS}</style>\n"
        "</head>\n<body>\n\n"
        f"{back_link_html}"
        f"<h1>{html.escape(p.title)}</h1>\n"
        f'<p class="meta">{_meta_line(p)}</p>\n'
        f"{tags_html}\n"
        f"{_toc(p)}\n"
        + "\n<hr class=\"section-divider\">\n\n".join(sections_html)
        + structured_prefix
        + "\n\n<hr class=\"section-divider\">\n\n"
        f'<footer>Generated {now}{footer_issue} · render via '
        f'<code>/dev-kit:proposal</code></footer>\n'
        "</body>\n</html>\n"
    )


def render_from_yaml(text: str) -> str:
    """Convenience wrapper: parse YAML text and render in one call."""
    return render(parse_proposal_yaml(text))


# --- CLI entry point --------------------------------------------------------
#
# Per the proposal-skill design (see skills/proposal/SKILL.md and
# docs/proposals/), the maintainer regenerates HTML from YAML by invoking
# this lib as a module:
#
#   python3 -m lib.render_proposal_html <main>/<sub>   # render one
#   python3 -m lib.render_proposal_html --list          # list <main>/<sub>
#   python3 -m lib.render_proposal_html --all           # render every topic
#
# Each topic lives at docs/proposals/<main>/<sub>.{yaml,html} (flat file,
# not a subdir). The leaf filename mirrors the sub-topic slug.
#
# The CLI lives in the lib (not a separate `bin/dev-kit-proposal.py`) so
# the path-traversal guard, atomic-write, and error reporting are
# colocated with the render logic.

# Topic slug (CLI argument) accepts two shapes:
#
#   2-level:  `<main>/<sub>`           (legacy flat layout -- still readable)
#   3-level:  `<bucket>/<main>/<sub>`  (status-routed layout -- the default)
#
# Both halves of `<main>/<sub>` are kebab/snake; one `/` separator per
# half is allowed; no leading/trailing slash, no double slash, no `.`
# segments. The 3-level shape requires `<bucket>` to be one of the
# three whitelist names in `BUCKETS` -- any other bucket name is
# rejected with an actionable error message.
_TWO_LEVEL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
)
_THREE_LEVEL_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
)


def _parse_topic_slug(topic: str) -> tuple[Optional[str], str, str]:
    """Parse a CLI topic arg into (bucket, main, sub).

    Returns:
        - (None, main, sub) for 2-level `<main>/<sub>` -- bucket auto-routes
          from the YAML's `status:` field via `bucket_for_status`.
        - (bucket, main, sub) for 3-level `<bucket>/<main>/<sub>` -- the
          explicit bucket wins and the YAML status is ignored.

    Raises ValueError with an actionable message for malformed slugs.
    """
    if _THREE_LEVEL_RE.fullmatch(topic):
        bucket, main, sub = topic.split("/", 2)
        if bucket not in BUCKETS:
            raise ValueError(
                f"invalid bucket {bucket!r}; must be one of {list(BUCKETS)} "
                f"(in topic {topic!r})"
            )
        return bucket, main, sub
    if _TWO_LEVEL_RE.fullmatch(topic):
        main, sub = topic.split("/", 1)
        return None, main, sub
    raise ValueError(
        f"invalid proposal topic {topic!r}: must be `<main>/<sub>` "
        f"(legacy) or `<bucket>/<main>/<sub>` (status-routed)"
    )


def _in_flight(project_root: Path) -> list[str]:
    """Return accepted proposals currently being shipped, newest started first.

    A proposal is "in flight" iff it has `status: accepted` (whether or
    not it has been routed into the `accepted/` bucket yet), has a
    `started:` date set, AND has no `shipped:` date. This is a derived
    view over data the proposal YAML already carries -- it does NOT
    move files, does NOT require a new bucket, and does NOT depend on
    git history.

    Both layouts are walked (status-routed bucket dir + legacy flat
    layout) and deduped by `(main, sub)`, mirroring `_list_proposals`.
    Pre-migration repos -- where the 16 umbrella dirs are still at the
    flat layout -- would otherwise return `(no proposals in flight)`
    for every proposal they hold (M2 reviewer, PR #804).

    Sort key: `started` desc, then `(main, sub)` lexicographic to keep
    ties deterministic. Malformed YAMLs and unreadable files are skipped
    so a single broken file does not abort the filter walk (m2
    reviewer, PR #804).
    """
    pdir = project_root / "docs" / "proposals"
    if not pdir.is_dir():
        return []
    candidates: list[tuple[str, str, str]] = []  # (started, main, sub)
    seen: set[tuple[str, str]] = set()

    # Status-routed shape first -- these win on collision.
    bucket_dir = pdir / "accepted"
    if bucket_dir.is_dir():
        for main_dir in sorted(bucket_dir.iterdir()):
            if not main_dir.is_dir():
                continue
            for sub_entry in sorted(main_dir.iterdir()):
                if not (sub_entry.is_file() and sub_entry.name.endswith(".yaml")):
                    continue
                sub = sub_entry.name[: -len(".yaml")]
                if sub in RESERVED_SLUGS:
                    continue
                if _collect_in_flight(sub_entry, main_dir.name, candidates, seen):
                    seen.add((main_dir.name, sub))

    # Legacy flat shape (skipping any bucket dir). Only emit a legacy
    # slug if no bucket copy exists for the same (main, sub).
    for entry in sorted(pdir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in BUCKETS:
            continue
        for sub_entry in sorted(entry.iterdir()):
            if not (sub_entry.is_file() and sub_entry.name.endswith(".yaml")):
                continue
            sub = sub_entry.name[: -len(".yaml")]
            if sub in RESERVED_SLUGS:
                continue
            if (entry.name, sub) in seen:
                continue
            if _collect_in_flight(sub_entry, entry.name, candidates, seen):
                seen.add((entry.name, sub))

    candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return [f"{main}/{sub}" for _started, main, sub in candidates]


def _collect_in_flight(
    sub_entry: Path,
    main: str,
    candidates: list[tuple[str, str, str]],
    seen: set[tuple[str, str]],
) -> bool:
    """Try to add one YAML to the in-flight candidates. Returns True
    when the entry was added (or considered and rejected). Swallows
    parse / IO errors so a single broken file does not abort the walk
    (m2 reviewer, PR #804)."""
    try:
        p = parse_proposal_yaml(sub_entry.read_text(encoding="utf-8"))
    except (ValueError, KeyError, yaml.YAMLError, OSError):
        return False
    if p.status != "accepted" or not p.started or p.shipped:
        return False
    candidates.append((p.started, main, sub_entry.name[: -len(".yaml")]))
    return True


def _list_proposals(project_root: Path) -> list[str]:
    """Return sorted topic slugs discovered across both layouts.

    Status-routed shape: `<bucket>/<main>/<sub>` for every `<sub>.yaml`
    directly under `docs/proposals/<bucket>/<main>/`. Walks every
    bucket in `BUCKETS`.

    Legacy flat shape: `<main>/<sub>` for every `<sub>.yaml` directly
    under `docs/proposals/<main>/` (i.e. outside any bucket dir).

    Reserved file stems (`proposal`, `index`) are skipped in BOTH
    shapes -- those are the names the previous refactors used and they
    would otherwise be mistaken for a sub-topic slug. Sub-directories
    inside a bucket (the old "one-level-per-topic" shape) are skipped.
    The sort order is `(bucket-or-empty, main, sub)`, all alphabetical.
    """
    pdir = project_root / "docs" / "proposals"
    if not pdir.exists():
        return []
    slugs: list[str] = []
    # Dedupe by (main, sub): when a topic exists in BOTH the legacy
    # flat layout AND a bucket dir, the bucket entry wins (it's the
    # SSOT after `--migrate`). The legacy entry is hidden to keep
    # `--all` from rendering the same output twice (PR #756 review).
    seen_bucketed: set[tuple[str, str]] = set()

    # Status-routed shape first -- these win on collision.
    for bucket in sorted(BUCKETS):
        bucket_dir = pdir / bucket
        if not bucket_dir.is_dir():
            continue
        for main_dir in sorted(bucket_dir.iterdir()):
            if not main_dir.is_dir():
                continue
            for sub_entry in sorted(main_dir.iterdir()):
                if not (sub_entry.is_file() and sub_entry.name.endswith(".yaml")):
                    continue
                sub = sub_entry.name[: -len(".yaml")]
                if sub in RESERVED_SLUGS:
                    continue
                seen_bucketed.add((main_dir.name, sub))
                slugs.append(f"{bucket}/{main_dir.name}/{sub}")

    # Legacy flat shape (top-level dirs only -- NOT bucket dirs).
    # Only emit a legacy slug if no bucket copy exists for the same
    # (main, sub) -- the bucket one already won.
    for entry in sorted(pdir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in BUCKETS:
            continue
        for sub_entry in sorted(entry.iterdir()):
            if not (sub_entry.is_file() and sub_entry.name.endswith(".yaml")):
                continue
            sub = sub_entry.name[: -len(".yaml")]
            if sub in RESERVED_SLUGS:
                continue
            if (entry.name, sub) in seen_bucketed:
                continue
            slugs.append(f"{entry.name}/{sub}")
    return slugs


def _migrate(project_root: Path) -> int:
    """Move legacy flat `<main>/<sub>.{yaml,html}` files into the
    bucket directory selected by each YAML's `status:` field.

    Files already under a bucket dir are left alone (the function is
    idempotent -- re-running it after a partial migration is safe).
    Files with an unrecognised `status:` are routed to `review` so a
    typo doesn't strand it in the legacy shape.

    Returns the process exit code (0 on success).
    """
    pdir = project_root / "docs" / "proposals"
    if not pdir.exists():
        return 0
    moves = 0
    for main_dir in sorted(pdir.iterdir()):
        if not main_dir.is_dir() or main_dir.name in BUCKETS:
            continue
        for sub_entry in sorted(main_dir.iterdir()):
            if not sub_entry.name.endswith((".yaml", ".html")):
                continue
            sub = sub_entry.name.rsplit(".", 1)[0]
            if sub in RESERVED_SLUGS:
                continue
            yaml_path = main_dir / f"{sub}.yaml"
            if not yaml_path.is_file():
                continue
            try:
                p = parse_proposal_yaml(yaml_path.read_text(encoding="utf-8"))
            except (ValueError, KeyError, yaml.YAMLError) as e:
                print(
                    f"warning: skipping {yaml_path}: parse error ({e}); "
                    f"leave it in the legacy layout and fix the YAML first",
                    file=sys.stderr,
                )
                continue
            bucket = bucket_for_status(p.status)
            target_dir = pdir / bucket / main_dir.name
            target_dir.mkdir(parents=True, exist_ok=True)
            for ext in (".yaml", ".html"):
                src = main_dir / f"{sub}{ext}"
                if not src.is_file():
                    continue
                dst = target_dir / f"{sub}{ext}"
                if dst.exists():
                    # Don't clobber an existing bucketed copy on a
                    # re-run; the bucketed one is the SSOT.
                    continue
                src.rename(dst)
                moves += 1
    print(f"migrated {moves} file(s) into bucket layout")
    return 0


def _render_one(project_root: Path, topic: str) -> int:
    """Render one proposal topic. Returns process exit code.

    `topic` may be either `<main>/<sub>` (legacy 2-level -- bucket is
    auto-routed from the YAML's `status:` field) or `<bucket>/<main>/<sub>`
    (3-level -- the explicit bucket wins).

    The source YAML is read from the legacy flat location OR from any
    bucket directory; the rendered HTML is written to the resolved
    bucket directory (creating it on demand).
    """
    try:
        explicit_bucket, main, sub = _parse_topic_slug(topic)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    proposals_dir = (project_root / "docs" / "proposals").resolve()
    main_dir = proposals_dir / main

    # Locate the source YAML. Bucket candidates are searched FIRST so a
    # bucketed proposal is the SSOT even when a stale legacy flat copy
    # also exists for the same (main, sub). The legacy flat is the
    # fallback for pre-migration repos only (PR #756 review).
    candidate_srcs: list[Path] = []
    if explicit_bucket is not None:
        candidate_srcs.append(proposals_dir / explicit_bucket / main / f"{sub}.yaml")
    for bucket in BUCKETS:
        candidate_srcs.append(proposals_dir / bucket / main / f"{sub}.yaml")
    candidate_srcs.append(main_dir / f"{sub}.yaml")

    src: Optional[Path] = None
    for c in candidate_srcs:
        c_resolved = c.resolve()
        if proposals_dir not in c_resolved.parents:
            continue
        if c.is_file():
            src = c
            break

    if src is None:
        # De-duplicate to give a stable "source not found" hint listing
        # only the primary candidate paths (skip duplicates when 3-level
        # topic points to the same dir as the flat layout).
        seen: set[str] = set()
        tried: list[str] = []
        for c in candidate_srcs:
            s = str(c)
            if s in seen:
                continue
            seen.add(s)
            tried.append(s)
        print(f"error: source not found: tried: {tried}", file=sys.stderr)
        print(
            f"hint: create {proposals_dir / main / f'{sub}.yaml'} "
            f"(or run --list to see existing topics)",
            file=sys.stderr,
        )
        return 1

    src_resolved = src.resolve()
    if proposals_dir not in src_resolved.parents:
        print(f"error: path traversal blocked ({topic!r})", file=sys.stderr)
        return 1

    text = src.read_text(encoding="utf-8")
    try:
        p = parse_proposal_yaml(text)
    except (ValueError, KeyError, yaml.YAMLError) as e:
        print(f"error: failed to parse {src}: {e}", file=sys.stderr)
        return 1

    # Resolve the bucket for the rendered output.
    out_bucket = explicit_bucket if explicit_bucket is not None else bucket_for_status(p.status)
    out_dir = proposals_dir / out_bucket / main
    out_resolved_check = (out_dir / f"{sub}.html").resolve()
    if proposals_dir not in out_resolved_check.parents:
        print(f"error: path traversal blocked ({topic!r})", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sub}.html"

    # Status-change workflow: when the YAML lives in a different
    # bucket than the resolved output bucket, move the YAML too so
    # YAML and HTML stay co-located after a status advance (PR #756
    # review). The HTML is about to be written at `out`; we only need
    # to relocate the YAML if its parent dir != out_dir. Skip when
    # the source is the same as the output dir (no-op).
    src_bucket = None
    for _bucket in (explicit_bucket, *BUCKETS):
        candidate = proposals_dir / (_bucket or "") / main / f"{sub}.yaml"
        if candidate == src:
            src_bucket = _bucket
            break
    if src_bucket is not None and src_bucket != out_bucket:
        new_yaml = out_dir / f"{sub}.yaml"
        new_yaml.parent.mkdir(parents=True, exist_ok=True)
        src.rename(new_yaml)
        # Re-point `src` at the new location so any later references
        # use the moved YAML.
        src = new_yaml

    # Auto-attach a "back to index" nav bar when a sibling
    # `00-index.html` exists in the OUTPUT bucket dir AND the current
    # page is not the index itself. Wiring only on the output side
    # avoids dangling links when the source dir has 00-index.yaml but
    # the sibling HTML was never rendered (PR #756 review). The
    # renderer is a pure function (no I/O) so the sibling check lives
    # in the CLI driver, not `render()`.
    back_to_href: Optional[str] = None
    if sub != "00-index":
        # Look at the actual rendered HTML on disk; fall back to
        # writing 00-index.html in the SAME render pass when the
        # sibling YAML is in the same output bucket dir.
        if (out_dir / "00-index.html").is_file():
            back_to_href = "00-index.html"
        elif (out_dir / "00-index.yaml").is_file():
            # Sibling index YAML exists but hasn't been rendered yet;
            # render it now so the back-link target resolves.
            sibling_text = (out_dir / "00-index.yaml").read_text(encoding="utf-8")
            try:
                sibling_p = parse_proposal_yaml(sibling_text)
            except (ValueError, KeyError, yaml.YAMLError) as e:
                print(
                    f"warning: 00-index sibling for {sub!r} failed to parse ({e}); "
                    f"back-link suppressed for this render",
                    file=sys.stderr,
                )
            else:
                sibling_html = render(sibling_p, back_to_href=None)
                atomic_write_text(out_dir / "00-index.html", sibling_html)
                back_to_href = "00-index.html"

    html_doc = render(p, back_to_href=back_to_href)

    atomic_write_text(out, html_doc)
    size_kb = len(html_doc.encode("utf-8")) / 1024
    print(
        f"wrote {out} ({size_kb:.1f} KB, source: {src.relative_to(proposals_dir)}, "
        f"bucket: {out_bucket})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lib.render_proposal_html",
        description=(
            "Render docs/proposals/<bucket>/<main>/<sub>.yaml to "
            "docs/proposals/<bucket>/<main>/<sub>.html "
            "(bucket auto-routes from YAML `status:`; legacy "
            "<main>/<sub> still readable)."
        ),
    )
    parser.add_argument(
        "topic",
        nargs="?",
        help=(
            "topic slug `<main>/<sub>` (legacy) or "
            "`<bucket>/<main>/<sub>` (status-routed, where bucket is "
            "review|accepted|rejected)"
        ),
    )
    parser.add_argument(
        "--project-root", default=".",
        help="project root (default: cwd)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list available proposal topics and exit",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="render every proposal topic and exit",
    )
    parser.add_argument(
        "--migrate", action="store_true",
        help=(
            "one-shot: move legacy flat `<main>/<sub>.{yaml,html}` "
            "files into the bucket dir matching each YAML's `status:`"
        ),
    )
    parser.add_argument(
        "--in-flight", action="store_true",
        help=(
            "list accepted proposals whose `started:` is set and "
            "`shipped:` is unset (currently being implemented), "
            "newest started first; the same data the timeline chips "
            "in the rendered HTML show, surfaced as a flat list"
        ),
    )
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()

    if args.migrate:
        return _migrate(root)

    if args.in_flight:
        names = _in_flight(root)
        if not names:
            print("(no proposals in flight)")
            return 0
        for n in names:
            print(n)
        return 0

    if args.list:
        names = _list_proposals(root)
        if not names:
            print("(no proposals found under docs/proposals/)")
            return 0
        for n in names:
            print(n)
        return 0

    if args.all:
        names = _list_proposals(root)
        if not names:
            print("no proposals found", file=sys.stderr)
            return 1
        for n in names:
            _render_one(root, n)
        return 0

    if not args.topic:
        parser.error(
            "proposal topic required (or pass --list to see available)"
        )
    return _render_one(root, args.topic)


if __name__ == "__main__":
    sys.exit(main())
