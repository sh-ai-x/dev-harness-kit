# Maintenance Gate — over-engineering + clean-code + value gate

The maintenance gate is the CI counterpart to the
pre-push "intent" check. It runs on every pull request and judges the
PR diff against the 20-checkbox code-sanity rubric from
`eval/prompts/judge-code-sanity.md`. The full rubric has three
buckets:

- **Clean code (CC-1..8)** — vague names, oversized functions, dead
  code, magic constants, copy-paste, swallowed errors, type unsafety,
  stale comments.
- **Over-engineering (OE-1..8)** — single-implementer abstract base
  classes, speculative parameters / YAGNI flags, premature
  optimization, excessive layering, factory/strategy/DI for one impl,
  deep inheritance, file-per-class sprawl.
- **Value / meaning (VM-1..4)** — stated purpose, no noise, scope
  discipline, "diff earns its lines."

The PR-only `push_intent` judge in `.githooks/pre-push` runs the
VM-1..4 axes locally before each push. The maintenance gate is the
full CC + OE + VM check on the consolidated PR diff — slower but
comprehensive.

## Architecture

| Surface | File | Role |
|---|---|---|
| Workflow | `.github/workflows/maintenance.yml` | CI entry point. Two jobs: `maintenance_judge` (runs `/dev-kit:maintenance --diff <PR>` via `claude-code-action`) and `gate` (extracts verdict + runs docs-updated sub-gate). |
| Judge prompt | `eval/prompts/judge-maintenance.md` | Wraps the 20-checkbox rubric and asks the model for `code_sanity_score`, `docs_coverage_score`, `scope_discipline_score` (each 0-10) plus `reason` and `items_flagged[]`. |
| Gate logic | `lib/maintenance_gate.py` | Pure-function verdict extraction + docs-updated check + `combine_verdict` derivation. Unit-tested in `tests/test_maintenance_gate.py`. |
| Pre-push sibling | `.githooks/pre-push` (opt-in via `DEV_KIT_PUSH_INTENT=1`) | Runs `lib/push_intent_judge.py` on the tip commit before push. Only the 4 value/meaning axes (VM-1..4). |
| Pre-push sibling CLI | `lib/push_intent_judge.py` | Thin wrapper over `lib/llm_judge.call_judge` with `dim="push_intent"`. |
| Regression fixtures | `eval/golden/maintenance-*.json` | Three golden baselines for the maintenance judge. |

## Verdict derivation

The judge prompt asks for three 0-10 composite axes. The gate maps
the `code_sanity_score` to a CI verdict:

| `code_sanity_score` | CI Verdict |
|---|---|
| ≥ 8.0 | **Approve** |
| 5.0 – 7.99 | **Changes Requested** |
| < 5.0 | **Blocked** |

The gate then runs the **docs-updated sub-gate**:

- **Pass** if (a) the PR touches no production path, (b) the PR also
  touches a `docs/` file (excluding `docs/stages/STAGES.md` and
  `docs/repo/REPOSITORY-MAP.md`, which are auto-managed), or (c) the PR
  body contains the `docs-not-required:` marker with a quoted
  pre-existing reference.
- **Fail** otherwise — the gate downgrades `Approve` to
  `Changes Requested` so the human reviewer notices.

`Blocked` from the judge short-circuits the docs check: even a
perfectly-documented PR fails the gate if the judge flags critical
over-engineering or clean-code violations.

## Thresholds (tunable)

The `combine_verdict` function in `lib/maintenance_gate.py` is the
single source of truth for the verdict map. To raise or lower the
threshold, edit the band's `>=` cutoffs in `maintenance.yml`'s judge
prompt mapping and update the matching unit test in
`tests/test_maintenance_gate.py`.

## Bypass

There is no `--no-verify` equivalent for this gate. To force-merge a
PR that fails maintenance, the operator must either:

1. Fix the gate's finding in a follow-up commit (re-runs the gate).
2. Have a maintainer with admin rights merge via the GitHub UI
   bypassing branch protection. The gate never auto-approves.

## Worked example

PR that adds `lib/awesome.py` (20 lines of well-named functions, a
clear docstring, no abstract base classes) and `docs/awesome.md`:

