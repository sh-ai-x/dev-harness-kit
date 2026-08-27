---
name: proposal
category: design
description: 0-arg HTML renderer for design proposals / plans. Renders any docs/proposals/<main>/<sub>.yaml to docs/proposals/<main>/<sub>.html for pre-implementation review, with structured before / after + pros / cons / limitations analysis.
alpha: state
when_to_use: |
  - User types /dev-kit:proposal
  - User wants to share a draft proposal/plan with reviewers before implementation
  - User wants to view an existing proposal as a single self-contained HTML doc
  - Plan stage (Gate 5/5 emit) auto-invokes this skill to render the design record
  - User wants the YAML → HTML renderer to emit a structured before/after + pros/cons/limitations analysis of the proposed code change
allowed-tools: Read Write Bash
model: sonnet
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:proposal -- design proposal HTML viewer

Renders any `docs/proposals/<main>/<sub>.yaml` to a single self-contained
HTML document at `docs/proposals/<main>/<sub>.html`. The skill is
generic across proposals; the MCP harness content (issue #280) is one example
input, not the skill's purpose.

**Layout invariant**: every proposal lives at
`docs/proposals/<main>/<sub>.{yaml,html}`.

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
and on any static-site host). The relative-path safety check
specifically allows bare relative paths and `../<sibling>.html` for
cross-document links; the dangerous schemes (`javascript:`, `data:`,
`vbscript:`, `file:`) are still rejected.

**Back-to-index nav**: the renderer's CLI auto-attaches a
`<nav class="back-link">` element at the top of every non-index
sub-topic page (rendered as `← 00-index` linking to `00-index.html`)
when a sibling `00-index.yaml` exists in the same umbrella dir. The
00-index page itself gets no back link (it IS the index). The pure
function `render()` takes optional `back_to_href=` and
`back_to_label=` kwargs; the CLI driver wires them based on the
filesystem sibling check.

**Why a separate skill, not a flag on `/dev-kit:plan`**: the
user has to remember the flag and slash autocomplete does not surface flags.
Proposals are a distinct artifact (pre-implementation plans) with a distinct
lifecycle (designed → reviewed → accepted/rejected → implemented). The slash
is the entrypoint; the YAML→HTML render is the work.

## What it does

1. List available proposal topics: `python3 -m lib.render_proposal_html --list`
2. Render one: `python3 -m lib.render_proposal_html <main>/<sub>` writes
   `docs/proposals/<main>/<sub>.html`
3. Print the file path so the user can open it in a browser
   (`open docs/proposals/<main>/<sub>.html` on macOS, or any browser
   via `file://`).
4. Stop. The skill does not edit the YAML -- the user authors the proposal;
   this skill renders.

The render logic lives in `lib/render_proposal_html.py` (pure function) plus a
`__main__` CLI entry (`python3 -m lib.render_proposal_html`). No separate
`bin/dev-kit-proposal.py` -- see the "Architecture" section below.

## Workflow (BEFORE / AFTER)

The skill **prescribes** a **before-then-after** authoring discipline. A
proposal is a contract between the existing code and the change being
proposed; reviewers benefit when both sides are present and citable.

The renderer does NOT enforce this discipline. The parser accepts any
proposal whose YAML matches the schema — including proposals that omit
`before:` and `after:` entirely. The §Workflow section below describes
the recommended discipline; §Limitations is honest about what the
parser does and does not catch. Reviewers are the enforcement mechanism.

### Before — analyze the existing code

