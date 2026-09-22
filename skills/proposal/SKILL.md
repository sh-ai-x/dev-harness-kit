---
name: proposal
category: design
description: 0-arg YAML-to-HTML renderer for reviewable design proposals.
alpha: state
when_to_use: |
  - User types `/dev-kit:proposal`
  - User wants to render or share a proposal before implementation
  - `/dev-kit:plan` emits a proposal at Gate 5/5
allowed-tools: Read Write Bash
model: sonnet
user-invocable: true
---
> [← Skills index](../../README.md) · Detailed reference: [docs/skills/proposal.md](../../docs/skills/proposal.md)

# `/dev-kit:proposal`

Render proposal YAML to one self-contained, escaped HTML document. The YAML
is the SSOT; this skill does not author or silently edit it. Read
`docs/skills/proposal.md` before changing renderer behavior or schema details.

## Status-routed layout

Use `docs/proposals/<bucket>/<main>/<sub>.{yaml,html}`. Route YAML `status:` as:

```text
draft | design-discussion | in-review -> reviewing
ready-for-review                         -> pending
accepted | applied-with-changes          -> applied
rejected | superseded                    -> rejected
```

Unknown statuses go to `reviewing`. An explicit `<bucket>/<main>/<sub>` CLI
path overrides routing. Legacy flat files are read-only compatible; new
writes use the bucket layout. Reserved `proposal.yaml` and `index.yaml` are
ignored. `started:` and `shipped:` are optional timeline metadata; they do
not create a new bucket.

## Commands

```bash
python3 -m lib.render_proposal_html --list
python3 -m lib.render_proposal_html <main>/<sub>
python3 -m lib.render_proposal_html <bucket>/<main>/<sub>
python3 -m lib.render_proposal_html --all
python3 -m lib.render_proposal_html --migrate
python3 -m lib.render_proposal_html --in-flight
```

Print the source, routed output, status, section count, and an `open` hint;
then stop. `--migrate` is idempotent. The renderer is the pure implementation
in `lib/render_proposal_html.py` plus its `__main__` CLI; no second proposal
binary is needed.

## Authoring contract

Use a narrow topic slug (`main/sub`, or `bucket/main/sub`) and keep the
reviewer commitment honest: every implementation file belongs in `after.files`.
New or materially changed proposals should include evidence-backed:

```yaml
title: One-line title
status: draft
date: YYYY-MM-DD
before:
  summary: Current behavior.
  evidence: ["file:line, test output, log, or commit"]
after:
  summary: Proposed behavior.
  files:
    - path: repo/file
      change: What changes.
pros: ["Cited gain"]
cons: ["Accepted weakness and mitigation"]
limitations: ["Out-of-scope design limit"]
sections:
  - title: Decision
    body: Markdown-lite content.
```

The five structured fields are optional and independently rendered, so legacy
section-only YAML remains valid. `before` describes the existing code with
evidence; `after` describes the proposed state. `cons` are accepted trade-offs,
not deferred work; `limitations` are intentional design boundaries.

## Safety and rendering invariants

- Escape every interpolated title, anchor, body value, and URL.
- Emit inline CSS only: no `<script>`, external assets, or executable HTML.
- Preserve relative sibling links such as `<sub>.html`; reject dangerous
  schemes (`javascript:`, `data:`, `vbscript:`, `file:`).
- Attach `← 00-index` navigation only when the sibling index exists.
- Keep parser shape validation and backward compatibility.

Tests in `tests/test_proposal_skill.py` pin escaping, routing, migration,
timeline, link safety, structured fields, and legacy compatibility. After
rendering, open the printed HTML path in a browser and update YAML `status:`
as the proposal moves through review. Accepted implementation follows
`/dev-kit:plan` → `/dev-kit:build`.

## PCL Rubric (loop engineering)

Score each pros/cons/limitations item before declaring a proposal ready for
review. Refactor the lists until the thresholds pass; the loop terminates in
≤5 iterations.

| List | Scale | Target | Items | Win condition |
|---|---|---|---|---|
| **Pros** | 0-3 each (3 = concrete file:line/cite + actionable + unique) | **sum ≥ 15** | ≥6 | sum ≥ 15 |
| **Cons** | 0-3 each (0 = fully mitigated, 3 = unmitigated blocker) | **sum ≤ 6** | ≤4 | sum ≤ 6 |
| **Limitations** | 0-3 each (0 = out-of-scope + future-work path, 3 = "didn't get to it") | **sum ≤ 5** | ≤5 | sum ≤ 5 |

**Per-item rubric:**

- Pros 3 — citation present (`file:line`, commit hash, or log excerpt) + actionable + unique
- Pros 2 — concrete claim but generic
- Pros 1 — vague
- Pros 0 — not a real pro

- Cons 0 — fully mitigated with documented workaround
- Cons 1 — acknowledged + explicit mitigation
- Cons 2 — known trade-off with escape path
- Cons 3 — unmitigated blocker

- Limitations 0 — out-of-scope by design + future-work path
- Limitations 1 — out-of-scope by design
- Limitations 2 — soft limitation (no enforcement means by nature)
- Limitations 3 — "we didn't get to it" (replace with a real one or delete)

**Iteration loop:**

1. Draft `before:`, `after:`, `pros:`, `cons:`, `limitations:`.
2. Score each item against the rubric above.
3. Refactor: weaken unmitigated cons to 1-pt mitigated ones, fold generic
   pros into a single cited one, drop fake "didn't get to it" limitations.
