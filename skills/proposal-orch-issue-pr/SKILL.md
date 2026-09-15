---
name: proposal-orch-issue-pr
category: design
description: 0-arg orchestrator-first GitHub backlog triage. Gathers open PRs + issues, scores (bottleneck / risk / change containment), orders by orchestrator critical path, and writes a proposal YAML + HTML via the existing `/dev-kit:proposal` renderer.
alpha: state
when_to_use: |
  - User types /dev-kit:proposal-orch-issue-pr
  - Operator wants a refreshed orchestrator-priority view of the open GitHub backlog
  - Maintainer needs a triage record that accepts issues independently of their current PRs
  - Reviewer wants structured before/after + pros/cons/limitations + acceptance gates for backlog disposition
allowed-tools: Read Write Bash Glob
disallowed-tools: Edit
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:proposal-orch-issue-pr — orchestrator-first GitHub backlog triage

A thin orchestration entry point that gathers the repository's open PRs
and issues, scores them through the orchestrator boundary lens, and
hands a freshly-composed YAML to the existing
[`/dev-kit:proposal`](../proposal/SKILL.md) renderer. The proposal skill
is **not modified** by this skill — this skill only writes YAML under
its expected path and then invokes `python3 -m lib.render_proposal_html`
to produce the HTML deliverable.

## Iron Law

**Accept the issue, not the PR, by default.** The triage scores every
open item on the orchestrator critical path. A current PR is kept only
when it (a) maps to one orchestrator boundary, (b) is rebased on current
`origin/main`, (c) has green required checks, and (d) has a rollback
that does not require disabling orchestrator protections. Otherwise the
issue remains accepted, the PR is marked superseded/replace, and a
smaller replacement PR is recommended.

The decision rule is **deterministic** in `lib/proposal_orch_issue_pr.py`
— `recommend_disposition()` returns one of `keep` / `replace` / `defer`
/ `reject` from a pure score + state vector. Reviewers are the only
authoritative override.

## What it does

1. **Snapshot the open backlog** via `gh pr list --json` and
   `gh issue list --json`. Each PR's required-check state is read
   separately via `gh pr view <n> --json statusCheckRollup`. New work
   (created or commented in the last 14 days) is captured before
   ordering. Snapshot metadata is recorded at the top of the YAML.
2. **Classify each item** to one orchestrator boundary:
   `state`, `edit-admission`, `artifact`, `side-effect`,
   `verification`, `recovery`, `throughput`, `measurement`,
   `documentation`. Boundary assignment is derived from labels +
   title patterns + body signal; the classifier is a deterministic
   lookup table in `lib/proposal_orch_issue_pr.py` (no LLM call).
3. **Score** each item internally on three axes, 1–5:
   - **Bottleneck (B)** — how directly it stops orchestrator progress.
   - **Risk (R)** — impact × likelihood × irreversibility.
   - **Change containment (C)** — how easy it is to isolate and roll
     back.
4. **Recommend a disposition** per item: `keep`, `replace`, `defer`,
   `reject`. The recommendation rule is documented in
   `lib/proposal_orch_issue_pr.py::recommend_disposition`.
5. **Order** the items into six orchestrator-critical-path buckets:
   `hard-stop → resume/audit → side-effect-integrity → safe-cleanup
   → measured-optimization → learning/documentation`. This is the
   `hard stop → resume/audit → side-effect integrity → safe cleanup →
   measured optimization → learning/documentation` sequence called for
   by issue #843.
6. **Compose a YAML proposal** at
   `docs/proposals/<bucket>/long-running-priorities/open-work-priority.yaml`
   with the snapshot header, `before` / `after` blocks, `pros` /
   `cons` / `limitations`, and per-bucket `sections`. The YAML is the
   deliverable; the markdown-lite grammar matches what
   `lib/render_proposal_html.py::parse_proposal_yaml` accepts.
7. **Render the HTML** via
   `python3 -m lib.render_proposal_html <bucket>/long-running-priorities/open-work-priority`
   — the existing proposal skill's CLI does the work; this skill only
   invokes it.
8. **Print** the source + output paths so the operator can open the
   HTML directly with `open <path>` (macOS) or any browser via
   `file://`.

The skill is **read-only against the existing `/dev-kit:proposal`
skill and its renderer**. The only files written are the new YAML +
HTML under `docs/proposals/<bucket>/long-running-priorities/`.

## Output (in chat)

```
## /dev-kit:proposal-orch-issue-pr

**Snapshot date**: YYYY-MM-DD (Asia/Seoul)
**Open PRs**: N
**Open issues**: N
**Bucket**: review|accepted|rejected (auto-routed from YAML status)
**Source**: docs/proposals/<bucket>/long-running-priorities/open-work-priority.yaml
**Output**: docs/proposals/<bucket>/long-running-priorities/open-work-priority.html

**Dispositions**:
  - keep: N
  - replace: N
  - defer: N
  - reject: N

**Open in browser**: `open docs/proposals/<bucket>/long-running-priorities/open-work-priority.html` (macOS)
```

## Ordering rule (orchestrator critical path)

