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
- Keep parser shape validation and backward compatibility; it cannot judge the
  quality of evidence, compare the implementation diff, or enforce limitations.

Tests in `tests/test_proposal_skill.py` pin escaping, routing, migration,
timeline, link safety, structured fields, and legacy compatibility. After
rendering, open the printed HTML path in a browser and update YAML `status:`
as the proposal moves through review. Accepted implementation follows
`/dev-kit:plan` → `/dev-kit:build`.
