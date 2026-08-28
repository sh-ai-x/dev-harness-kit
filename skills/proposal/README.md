# /dev-kit:proposal — Skill README

> Render a hand-authored `docs/proposals/<bucket>/<main>/<sub>.yaml` proposal
> (or legacy flat `docs/proposals/<main>/<sub>.yaml`) into a single
> self-contained HTML document for pre-implementation review and sharing.
> Slash command: `/dev-kit:proposal`.

## What this skill does

Renders any YAML file under `docs/proposals/` into a sibling `<sub>.html`
alongside it. The bucket (`review` / `accepted` / `rejected`) is
auto-routed from the YAML's `status:` field. The HTML is:

- **Self-contained** — inline CSS only, no `<script>`, no external
  `<link rel="stylesheet">`, no remote `<img>`. Safe to email, archive, or
  open directly from `file://`.
- **Dark-mode aware** — uses `prefers-color-scheme` so the same file reads
  correctly in light and dark browser themes.
- **HTML-escaped** — every interpolated value (title, body, link URL,
  frontmatter fields) passes through `html.escape`. A `<script>` in YAML
  renders as `&lt;script&gt;`; the browser never executes it.
- **URL-scheme allowlisted** — only `https://`, `http://`, and `mailto:`
  links become anchors. `javascript:`, `data:`, `vbscript:`, `file:`,
  and bare relative paths render as plain text with the scheme shown
  in parentheses.

The skill does **not** edit the YAML. The user authors the proposal;
this skill renders and writes the HTML.

## Workflow (BEFORE / AFTER)

The skill **prescribes** a **before-then-after** authoring discipline. A
proposal is a contract between the existing code and the change being
proposed; reviewers benefit when both sides are present and citable.

The renderer does NOT enforce this discipline — the parser accepts any
proposal whose YAML matches the schema, including those that omit
`before:` and `after:` entirely. The §Workflow describes the
recommended discipline; §Limitations is honest about what the parser
does and does not catch. Reviewers are the enforcement mechanism.

**BEFORE** — analyze the existing code first. Read the file(s) the
proposal will touch, capture concrete observations (file:line, commit
hash, log excerpt, test output), and write them into the YAML's
`before:` block as `summary` + `evidence` items.

**AFTER** — describe the proposed state. Write the `after:` block as
`summary` + a `files` list. The `files` list is a reviewer commitment:
anything not in it MUST NOT change.

**PROS / CONS / LIMITATIONS** — capture in the same draft. Pros are
cited strengths; cons are knowingly accepted weaknesses with a
mitigation; limitations are what the design CANNOT do (out-of-scope by
design, not "we didn't get to it").

When any of `before:`, `after:`, `pros:`, `cons:`, `limitations:` are
absent, the renderer emits no new section wrappers (only an extended
inline-CSS block). Existing proposals render identically. See
`skills/proposal/SKILL.md` §Workflow for the full rule.

## Why a separate skill (not a flag on `/dev-kit:plan`)

The user typed `/dev-kit:proposal` and got a single result. The flag-vs-slash
choice is the architecture:

- Proposals are a **distinct artifact** (pre-implementation design records)
  with a **distinct lifecycle** — `draft` → `design-discussion` →
  `ready-for-review` → `accepted` / `rejected` / `superseded`.
- Slash autocomplete does not surface flags. A `proposal` flag on
  `/dev-kit:plan` would be invisible at the moment of invocation.
- The render output is the **handoff artifact** itself — share the HTML
  file with reviewers, archive it in the repo, link it from the issue.

The pattern is the same as `/dev-kit:llm-refresh`: a domain-specific
lifecycle persisted in versioned files, with a deterministic render.

## File layout

