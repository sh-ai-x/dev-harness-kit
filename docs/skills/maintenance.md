> [← Skills index](README.md) · [Project README](../../README.md)

# `maintenance`

**Category:** `audit` · **Alpha:** `enforcement` · **Invocation:** `/dev-kit:maintenance --pr N` (human-invoked)

Code-sanity gate. Runs the 20-checkbox rubric from
`eval/prompts/judge-code-sanity.md` on the PR diff: **CC-1..8** (clean code),
**OE-1..8** (over-engineering), **VM-1..4** (value / meaning). Three axes are
scored 0–10 (`code_sanity_score`, `docs_coverage_score`, `scope_discipline_score`)
and collapsed into a single verdict.

## When to use it

- You opened a PR that touches production code and want the maintenance gate
  verdict before requesting review.
- You want a local audit of an arbitrary file set
  (`/dev-kit:maintenance --diff lib/foo.py bin/bar.sh`).
- GH-Actions minutes are exhausted and you need a local mirror verdict before
  pushing (`bin/review-local.sh` chains this into the review-local loop).

## How it works

The skill is a thin wrapper around the canonical rubric:

```bash
/dev-kit:maintenance --pr 762                  # audit PR #762
/dev-kit:maintenance --diff lib/foo.py         # audit an arbitrary file set
/dev-kit:maintenance --pr 762 --json           # machine-readable verdict
```

Body of the judge prompt lives in
[`skills/maintenance/SKILL.md`](../../skills/maintenance/SKILL.md); the slash
command forwards `$ARGUMENTS` to it. The CI gate invokes the same skill from
`.github/workflows/maintenance.yml:208-209` via `claude-code-action`.

### Verdict mapping

| `code_sanity_score` | Verdict |
|---|---|
| ≥ 8.0 | **Approve** |
| 5.0 – 7.99 | **Changes Requested** |
| < 5.0 | **Blocked** |

`Blocked` short-circuits the docs check — even a perfectly-documented PR fails
if the judge flags a critical over-engineering or clean-code violation.

### Docs-updated sub-gate

A second sub-gate runs alongside the score: PASS if (a) the PR touches no
production path, (b) the PR also touches a `docs/` file (excluding the
auto-managed `docs/stages/STAGES.md` and `docs/repo/REPOSITORY-MAP.md`), or
(c) the PR body carries a `docs-not-required:` marker with a quoted
pre-existing reference. Otherwise the verdict is downgraded from `Approve` to
`Changes Requested`.

## Output format

The response MUST begin with one exact line (parsed by
`lib/maintenance_gate.py::extract_verdict_from_stdin`):

```
**Verdict:** Approve
```

Followed by per-axis breakdowns, per-line inline findings for every CC / OE / VM
item flagged, and the docs-updated sub-gate verdict.

## When NOT to use

- Pure docs change with no production code touched — skip the gate.
- Auto-bump PR (`chore(release): bump dev-kit to vX.Y.Z`) — the CI workflow
  short-circuits at `.github/workflows/maintenance.yml:75`.
- Diff exceeds ~3000 lines — split into per-file audits or run
  `/dev-kit:inspect` first.

## Related

- [`evaluate`](evaluate.md) — full eval pass; maintenance is one of three
  rubric outputs (review / security / maintenance) chained in
  `.github/workflows/review.yml`.
- [`review-local`](../../commands/review-local.md) — local mirror of the
  review chain, including this maintenance verdict loop.
- `eval/prompts/judge-code-sanity.md` — the canonical 20-checkbox rubric.
- `lib/maintenance_gate.py::combine_verdict` — verdict-extraction helper.