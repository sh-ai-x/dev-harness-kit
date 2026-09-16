# /dev-kit:proposal-orch-issue-pr — Skill README

> Thin orchestrator-first triage entry point. Gathers the repo's open
> GitHub PRs + issues, scores them by orchestrator boundary, and writes
> a proposal YAML + HTML via the existing `/dev-kit:proposal` renderer.
> Slash command: `/dev-kit:proposal-orch-issue-pr`.

## What this skill does

`/dev-kit:proposal-orch-issue-pr` is a **0-arg** skill. It accepts no
options. The user types the slash command and gets:

1. **Snapshot** — `gh pr list --json ...` + `gh issue list --json ...`
   + `gh pr view --json statusCheckRollup,changedFiles` per PR.
2. **Classification** — every item is mapped to one of nine
   orchestrator boundaries (state / edit-admission / artifact /
   side-effect / verification / recovery / throughput / measurement /
   documentation) via a deterministic label + title regex table.
3. **Scoring** — each item gets a `(Bottleneck, Risk, Change
   containment)` triple from a default table plus modifiers for
   wide PRs and red checks.
4. **Disposition** — `keep` / `replace` / `defer` / `reject` from the
   rule documented in `lib/proposal_orch_issue_pr.py`.
5. **Ordering** — items are bucketed into the six critical-path
   sequences (hard-stop → resume/audit → side-effect-integrity →
   safe-cleanup → measured-optimization → learning/documentation).
6. **YAML composition** — a proposal YAML is generated at
   `docs/proposals/<bucket>/long-running-priorities/open-work-priority.yaml`
   matching the `/dev-kit:proposal` schema (the renderer accepts it
   unchanged).
7. **HTML render** — `python3 -m lib.render_proposal_html` produces
   the matching `.html` file. The renderer is **not modified** —
   this skill only invokes it.

The skill deliberately preserves the existing `/dev-kit:proposal`
contract. The renderer handles HTML escape, dark-mode awareness, and
inline-CSS-only output; this skill contributes only the triage logic.

## Why a separate skill (not a flag on `/dev-kit:proposal`)

- The triage is a **distinct user action** — "regenerate the backlog
  snapshot" — distinct from "render my YAML". A slash on its own
  keeps the entry point visible.
- Slash autocomplete does not surface flags. A `proposal --orch-issue-pr`
  flag would be invisible at the moment of invocation.
- The two skills share no state — `/dev-kit:proposal` is render-only,
  this skill is read-only-snapshot + write-only-to-proposals. The
  hand-off contract is just "this skill writes a YAML and then calls
  the existing renderer".

## File layout

```
skills/proposal-orch-issue-pr/
├── SKILL.md              # slash command frontmatter + body
├── README.md             # this file
└── (no scripts/ — CLI lives in lib/proposal_orch_issue_pr.py)

lib/
└── proposal_orch_issue_pr.py  # pure pipeline + __main__ CLI entry point

tests/
└── test_proposal_orch_issue_pr.py  # classify / score / recommend / bucket /
                                    # snapshot / compose / render / CLI tests

docs/proposals/
└── <bucket>/long-running-priorities/open-work-priority.{yaml,html}
        # the rendered deliverable (re-generated on every invocation)
```