```
skills/proposal/
├── SKILL.md                 # slash command frontmatter + body
├── README.md                # this file
└── (no scripts/ — CLI lives in lib/render_proposal_html.py)

lib/
└── render_proposal_html.py  # pure renderer + __main__ CLI entry point

docs/proposals/
├── review/                  # status: draft, design-discussion, ready-for-review
│   └── <main>/<sub>.{yaml,html}
├── accepted/                # status: accepted
│   └── <main>/<sub>.{yaml,html}
└── rejected/                # status: rejected, superseded
    └── <main>/<sub>.{yaml,html}

tests/
└── test_proposal_skill.py   # parse + render + escape + bucket-routing tests
```

The proposal skill **deviates** from the project's typical skill pattern
(`read-only-skill` + `bin/dev-kit-*.py` CLI driver):

- The proposal skill has `Write` permission (it writes the HTML).
- The CLI lives in `lib/render_proposal_html.py`'s `__main__` block,
  not in a separate `bin/dev-kit-proposal.py`.
- The skill invokes `python3 -m lib.render_proposal_html <topic>` directly.

Rationale (from `skills/proposal/SKILL.md` §Architecture): the proposal
skill is the only caller, the maintainer workflow is *edit YAML, regenerate
HTML*, and a separate binary added indirection without adding capability.
The path-traversal guard, atomic-write, and error reporting are colocated
with the render logic.

## Invocation

### Slash command (human)

```
/dev-kit:proposal <main>/<sub>            # render one (bucket auto-routes from YAML status)
/dev-kit:proposal accepted/<main>/<sub>   # render one with explicit bucket override
/dev-kit:proposal --list                  # list available proposals
/dev-kit:proposal --all                   # render every proposal
/dev-kit:proposal --migrate               # one-shot move legacy flat proposals into bucket dirs
```

`<bucket>` is one of `review`, `accepted`, `rejected` -- the CLI picks
the bucket from the file's `status:` field by default, but accepts an
explicit override. The legacy 2-level `<main>/<sub>` form is still
accepted for backward compatibility (CLI scans both shapes when listing,
renders to the status-routed shape when writing).

### Direct CLI (debug + scripting)

```bash
# from the repo root
python3 -m lib.render_proposal_html main/alpha                   # auto-route by status
python3 -m lib.render_proposal_html accepted/main/alpha          # explicit bucket
python3 -m lib.render_proposal_html --list                       # list across all 3 buckets + legacy
python3 -m lib.render_proposal_html --all                        # render every topic
python3 -m lib.render_proposal_html --migrate                    # legacy -> bucket dirs
python3 -m lib.render_proposal_html my-slug --project-root /path/to/repo
```

The renderer is a **pure function** with a thin I/O wrapper. The two
testable entry points are:

```python
from lib.render_proposal_html import render_from_yaml, render, parse_proposal_yaml

html = render_from_yaml(yaml_text)              # parse + render
p = parse_proposal_yaml(yaml_text)              # value object only
html = render(p, now="2026-07-23")              # pass fixed `now` for deterministic output
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | render succeeded; `--list` returns 0 even when no proposals are found (prints `(no proposals found under docs/proposals/)`); `--migrate` returns 0 even when there is nothing to move |
| 1 | invalid topic (must match `<main>/<sub>` or `<bucket>/<main>/<sub>`), source not found, YAML parse failure, path-traversal blocked, `--all` with no proposals |

## Output (in chat)

```
## /dev-kit:proposal -- <bucket>/<main>/<sub>

**Source**: docs/proposals/<bucket>/<main>/<sub>.yaml
**Output**: docs/proposals/<bucket>/<main>/<sub>.html (one self-contained HTML doc, inline CSS only, no JS, dark-mode aware)
**Status**: <status from YAML frontmatter>
**Bucket**: <review|accepted|rejected, auto-routed from `status:`>
**Sections**: <count>

**Open in browser**: `open docs/proposals/<bucket>/<main>/<sub>.html` (macOS)
```

The output file is the deliverable. Open it directly with
`open docs/proposals/<bucket>/<main>/<sub>.html` on macOS, or any browser
via `file://`.

## Authoring a proposal

Create `docs/proposals/<name>.yaml` with this shape:

