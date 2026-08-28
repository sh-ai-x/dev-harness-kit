> [← Skills index](README.md) · [Project README](../../README.md)

# `proposal`

**Category:** `design` · **Alpha:** `state` · **Invocation:** `/dev-kit:proposal` (human-invoked)

`proposal` renders any `docs/proposals/<bucket>/<main>/<sub>.yaml` design document (where `<bucket>` is auto-routed from the YAML's `status:` field — `review`/`accepted`/`rejected`) into a single self-contained HTML page at `docs/proposals/<bucket>/<main>/<sub>.html`, for sharing with reviewers before implementation begins. It is a distinct skill (rather than a flag on `/dev-kit:plan`) because proposals are a distinct artifact with their own lifecycle — `draft → design-discussion → ready-for-review → accepted/rejected/superseded` — and because slash-command autocomplete doesn't surface flags, so a dedicated entrypoint is the only reliable way for the user to find it.

## When to use it

- The user types `/dev-kit:proposal`.
- The user wants to share a draft proposal or plan with reviewers before implementation.
- The user wants to view an existing proposal as a single self-contained HTML document.
- The plan stage's Gate 5/5 (emit) auto-invokes this skill to render the design record.

## How it works

Every proposal lives at `docs/proposals/<bucket>/<main>/<sub>.{yaml,html}`:

- `<bucket>` is one of `review` / `accepted` / `rejected`, auto-routed from the YAML's `status:` field via `STATUS_TO_BUCKET` (`draft`/`design-discussion`/`ready-for-review` → `review`; `accepted` → `accepted`; `rejected`/`superseded` → `rejected`). Pass `<bucket>/<main>/<sub>` explicitly to override.
- `<main>` is the umbrella grouping N related sub-proposals (e.g. `harness-architecture`).
- `<sub>` is the sub-topic slug (e.g. `protocol-layer`, `00-index`) — the file is named after the sub-topic, not `index.{yaml,html}`, so it stays recognizable on a flat directory listing or static-site host.

The render pipeline: (1) list available topics via `python3 -m lib.render_proposal_html --list` (scans all three buckets + the legacy flat shape); (2) render one topic via `python3 -m lib.render_proposal_html <main>/<sub>` (bucket auto-routes from the YAML's `status:`) or `<bucket>/<main>/<sub>` (explicit override), which writes the HTML into the status-routed layout; (3) print the output path so the user can open it (`open docs/proposals/<bucket>/<main>/<sub>.html` on macOS, or any browser via `file://`); (4) stop — the skill never edits the YAML, only renders it. `--migrate` is a one-shot that moves legacy flat proposals into the bucket each YAML declares (idempotent). The render logic is a pure function in `lib/render_proposal_html.py` plus a `__main__` CLI entry; there is no separate `bin/dev-kit-proposal.py` binary, since the proposal skill is the only caller.

The renderer auto-attaches a `<nav class="back-link">` element (`← 00-index`) at the top of every non-index sub-topic page when a sibling `00-index.yaml` exists in either the source umbrella directory OR the output bucket directory; the 00-index page itself gets no back link. The pure `render()` function takes optional `back_to_href=`/`back_to_label=` kwargs, which the CLI driver wires based on the filesystem sibling check.

**Cross-references**: inside a proposal body, link to a sibling as `[label](<other-sub>.html)` (bare relative path, since both files live in the same `<bucket>/<main>/` directory) or `../<other-main>/<sub>.html` for a cross-umbrella link. The relative-path safety check allows bare relative paths and `../<sibling>.html`, but rejects dangerous schemes (`javascript:`, `data:`, `vbscript:`, `file:`).

The topic slug matches `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (legacy 2-level) or `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (3-level, with the bucket name matching `review|accepted|rejected`). One `/` separator per level, no leading/trailing slash, no `.` segments. The legacy filenames `proposal.yaml` and `index.yaml` are reserved and skipped as leftovers from a previous refactor.

## Usage

```bash
/dev-kit:proposal [<main>/<sub>]              # bucket auto-routes from YAML status
/dev-kit:proposal [<bucket>/<main>/<sub>]     # explicit bucket override
/dev-kit:proposal --list                       # list all (across all 3 buckets + legacy)
/dev-kit:proposal --all                        # render every proposal
/dev-kit:proposal --migrate                    # one-shot move legacy flat -> bucket dirs
```

| Form | Effect |
|---|---|
| `--list` | Lists available proposal topics across all three buckets and the legacy flat shape. |
| `--all` | Renders every discovered proposal to its routed bucket. |
| `--migrate` | One-shot: moves every legacy flat `<main>/<sub>.{yaml,html}` into the bucket its YAML's `status:` declares. Idempotent. |
| `<main>/<sub>` | Renders that topic's YAML to HTML (bucket auto-routes). |
| `<bucket>/<main>/<sub>` | Renders that topic to the explicit bucket, ignoring YAML status. |

## Output

```
## /dev-kit:proposal -- <bucket>/<main>/<sub>

**Source**: docs/proposals/<bucket>/<main>/<sub>.yaml
**Output**: docs/proposals/<bucket>/<main>/<sub>.html (one self-contained HTML doc, inline CSS only, no JS, dark-mode aware)
**Status**: <status from YAML frontmatter>
**Bucket**: <review|accepted|rejected, auto-routed from `status:`>
**Sections**: <count>

**Open in browser**: `open docs/proposals/<bucket>/<main>/<sub>.html` (macOS)
```

## Why `alpha: state`

The proposal artifact has a stateful lifecycle: a YAML source on disk, a derived HTML rendered from it, and a status tag the maintainer advances over time (draft → design-discussion → ready-for-review → accepted/rejected/superseded). That is `state` by definition — the skill persists an artifact and gates its progression, distinct from `analysis` (reasoning over a corpus) or `enforcement` (deterministic guards). Per L7, the deterministic render + status-tag + HTML-escape contract is exactly the part a model can't self-impose; the model reasons about whether a proposal should be accepted, the skill owns how it is rendered, versioned, and surfaced.

## Iron Law

Defensive HTML escaping on every interpolated value — titles, anchors, free-text fields, link URLs. A title containing `<script>` renders as `&lt;script&gt;`, never executed. The output has no `<script>` tag and no external assets (inline CSS only), so it is safe to email, archive, or open from `file://` — the same self-contained-HTML invariant that other dev-kit HTML renderers (e.g. `inspect`, `code-viz`) hold. Pinned by `HtmlEscapeTests` in `tests/test_proposal_skill.py`.

## Related

- [plan](plan.md) — Gate 5/5 auto-invokes this skill (`Skill("proposal", topic="<main>/<sub>")`) to render the design record; the hand-off chain becomes `plan → proposal → build`.
- `lib/render_proposal_html.py` — pure function: `parse_proposal_yaml` + `render` + `__main__` CLI entry.
- `lib/render_report_html.py` — sibling renderer for eval + inspect reports.
- `skills/report/SKILL.md` — sibling skill that still uses the read-only-skill + `bin/` CLI pattern this skill deviates from.
- `tests/test_proposal_skill.py` — pins the HTML-escape contract.

## Structured fields: before / after + pros / cons / limitations

The YAML schema accepts five optional top-level fields that the renderer
emits as first-class sections in the HTML:

```yaml
before:
  summary: |                                  # markdown-lite body
    Description of the code's CURRENT state.
  evidence:                                   # list[str]
    - 'file:line / log excerpt / commit hash'

after:
  summary: |                                  # markdown-lite body
    Description of the code's PROPOSED state.
  files:                                      # list[{path, change}]
    - path: hooks/lib/x.sh
      change: |                                # markdown-lite body
        What this file becomes.

pros:                                         # list[str]
  - 'Strength 1 (with citation).'
cons:                                         # list[str]
  - 'Weakness the proposal knowingly accepts + mitigation.'
limitations:                                  # list[str]
  - 'What the design CANNOT do (out-of-scope by design).'
```

Each field is independent: a proposal may declare any subset. The
renderer emits a `<section id="ba-section">` (two-column `.ba-grid`
of `.before-card` + `.after-card`) when `before:` or `after:` is
present; `<section id="pcl-{pros,cons,limit}">` for each present
pros/cons/limitations list; and skips each wrapper when the
corresponding field is absent. Backward-compatible: legacy proposals
without any of the five fields render byte-identically except for
the inline-CSS block growing.

The parser enforces shape only (list[str], required `change`,
non-string items rejected), not quality — see
[`skills/proposal/SKILL.md` §Limitations](../../skills/proposal/SKILL.md)
for what the lint cannot catch.

---
*Source: [`skills/proposal/SKILL.md`](../../skills/proposal/SKILL.md)*