The maintainer (or the model writing on the maintainer's behalf) is
**expected to** read the existing code that the proposal will touch
and capture concrete observations before authoring the YAML:

- Which file(s) currently implement the behavior the proposal changes?
- What does that code do today, and where does it fall short?
- Cite evidence: file:line, commit hash, log excerpt, or test output.
  No unsourced "currently broken" claims.

The result of this pass is captured in the YAML's `before:` block:

```yaml
before:
  summary: |
    No doom-loop detection exists today.
  evidence:
    - '12-18% of long sessions have 3+ identical Bash calls'
    - 'See `logs/claude-code/*.jsonl`'
```

### After — describe the proposed state

After the "before" analysis, the maintainer writes the `after:` block:

```yaml
after:
  summary: |
    A new hook reads the last 10 entries of the hand-off log and emits
    a UserPromptSubmit injection on the 3rd identical call.
  files:
    - path: hooks/lib/loop-detect.sh
      change: |
        Reads `.dev-kit/hand-off/<session>.log`; emits injection.
    - path: hooks/index.md
      change: 'register loop-detect.sh in the matrix'
```

The `files` list is a **reviewer commitment**. Anything not listed MUST
NOT change. The maintainer is responsible for keeping this list narrow
and honest; reviewers check it during acceptance.

### Pros, cons, limitations

In the same draft, the maintainer MUST capture:

```yaml
pros:
  - 'Catches silent doom loops that burn 5-10k tokens before timeout'
  - 'Additive only (no existing flow changes)'
cons:
  - 'False positives on legitimate retries (mitigation: 3 identical *input*, not 3 same tool)'
limitations:
  - 'Cannot detect slow-think loops (3 calls spread across minutes)'
```

- **Pros** = strengths the change brings. Reviewer uses this to confirm
  the gain is real and cited.
- **Cons** = known weaknesses. NOT scope-cut items; items the proposal
  knowingly accepts. Reviewer uses this to confirm the trade-off was
  made consciously.
- **Limitations** = what the proposal CANNOT do (out-of-scope-by-design,
  not "we didn't get to it"). These are facts about the design, not
  the implementation.

The renderer draws each list with a distinct visual cue (check / ballot-x
/ warn-glyph) so reviewers can scan the trade-off shape at a glance.

### When the structured fields are NOT used

Proposals authored before this extension have only `sections:`. The
skill MUST still render them correctly. The renderer is backward
compatible: when `before:`, `after:`, `pros:`, `cons:`, `limitations:`
are absent, the HTML is identical to the pre-extension shape except for
an extended inline-CSS block. No section wrappers are emitted. Pin:
`tests/test_proposal_skill.py::BeforeAfterRenderTests::test_render_no_fields_emits_no_ba_sections`.

## Output (in chat)

```
## /dev-kit:proposal -- <main>/<sub>

**Source**: docs/proposals/<main>/<sub>.yaml
**Output**: docs/proposals/<main>/<sub>.html (one self-contained HTML doc, inline CSS only, no JS, dark-mode aware)
**Status**: <status from YAML frontmatter>
**Sections**: <count>

**Open in browser**: `open docs/proposals/<main>/<sub>.html` (macOS)
```

## Authoring a proposal

Create `docs/proposals/<main>/<sub>.yaml` with this shape:

```yaml
title: <one-line title>
status: draft | design-discussion | ready-for-review | accepted | rejected | superseded
issue: <issue number, optional>
date: YYYY-MM-DD
tags: [<tag1>, <tag2>]

# Structured before / after + pros / cons / limitations -- all optional.
# When absent the proposal renders exactly as before. See §Workflow.
before:
  summary: |
    Markdown-lite description of the code's CURRENT state.
  evidence:
    - 'file:line, log excerpt, or commit hash supporting the claim'
after:
  summary: |
    Markdown-lite description of the code's PROPOSED state.
  files:
    - path: <repo-relative file path>
      change: |
        Markdown-lite description of what this file becomes.
pros:
  - 'Strength 1 with citation'
  - 'Strength 2'
cons:
  - 'Weakness the proposal knowingly accepts + mitigation'
limitations:
  - 'What the design CANNOT do (out-of-scope-by-design)'

sections:
  - title: <section 1>
    body: |
      Markdown-lite body. Supports:

      - # ## ### headings
      - paragraphs
      - **bold**, *italic*, `code`
      - [link text](https://...)
      - [cross-doc link](<sub>.html) -- bare relative paths and
        `../<sibling>.html` are both allowed
      - unordered (- ) and ordered (1. ) lists
      - | GFM tables |
      - ``` fenced code blocks ```
      - > blockquotes
      - --- horizontal rules
  - title: <section 2>
    body: |
      ...
```

Then run `/dev-kit:proposal <main>/<sub>` to render. The topic slug must
match `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`
(one `/` separator; no leading/trailing slash; no `.` segments). The
filenames must not collide with the reserved legacy canonical names
`proposal.yaml` and `index.yaml` -- if they do, the renderer treats
the file as a leftover from a previous refactor and skips it.

### Cross-references between proposals

Inside a body, link to another proposal in the same umbrella as
`[label](<other-sub>.html)`. From a proposal at `<main>/<sub>.html`,
the relative hop to a sibling is the bare `<other-sub>.html` (same
directory under `<main>/`). The 00-index page follows this convention
-- the 12 sub-topic links in its table read as `[label](<sub>.html)`
and resolve to siblings of the index.

Cross-umbrella links (rare) would use `../<other-main>/<sub>.html`.
The proposal skill does not enforce a single umbrella -- each
`<main>/<sub>` pair is independent at the filesystem level.

## Why this is `alpha: state`

Per CLAUDE.md Iron Law L6, every new skill must declare an `alpha:` field. The
proposal artifact has **stateful lifecycle**: a YAML source on disk, a derived
HTML rendered from it, a status tag (draft → design-discussion →
ready-for-review → accepted/rejected/superseded) that the maintainer advances
over time. That is `state` by definition -- the skill persists a proposal
artifact and gates its progression, distinct from analysis (reasoning over a
corpus) and enforcement (deterministic guards).

**L7 fit**: the deterministic render + status-tag + HTML-escape contract is
exactly the part next-gen models can't self-impose. The model can reason
about *whether* a proposal should be accepted; the skill owns *how* the
artifact is rendered, versioned, and surfaced for review.

## Iron Law

**Defensive HTML escaping on every interpolated value.** The renderer escapes
titles, anchors, free-text fields, and link URLs. A title with `<script>` in
it renders as `&lt;script&gt;` -- the browser never executes it. The contract
is enforced by the `HtmlEscapeTests` class in `tests/test_proposal_skill.py`
(e.g. `test_script_in_title_escaped`, `test_script_in_body_escaped`,
`test_link_href_escapes_quotes`, `test_ampersand_escaped`,
`test_less_than_greater_than_escaped`).

**No `<script>` tag, no external assets, inline CSS only.** The output is
safe to email, archive, or open from `file://`.

## Editing the proposal

The YAML is hand-edited, not generated. Re-run
`/dev-kit:proposal <main>/<sub>` (or `python3 -m lib.render_proposal_html
<main>/<sub>`) to refresh the HTML.

## Related

- `lib/render_proposal_html.py` -- pure function: `parse_proposal_yaml` +
  `render` + `__main__` CLI entry
- `lib/render_report_html.py` -- sibling renderer (eval + inspect reports)
- `bin/dev-kit-report.py` -- sibling CLI driver (kept as-is; this skill no
  longer uses this pattern). The underlying `/dev-kit:report` slash was
  removed but the lib + driver remain.
- `skills/plan/SKILL.md` -- Gate 5/5 calls this skill to auto-render the
  design record

## Architecture

This skill deviates from the project's typical read-only-skill +
`bin/dev-kit-*` CLI pattern. The proposal skill has Write permission and
invokes `python3 -m lib.render_proposal_html <topic>` directly. The CLI
logic lives in the lib's `__main__` block. Rationale: the proposal skill is
the only caller, the maintainer workflow is "edit YAML, regenerate HTML",
and a separate binary added indirection without adding capability.
- `docs/proposals/<main>/<sub>.{yaml,html}` -- per-sub-topic flat files
  under an umbrella; the leaf is named after the sub-topic (not
  `index.{yaml,html}`) so the file is recognisable on a flat
  directory listing and on a static-site host
- Issue #280 -- the MCP harness analysis (12 sub-topics + 00-index under
  the `harness-architecture` umbrella) is the first proposal authored
  against this skill

## Pros of this skill

- **Structured before/after analysis forces honest proposals.** The
  YAML declares `before.evidence` and `after.files` slots so the
  maintainer is nudged to cite file:line, log excerpts, or commit
  hashes; a reviewer can verify both sides with concrete references.
  The shape of the evidence is enforced by the parser (it must be a
  list of strings), but the *quality* of the citations is not — see
  §Limitations.
- **Pros / Cons / Limitations are visually distinct in the rendered
  HTML.** The three lists use check (--ok), ballot-x (--bad), and
  warn-glyph (--warn) cues, so a reviewer can scan the trade-off shape
  in one glance instead of parsing prose.
- **Deterministic renderer.** `render(p, now=...)` is byte-identical
  across runs. Two reviewers opening the same proposal see the same
  document. The state machine `render() → atomic_write_text()` makes
  partial writes impossible.
- **Inline-CSS-only output.** No `<script>`, no remote `<link>`, no
  remote `<img>`. Safe to email, archive, or open from `file://`.
  HTML-escape on every interpolated value keeps a `<script>` in YAML
  from ever being executable in the browser.
- **Backward compatible.** Existing proposals without the new fields
  render exactly as before (only the inline-CSS block grows; no new
  section wrappers are emitted). The migration is opt-in per file.
- **Files-list is a reviewer commitment.** Anything not in
  `after.files` MUST NOT change. This prevents "scope creep" PRs that
  silently edit code the proposal didn't discuss.

## Cons of this skill

- **Markdown-lite grammar is intentionally narrow.** Headings stop at
  H3, no nested lists, no definition lists, no footnotes, no images,
  no HTML pass-through. A proposal that wants any of those has to
  extend `lib/render_proposal_html.py::_is_block_start` + a
  corresponding block detector in `render_body`. Migration cost is
  paid per construct.
- **Authoring burden is higher than a free-form document.** A
  maintainer who just wants to jot "should we add X?" must still
  produce structured `before:` / `after:` / `pros:` / `cons:` /
  `limitations:` blocks. For very early drafts (status: `draft`) this
  can feel like premature ceremony; the workaround is to leave the
  new fields empty until the proposal matures to `design-discussion`
  or later.
- **No diff between `before:` and `after:`.** The renderer shows them
  side-by-side, but it does NOT generate a syntactic diff. The
  reviewer has to read both halves and compare. A future enhancement
  could render the file list as a tree-diff via `git diff` against
  the proposal commit, but that's out of scope today.
- **Single file per topic.** Cross-references between proposals work
  via `<sub>.html` relative links, but the skill has no notion of a
  "supersedes" relationship at the YAML level (it tracks that via the
  `superseded` status tag, which is hand-set by the maintainer).
- **CLI driver lives in the lib's `__main__` block.** This deviates
  from the project's typical `bin/dev-kit-*.py` pattern. New
  contributors may look for a `bin/dev-kit-proposal.py` and not find
  one; the SKILL.md §Architecture section calls this out, but it is a
  trip-hazard.

## Limitations of this skill

- **Cannot detect code-analysis shortcuts.** The skill formats the
  `before.evidence` block as the maintainer writes it. If the
  maintainer hand-waves ("looks broken to me") without file:line or
  log citation, the renderer does not reject it. The lint enforces
  the SHAPE (a list of strings), not the QUALITY of the citations.
  Reviewers must catch shallow evidence during acceptance.
- **Cannot enforce that `after.files` is actually what the
  implementation PR changes.** A proposal can list 2 files; the PR
  can touch 5. The skill can render the divergence but cannot block
  it. The hand-off contract is the implementation PR body citing the
  proposal's `issue:` number.
- **Limitations list is decorative, not enforced.** The skill renders
  `<ul class="limitations-list">` with a warn-glyph, but it does not
  check that any implementation comment, test, or docs acknowledge
  the listed limitations. The reviewer must.
- **No versioning of the proposal itself.** A single
  `docs/proposals/<main>/<sub>.yaml` is the SSOT; there is no
  per-revision history beyond git's. A proposal that evolves through
  multiple review rounds is git history, not a structured changelog.
  If round-by-round audit trail becomes important, the proposal must
  be re-authored into a new sub-topic (`02-...`).
- **Skill workflow cannot enforce that the maintainer ACTUALLY read
  the existing code before writing `before:`.** The skill's job is
  render-only. The discipline is documented in §Workflow and called
  out in PR review, but the lint does not (and cannot) detect a
  fabricated `evidence:` list.
- **The renderer is single-pass.** It does not pre-compute a tree
  diff, does not fetch referenced external links, does not validate
  that the `after.files` paths actually exist in the repo (yet). All
  three are future-work; the skill is honest about the gap rather
  than papering over it.

## Hand-off

Next: open `docs/proposals/<main>/<sub>.html` in a browser, share the
file with reviewers, then update the YAML's `status:` field as the proposal
progresses through review.

After a proposal moves to `status: accepted`, the implementation work follows
`/dev-kit:plan` → `/dev-kit:build`. The proposal itself is the design record
that closes the issue.

**Auto-invoked by `/dev-kit:plan` Gate 5/5 (emit)**: when the plan skill
finishes a 5-gate interview it writes a proposal YAML derived from the PRD
and calls this skill to render the HTML. The topic slug is derived from
the phase name (see `skills/plan/SKILL.md` Gate 5/5). The hand-off chain
becomes `plan → proposal (this skill) → build`.