1. `maintenance_judge` runs `/dev-kit:maintenance --diff 42`.
   - Judge prompt returns `code_sanity_score=9.0`,
     `docs_coverage_score=10.0`, `scope_discipline_score=10.0`,
     `items_flagged=[]`.
   - Judge emits `**Verdict:** Approve`.
2. `gate` extracts `Approve`.
3. `gate` runs the docs-updated sub-gate:
   `docs_updated_ok(["lib/awesome.py", "docs/awesome.md"], "")` →
   `(True, "docs updated: docs/awesome.md")`.
4. `gate` runs `combine_verdict(Approve, True, "...")` →
   `{"verdict": "Approve", "docs_ok": True, ...}`.
5. Gate exits 0.

Same PR but only `lib/awesome.py` (no docs update):

1. `maintenance_judge` returns `code_sanity_score=9.0` →
   `**Verdict:** Approve`.
2. `gate` extracts `Approve`.
3. `gate` runs `docs_updated_ok(["lib/awesome.py"], "")` →
   `(False, "PR changed lib/awesome.py but no doc under docs/ ...")` .
4. `gate` runs `combine_verdict(Approve, False, "...")` →
   `{"verdict": "Changes Requested", "docs_ok": False, ...}`.
5. Gate exits 1 with `::error::reason: ...`.

## Local development

Run the gate's logic locally without GitHub Actions:

```bash
# Extract verdict from a comment body
echo "**Verdict:** Approve" | python3 -m lib.maintenance_gate \
  --extract-verdict-from-stdin

# Run the docs-updated check
python3 -m lib.maintenance_gate --docs-check \
  --changed-files lib/foo.py --changed-files docs/foo.md \
  --pr-body ""

# Combine judge verdict + docs check
python3 -m lib.maintenance_gate \
  --judge-verdict Approve \
  --docs-ok \
  --docs-reason "ok"
```

`.github/workflows/fork-pr-review.yml` is the escalation path: on
`pull_request_target` (workflow file read from trusted `main`, so safe
to grant write permissions) for fork PRs only, gated behind the
`fork-pr-review` GitHub Environment (required reviewer = repo owner).
On approval it dispatches `maintenance.yml` + `review.yml` via
`workflow_dispatch` — not a fork-origin event, so that run reaches
`maintenance_judge`/`gate` with full normal permissions. Same-repo PRs
(including the owner's own) are unaffected; they keep running on
`pull_request` fully automatically.

Because `workflow_dispatch` runs do **not** auto-link their jobs to
the PR's commit in the PR Checks tab (only `pull_request` /
`pull_request_target` events do), the gate dispatches run the AI
judges invisibly from the PR's perspective — the original
`pull_request` run keeps showing `skipped` for each judge job
indefinitely. The gate compensates by writing a single
**`fork-pr-review/ai-judges`** commit status to the PR's HEAD commit
itself (via `gh api repos/.../statuses/$SHA`):

- `state=pending` immediately after dispatch (with target_url pointing
  to the dispatched `review.yml` run)
- `state=success` once both dispatched runs complete with
  `conclusion=success` (judges passed and PR is mergeable via
  auto-approve)
- `state=failure` if either dispatched run fails (judge rejected,
  infra error, or timeout)

The status lives on the PR commit regardless of which workflow event
drove it, so the PR Checks tab always shows the aggregate instead of
the stale per-judge `skipped` verdicts. Per-judge verdict comments are
still posted on the PR by each dispatched run as before.

## Related

- `eval/prompts/judge-code-sanity.md` — the 20-checkbox rubric (the
  deterministic SSOT the maintenance judge prompt references).
- `eval/prompts/judge-maintenance.md` — the judge's user prompt.
- `lib/maintenance_gate.py` — gate logic (verdict extraction +
  docs-updated check + `combine_verdict`).
- `tests/test_maintenance_gate.py` — unit tests (19 tests covering
  each verdict + each docs-check branch + CLI parity).
- `eval/golden/maintenance-*.json` — three regression fixtures for
  the maintenance judge (value-aligned, over-engineering, scope
  drift).
- `.github/workflows/review.yml` — the sibling security/correctness
  gate. Both gates share the verdict-extraction pattern so PR
  comments look identical to operators.
- `.github/workflows/fork-pr-review.yml` — maintainer-approval gate
  that dispatches this workflow (+ `review.yml`) for fork PRs. See
  "Fork PRs" above.
- `docs/stages/STAGES.md` §7 — the pipeline-stage description.