Six buckets, in this fixed order: `hard-stop → resume-audit →
side-effect-integrity → safe-cleanup → measured-optimization →
learning-documentation`.

The ordering is enforced by `lib/proposal_orch_issue_pr.py::bucket_for`.
It is NOT controlled by PR age, PR size, or issue labels.

## Boundary classification (deterministic)

`lib/proposal_orch_issue_pr.py::classify` assigns one of nine
orchestrator boundaries using a small rule table (label first,
title patterns second, default `state`). The rule tables live in
`lib/proposal_orch_issue_pr.py` — the SKILL.md does not restate them.

## Disposition rule (deterministic)

`lib/proposal_orch_issue_pr.py::recommend_disposition` returns one of
`keep` / `replace` / `defer` / `reject`. The full rule precedence lives
there; the rule is conservative (when in doubt, `replace` beats `keep`,
and `defer` beats `keep`). The reviewer is the only authority to
override.

## How to use

```text
/dev-kit:proposal-orch-issue-pr
```

The skill accepts no options. The output YAML + HTML land at:

```
docs/proposals/<bucket>/long-running-priorities/open-work-priority.yaml
docs/proposals/<bucket>/long-running-priorities/open-work-priority.html
```

`<bucket>` is one of `review` / `accepted` / `rejected`, auto-routed
from the YAML's `status:` field by `lib/render_proposal_html.py`. The
default status is `ready-for-review` (the proposal is complete, not
a working draft). To mark a different status, edit the YAML's
`status:` field and re-run `/dev-kit:proposal long-running-priorities/open-work-priority`.

## Pros of this skill

- **Accepts issues independently of PRs.** A correct observation is not
  coupled to its current implementation. The triage result is portable
  across PRs.
- **Deterministic scoring.** `recommend_disposition()` is a pure
  function of `(boundary, score, current_main_head, checks_state,
  file_count)`. Same input → same output, every time. Reviewers can
  challenge a recommendation by inspecting the input vector.
- **Orchestrator-first ordering.** Items appear in critical-path
  order, not by PR age or size. The first item a reviewer reads is
  the one that blocks everything else.
- **Reuses the existing renderer.** No duplicate HTML logic. The
  deterministic renderer + atomic write + HTML-escape contract from
  `/dev-kit:proposal` applies unchanged.

## Cons of this skill

- **Boundary classification is heuristic.** The label + title pattern
  table in `lib/proposal_orch_issue_pr.py::classify` can mis-route a
  new area. Reviewers should override the assignment if the boundary
  does not match their mental model.
- **Bottleneck/Risk/Containment scores are operator defaults.** The
  scoring table is hand-coded; the project does not yet have a stable
  corpus for stop rate, recovery time, or artifact-loss rate. Future
  PRs may re-tune the table.
- **Requires `gh` CLI authenticated.** The skill fails with a clear
  error if `gh auth status` does not succeed — see
  `lib/gh_cli.py::gh_available`.
- **Single bucket (long-running-priorities).** Different umbrellas
  (e.g. `ralph-autonomy`, `cache-hit-rate`) would each be a separate
  invocation of the underlying proposal renderer. This skill covers
  one umbrella only.

## Limitations of this skill

- **Cannot detect semantic duplicates.** Two open issues covering the
  same root cause appear as separate items; reviewers dedupe.
- **Cannot detect staleness from lack of activity.** A 2-year-old PR
  with no comments is treated as a live item; reviewers can override
  with a `reject` disposition.
- **Cannot run the underlying GitHub checks itself.** `gh pr view
  --json statusCheckRollup` reads GitHub's record of the last CI
  run; it does not re-trigger the workflow. A green run is fresh as
  of the snapshot moment only.
- **Cannot propose a replacement PR body.** When `replace` is
  recommended, the YAML lists the recommended file scope + tests but
  does not draft the new PR description.
- **YAML body is a programmatic scaffold.** Per-bucket sections are
  deterministically generated from the snapshot + scores. A reviewer
  who wants prose justifications should edit the YAML before
  promoting it to `accepted`.

## Files installed

| Path | Purpose |
|---|---|
| `skills/proposal-orch-issue-pr/SKILL.md` | This file |
| `lib/proposal_orch_issue_pr.py` | Deterministic engine: snapshot, classify, score, recommend, order, compose. Pure functions only; gh-CLI calls live in `_run_gh()` and are mockable. |
| `tests/test_proposal_orch_issue_pr.py` | Snapshot parsing, classification table, score computation, disposition rule, ordering invariant, YAML composition shape, renderer hand-off. |

## Hand-off

After this skill produces the YAML + HTML:

1. Open the HTML in a browser.
2. Override any disposition the reviewer disagrees with (edit the
   YAML's per-bucket sections).
3. Promote the YAML's `status:` to `accepted` once the disposition
   is locked.
4. Hand off to `/dev-kit:plan` if a small replacement PR should be
   created from current `origin/main` for any `replace` / `defer`
   item. The plan skill consumes the YAML's `after.files` list as its
   implementation commitment.
5. The proposal HTML closes the triage record; no separate archive
   step is required.