4. Re-score; if any threshold fails, repeat.
5. **Cap at 5 iterations.** Beyond that, promote the proposal with a
   `rubric-failed:` YAML note — a miscalibrated rubric is itself a bug.

## Pros

Score each item per the rubric in § PCL Rubric (sum **17 / target ≥ 15**, 7 items).

- **[3/3] Pure function renderer.** `lib/render_proposal_html.py:1465 render()` is byte-identical across runs (`render(p, now=...)` makes time deterministic), so two reviewers see the same HTML.
- **[3/3] Backward-compatible structured fields.** All five `before:` / `after:` / `pros:` / `cons:` / `limitations:` fields are independently optional; legacy `sections:`-only YAML renders unchanged (`tests/test_proposal_skill.py::BeforeAfterRenderTests::test_render_no_fields_emits_no_ba_sections`).
- **[3/3] Status-routed filesystem layout.** YAML `status:` auto-routes to `reviewing|pending|applied|rejected`; unknown statuses fall back to `reviewing` so a typo still produces a routable path (`lib/render_proposal_html.py:STATUS_TO_BUCKET` table).
- **[2/3] Idempotent migration.** `python3 -m lib.render_proposal_html --migrate` is safe to re-run after adding new proposals; legacy flat files stay read-only compatible.
- **[2/3] Inline-CSS-only output.** No `<script>`, no remote `<link>`, no remote `<img>`; `<script>` in a YAML title renders as `&lt;script&gt;` (`tests/test_proposal_skill.py::HtmlEscapeTests::test_script_in_title_escaped`).
- **[2/3] PCL rubric bakes quality into authoring.** Authors see the score contract at slash-autocomplete (§ PCL Rubric), not behind a flag — the loop terminates in ≤5 iterations, so a miscalibrated rubric can't trap them.
- **[2/3] Files-list is a reviewer commitment.** Anything not in `after.files` MUST NOT change in the implementation PR; the hand-off contract is the PR body citing the proposal's `issue:`.

**Sum: 3+3+3+2+2+2+2 = 17** (target ≥ 15, ✓)

## Cons

Score each item per the rubric in § PCL Rubric (sum **3 / target ≤ 6**, 3 items; all "by design" = 1 each).

- **[1/3] Two-file documentation split — by design.** `SKILL.md` is the brief; the full schema reference lives at `docs/skills/proposal.md`. The split keeps `SKILL.md` skimmable at slash-autocomplete (top-of-skill, ~180 lines) and lets the detailed schema go where contributors actually look. The trade-off (cross-URL lookup) is bounded by the pointer banner at line 14; readers who need a specific schema detail follow one link.
- **[1/3] Markdown-lite grammar is intentionally narrow — by design.** Headings stop at H3; no nested lists, footnotes, images, or HTML pass-through. Every construct added costs ~30 LOC in `lib/render_proposal_html.py::_is_block_start` plus a matching detector in `render_body`; the trade-off is bounded per-construct, so most proposals fit without extension, and the cost of adding one is explicit (not hidden in a parser upgrade).
- **[1/3] CLI driver lives in the lib's `__main__` — by design.** The entry point is `lib/render_proposal_html.py:__main__`, not `bin/dev-kit-*.py`, because the proposal skill is the only caller. The trade-off is a trip-hazard for new contributors who look for `bin/dev-kit-proposal.py`; `§ Architecture` explicitly calls this out so the deviation is discoverable in one read, not silent.

**Sum: 1+1+1 = 3** (target ≤ 6, ✓)

## Limitations

Score each item per the rubric in § PCL Rubric (sum **1 / target ≤ 5**, 4 items; three are "out-of-scope + future-work" = 0).

- **[1/3] Rubric scores prose, not implementation correctness.** Test coverage stays independent; the PCL rubric governs the artifact quality, not the resulting code change. Out-of-scope by design (no enforcement means by nature).
- **[0/3] No auto-grade of `good evidence`.** Citations must be human-verifiable from `file:line`; the parser only enforces the list shape. Future-work: `lint_proposal.py --verify-citations` grep-resolves each `path:line` anchor against HEAD.
- **[0/3] Cross-proposal rubric coordination.** Each proposal's PCL score is local. Future-work: a shared `proposals/_rubric/` table per umbrella.
- **[0/3] Renderer is single-pass.** No tree-diff precompute, no external link fetch, no `after.files` existence check. Future-work; the skill is honest about the gap rather than papering over it.

**Sum: 1+0+0+0 = 1** (target ≤ 5, ✓)

## Out of scope by design

This skill renders and routes. It does not judge evidence quality, diff the
implementation against the proposal, or enforce that listed limitations land
in the implementation. Each is a reviewer responsibility:

- **Evidence quality** — read each `before.evidence` citation from
  `file:line` and confirm; the parser only enforces the list shape.
- **Implementation diff** — compare the merged PR's touched files against
  `after.files`; the hand-off contract is the PR body citing `issue:`.
- **Limitation acknowledgment** — verify each `limitations:` entry shows up
  in the implementation's tests or docs; the renderer only styles the list.

These three are out-of-scope by design and tracked as future-work in
`docs/proposals/reviewing/proposal-skill-pcl/00-pcl-engineering.html`
(prospective: `lint_proposal.py --verify-citations` and a shared
`proposals/_rubric/` table).