The skill pattern mirrors `/dev-kit:proposal` (skill has Write
permission; CLI lives in the lib's `__main__` block). See
`skills/proposal/SKILL.md §Architecture` for the rationale.

## Invocation

```text
/dev-kit:proposal-orch-issue-pr
```

No options. The output lands at:

```
docs/proposals/<bucket>/long-running-priorities/open-work-priority.yaml
docs/proposals/<bucket>/long-running-priorities/open-work-priority.html
```

`<bucket>` is one of `review` / `accepted` / `rejected`, auto-routed
from the YAML's `status:` field by the proposal renderer. The skill
writes `status: ready-for-review` by default; promote by editing the
YAML.

### Direct CLI (debug + scripting)

```bash
# from the repo root
python3 -m lib.proposal_orch_issue_pr                     # full pipeline
python3 -m lib.proposal_orch_issue_pr --print-yaml        # YAML only
python3 -m lib.proposal_orch_issue_pr --bucket accepted   # write to accepted/
python3 -m lib.proposal_orch_issue_pr --project-root /path/to/repo
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | pipeline succeeded; YAML + HTML written |
| 2 | `gh` CLI missing or not authenticated |
| 3 | snapshot failed (malformed JSON, network error) |
| 4 | render failed (bad bucket or slug) |

## Why this skill exists

Without `/dev-kit:proposal-orch-issue-pr`, the orchestrator triage is
either (a) hand-written each sprint and prone to bias toward PR age
or (b) hidden in `/dev-kit:ralph` where it can't be regenerated on
demand. This skill gives the operator a **deterministic, regenerable
snapshot** keyed off the live GitHub state, so the proposal always
reflects what is open *now*, not what was open when the YAML was last
hand-edited.

The deterministic pipeline (classify → score → recommend → bucket) is
the part next-gen models can't self-impose. The YAML body itself is a
programmatic scaffold; reviewers add prose justification before
promoting the proposal to `accepted`.

## Trust model

- **`gh` CLI is the only network boundary.** `lib/gh_cli.py::gh_available`
  fails closed when `gh` is missing or unauthenticated. The skill
  returns exit 2 in that case — never an empty proposal.
- **No LLM call inside the lib.** `classify`, `score`,
  `recommend_disposition`, and `bucket_for` are pure functions of the
  snapshot + label regex tables. The model only authors the user's
  invocation message; the deterministic engine produces the YAML.
- **Renderer is unchanged.** HTML escape, URL-scheme allowlist, and
  inline-CSS-only output are owned by `lib/render_proposal_html.py`.
  A `<script>` in any field is escaped before reaching the browser.
- **Atomic writes.** YAML and HTML go through `lib.atomic.atomic_write_text`
  so a partial write cannot leave a half-written proposal on disk.
- **Bucket / slug whitelist.** `_safe_bucket` and `_safe_slug` reject
  any path component outside the renderer's BUCKETS whitelist or the
  `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` slug regex. A path-traversal
  argument cannot escape `docs/proposals/`.

## Files installed

| Path | Purpose |
|---|---|
| `skills/proposal-orch-issue-pr/SKILL.md` | Slash command frontmatter + body |
| `skills/proposal-orch-issue-pr/README.md` | This file |
| `lib/proposal_orch_issue_pr.py` | Deterministic engine: snapshot, classify, score, recommend, order, compose. Pure functions + a single gh-CLI integration point. |
| `tests/test_proposal_orch_issue_pr.py` | Snapshot parsing, classification table, score computation, disposition rule, ordering invariant, YAML composition shape, renderer hand-off, gh-availability degradation. 54 tests, all passing. |

## Related files

- `skills/proposal/SKILL.md` — the existing proposal skill (untouched).
- `lib/render_proposal_html.py` — the renderer this skill delegates to.
- `lib/gh_cli.py` — `gh` CLI presence + auth probe shared across the kit.
- `lib/atomic.py` — atomic write helper used by both this skill and the renderer.
- `tests/test_proposal_skill.py` — sibling test contract for the renderer.

## Why this is `alpha: state`

Per CLAUDE.md Iron Law L6, every new skill must declare `alpha:`. The
proposal artifact has a **stateful lifecycle**:

- A YAML source on disk is the SSOT.
- HTML is **derived state** regenerated from YAML.
- The `status:` field is a **state machine** the maintainer advances
  over time.
- The skill persists a proposal artifact and gates its progression.

That is `state` by definition — distinct from `analysis` (reasoning
over a corpus) and `enforcement` (deterministic guards). **L7 fit**:
the deterministic disposition rule + boundary classification table +
critical-path ordering is exactly the part next-gen models can't
self-impose. The model can reason about *whether* a disposition is
right; the skill owns *how* the artifact is generated, versioned, and
surfaced for review.

## Hand-off

After this skill produces the YAML + HTML:

1. Open the HTML in a browser.
2. Override any disposition the reviewer disagrees with (edit the
   YAML's per-bucket sections).
3. Promote the YAML's `status:` to `accepted` once the disposition
   is locked.
4. Hand off to `/dev-kit:plan` if a small replacement PR should be
   created from current `origin/main` for any `replace` / `defer`
   item. The plan skill consumes the YAML's `after.files` list as
   its implementation commitment.
5. The proposal HTML closes the triage record; no separate archive
   step is required.