```yaml
title: <one-line title>
status: draft | design-discussion | ready-for-review | accepted | rejected | superseded
issue: <issue number, optional>
date: YYYY-MM-DD
tags: [<tag1>, <tag2>]

# Structured before / after + pros / cons / limitations -- all optional.
# See §Workflow for the discipline these enforce.
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
      - unordered (- ) and ordered (1. ) lists
      - | GFM tables |
      - ``` fenced code blocks ```
      - > blockquotes
      - --- horizontal rules
  - title: <section 2>
    body: |
      ...
```

Required top-level fields: `title`, `status`. Optional: `issue` (int),
`date` (str), `tags` (list[str]), `sections` (list of `{title, body}`),
`before` (`{summary, evidence}`), `after` (`{summary, files}`), `pros`
(list[str]), `cons` (list[str]), `limitations` (list[str]).
Validation lives in `lib/render_proposal_html.py::parse_proposal_yaml`
and `tests/test_proposal_skill.py::ParseYAMLTests` and
`::BeforeAfterFieldsTests`.

### Status field lifecycle

| Status | Tag class | Use for |
|---|---|---|
| `draft` | `tag-warn` | Initial outline, not yet ready for review |
| `design-discussion` | `tag-info` | Open for comment on approach |
| `ready-for-review` | `tag-info` | Complete, awaiting reviewer verdict |
| `accepted` | `tag-ok` | Approved — implementation follows via `/dev-kit:plan` → `/dev-kit:build` |
| `rejected` | `tag-bad` | Decided against; record kept for context |
| `superseded` | `tag-warn` | Replaced by a later proposal (link it in `tags` or `sections`) |

Unknown statuses fall back to `tag-info` (visible in
`ParseYAMLTests::test_status_class_unknown_falls_back_to_info`).

## Markdown-lite grammar

The body renderer is intentionally narrow — see
`lib/render_proposal_html.py::render_body` for the exact block-detector
state machine. Supported inline + block constructs:

| Construct | Syntax | Notes |
|---|---|---|
| Heading | `# H1`, `## H2`, `### H3` | No H4+ |
| Paragraph | blank-line separated text | Auto-collected |
| Bold | `**text**` | |
| Italic | `*text*` | `*` is NOT a list bullet; only `-` is |
| Inline code | `` `text` `` | |
| Link | `[label](https://...)` | Only `https?`, `mailto` produce anchors |
| Unordered list | `- item` | `*` is rejected to keep bold/italic unambiguous |
| Ordered list | `1. item` | |
| GFM table | `\| col \| col \|` + `\|---\|---\|` | Contiguous pipe-delimited lines |
| Fenced code | ` ```lang ... ``` ` | Lang flows into `class="language-..."` |
| Blockquote | `> text` | |
| Horizontal rule | `---` | At least 3 dashes |

**Forward-progress safety** — the `render_body` state machine forces
forward progress on any line that no block branch matched, so even
malformed input terminates. The earlier infinite-loop bug on
`**bold** at start of line` is covered by
`RenderBodyTests::test_bold_at_start_of_line_is_inline_not_block` and
`test_paragraph_terminates`.

## Why this is `alpha: state`

Per CLAUDE.md Iron Law L6, every new skill must declare `alpha:`. The
proposal artifact has a **stateful lifecycle**:

- A YAML source on disk is the SSOT.
- HTML is **derived state** regenerated from YAML.
- The `status:` field is a **state machine** that the maintainer advances
  over time (`draft` → `design-discussion` → `ready-for-review` → …).
- The skill renders the artifact and gates its progression.

That is `state` by definition — distinct from `analysis` (reasoning over a
corpus) and `enforcement` (deterministic guards). The skill persists a
proposal artifact and gates its progression. **L7 fit**: the deterministic
render + URL-scheme allowlist + HTML-escape contract is exactly the part
next-gen models can't self-impose. The model can reason about *whether*
a proposal should be accepted; the skill owns *how* the artifact is
rendered, versioned, and surfaced for review.

