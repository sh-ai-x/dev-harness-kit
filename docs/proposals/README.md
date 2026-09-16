# Proposals directory

Layout invariant: every proposal lives at `docs/proposals/<bucket>/<main>/<sub>.{yaml,html}` where `<bucket>` is one of four lifecycle stages.

## Buckets (left-to-right lifecycle)

| Bucket | YAML `status:` values | Meaning |
|---|---|---|
| `reviewing/` | `draft`, `design-discussion`, `in-review` | Proposal is being iterated on. Stays here until promoted or killed. |
| `pending/` | `ready-for-review` | Approved, queued for implementation. Work has not started. |
| `applied/` | `accepted`, `applied-with-changes` | Implemented in code. `accepted` (AS DESIGNED) and `applied-with-changes` (WITH design deviations) both live here. The umbrella directory carries a `-mod` suffix for the latter and the YAML records the as-shipped deviations in `modifications:`. The shipped date is recorded in the YAML's `shipped:` field. |
| `rejected/` | `rejected`, `superseded` | Killed without implementation. |

`STATUS_TO_BUCKET` in `lib/render_proposal_html.py` is the single source of truth — the CLI auto-creates the bucket directory when missing.

## Sub-proposals and umbrellas

`<main>` is the umbrella slug (one umbrella groups N related sub-proposals).
`<sub>` is the sub-topic slug. Files named after the sub-topic — not `index.yaml` — so leaves are recognizable on a flat listing.

Cross-references between siblings use bare `<sub>.html` (same `<main>/` parent).
Cross-umbrella links would use `../<other-main>/<sub>.html`.

## `-mod` suffix

Proposals whose implementation deviated from the design live under `applied/<umbrella>-mod/` (e.g. `applied/harness-effectiveness-mod/`). The `-mod` suffix is the visual marker; the YAML's `modifications:` block records the original-vs-shipped diff inline so reviewers see both.

Co-locating `accepted` and `applied-with-changes` under one bucket keeps the umbrella grouping (e.g. all `harness-effectiveness-*` proposals together) intact across 2-level re-renders. Splitting them into separate buckets would silently rename the umbrella on every render of a `-mod` proposal.

## Renderer and CLI

```
/dev-kit:proposal <main>/<sub>             # render one (bucket auto-routes from YAML status)
/dev-kit:proposal applied/<main>/<sub>     # render one with explicit bucket override
/dev-kit:proposal --list                   # list available proposals
/dev-kit:proposal --all                    # render every proposal
/dev-kit:proposal --migrate                # one-shot move legacy flat proposals into bucket dirs
```

See `skills/proposal/SKILL.md` for the full contract.