## Trust model

- **Self-authored, never untrusted input.** Proposals are hand-edited by
  maintainers and reviewed via PRs. The renderer's security layer (HTML
  escape, URL-scheme allowlist) is a **defense-in-depth** against a
  malicious author, not the primary trust anchor.
- **No `<script>` ever, anywhere.** Output is safe to email, archive, or
  open from `file://`. `OutputInvariantsTests::test_no_script_tag_in_output`
  enforces this. The `INLINE_CSS` constant is the only CSS source.
- **URL-scheme allowlist.** `javascript:`, `data:`, `vbscript:`, `file:`,
  and bare relative paths are rejected at render time. The proposal HTML
  is meant to be safe-to-open from `file://`; allowing `file:` links would
  defeat that. See `RenderBodyTests::test_link_*_scheme_rejected`.
- **Atomic write.** `lib/render_proposal_html.py::_render_one` writes via
  `lib.atomic.atomic_write_text` so a partial render cannot leave a
  half-written HTML on disk.
- **Path-traversal guard.** The name argument is matched against
  `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` and the resolved paths are checked
  to lie under `docs/proposals/`. A relative-path argument cannot escape
  the proposals directory.
- **Deterministic when `now` is fixed.** Pass `render(p, now="YYYY-MM-DD")`
  for byte-identical output across runs. Default uses today's KST date —
  `test_render_is_deterministic_when_now_is_fixed` and
  `test_render_default_now_is_today` pin both.

## How to add a new proposal

1. Create `docs/proposals/<bucket>/<main>/<slug>.yaml` with the shape
   above. The CLI auto-routes into the bucket each YAML's `status:`
   declares — for a fresh draft, place the file under
   `docs/proposals/review/<main>/<slug>.yaml` (or let the CLI place it
   there when you render).
2. Run `/dev-kit:proposal <main>/<slug>` (auto-routes) or
   `/dev-kit:proposal <bucket>/<main>/<slug>` (explicit).
3. Open `docs/proposals/<bucket>/<main>/<slug>.html` in a browser and
   review.
4. Commit both `.yaml` and `.html` — the HTML is the shareable artifact
   (viewable offline from `file://`).
5. Update the `status:` field as the proposal progresses through review.
   Re-run `/dev-kit:proposal <main>/<slug>` after each edit; the CLI
   moves the file to the new bucket on every render.

## How to handle a vendor pattern change

The renderer's block grammar is intentionally narrow. If a future
proposal needs a construct not yet supported (e.g. nested lists,
definition lists, footnotes):

1. Extend `lib/render_proposal_html.py::render_body` and add the
   corresponding block detector to `_is_block_start`.
2. Add a `RenderBodyTests` case covering the new construct.
3. Add a release note in the proposal's body — the renderer is the
   rendering contract, not a generic Markdown implementation.

## Hand-off

After a proposal moves to `status: accepted`, the implementation work follows
`/dev-kit:plan` → `/dev-kit:build`. The proposal HTML is the design record
that closes the issue; the implementation PR references the proposal's
`issue:` number for traceability.

The proposal skill is intentionally **read-only** with respect to the YAML.
Editing the YAML is the maintainer's responsibility; the skill only renders.

## Related files

- `skills/proposal/SKILL.md` — slash command frontmatter + body.
- `lib/render_proposal_html.py` — pure renderer + `__main__` CLI entry.
- `lib/render_report_html.py` — sibling renderer (eval + inspect reports).
- `bin/dev-kit-report.py` — sibling CLI driver pattern (kept as-is; this
  skill deviated from it intentionally). Underlying `/dev-kit:report` slash
  was removed but the lib + driver remain.
- `skills/llm-refresh/README.md` — closest sibling in skill README structure.
- `tests/test_proposal_skill.py` — parse + render + escape + invariants.
- `tests/test_render_report_html.py` — sibling renderer test contract.
- `docs/proposals/` — proposal source/output directory.
- `rules/skill-authoring.md` — L6 skill gate that this skill satisfies with
  `alpha: state` declared on `skills/proposal/SKILL.md:5`.

## Why this skill exists

Without `/dev-kit:proposal`, each proposal would be hand-edited as HTML
(or pasted into a generic Markdown service that introduces its own
scripting risk). The deterministic `lib/render_proposal_html.py` renderer
plus the `status:` state machine plus the inline-CSS-only output is the
single edit point: a maintainer edits YAML, regenerates HTML, and shares
the file. The skill is intentionally narrow, deterministic, and filesystem-
scoped so a silent vulnerability is impossible.

## Pros of this skill

- **Structured before/after analysis forces honest proposals.** The
  YAML declares `before.evidence` and `after.files` slots so the
  maintainer is nudged to cite file:line, log excerpts, or commit
  hashes; a reviewer can verify both sides with concrete references.
  The shape of the evidence is enforced by the parser (it must be a
  list of strings), but the *quality* of the citations is not — see
  §Limitations.
- **Pros / Cons / Limitations visually distinct** in the rendered HTML
  (check / ballot-x / warn-glyph), so reviewers can scan the
  trade-off shape at a glance.
- **Deterministic renderer.** `render(p, now=...)` is byte-identical
  across runs. `atomic_write_text()` makes partial writes impossible.
- **Inline-CSS-only output.** No `<script>`, no remote `<link>`, no
  remote `<img>`. Safe to email, archive, or open from `file://`.
  HTML-escape on every interpolated value keeps a `<script>` in YAML
  from ever being executable in the browser.
- **Backward compatible.** Existing proposals without the new fields
  render exactly as before (only the inline-CSS block grows; no new
  section wrappers are emitted).
- **`after.files` is a reviewer commitment.** Anything not listed
  MUST NOT change — prevents scope-creep PRs.

## Cons of this skill

- **Markdown-lite grammar is intentionally narrow.** No H4+, nested
  lists, definition lists, footnotes, images, or HTML pass-through.
  Adding a new construct requires editing
  `lib/render_proposal_html.py::_is_block_start` and `render_body`,
  plus a corresponding test in `tests/test_proposal_skill.py`.
- **Higher authoring burden than a free-form document.** Early
  `draft` proposals may feel like premature ceremony; the workaround
  is to leave the new fields empty until status advances to
  `design-discussion` or later.
- **No diff between `before:` and `after:`.** Side-by-side rendering
  only; reviewers read both halves and compare themselves.
- **Single file per topic.** Supersedes-tracking is the `superseded`
  status tag (hand-set), not a YAML-level relationship field.
- **CLI driver lives in the lib's `__main__` block.** Deviates from
  the project's typical `bin/dev-kit-*.py` pattern; new contributors
  may look for a separate binary.

## Limitations of this skill

- **Cannot detect code-analysis shortcuts.** The lint enforces the
  SHAPE of `before.evidence` (a list of strings), not the QUALITY of
  the citations. Hand-waved evidence passes the lint; reviewers catch
  shallow claims during acceptance.
- **Cannot enforce that `after.files` matches the implementation PR.**
  A proposal can list 2 files; the PR can touch 5. The hand-off
  contract is the implementation PR body citing the proposal's
  `issue:` number.
- **Limitations list is decorative, not enforced.** The renderer draws
  a warn-glyph; the lint does not check that implementation comments,
  tests, or docs acknowledge the listed limitations.
- **No versioning of the proposal itself.** A single YAML is the
  SSOT; per-revision history is git history, not a structured
  changelog. Round-by-round audit trail requires re-authoring into a
  new sub-topic (`02-...`).
- **Skill workflow cannot enforce that the maintainer ACTUALLY read
  the existing code.** Render-only by design; the discipline is
  documented in §Workflow and called out in PR review.
- **Single-pass renderer.** No tree diff, no external link fetch, no
  path-existence check on `after.files` paths. Future-work; honest
  about the gap rather than papering over it.
