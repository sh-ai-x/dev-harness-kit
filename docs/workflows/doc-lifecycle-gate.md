# Doc Lifecycle Gate (`stale_after`)

This document is the source-level record for the OKF §5.4–§5.5 doc
lifecycle `stale_after` gate wired into `.github/workflows/ci.yml`'s
`validate` job.

The gate is the deterministic enforcement Iron Law L7 requires: the
LLM staleness heuristic in `skills/docs-maintenance/SKILL.md` cannot
self-impose a deadline on itself. The checker (`tools/check_doc_lifecycle.py`)
parses the YAML frontmatter of every `rules/*.md` (except
`rules/index.md`, the navigation page) and fails the CI run when any
field-explicit `stale_after` is in the past.

## Gate semantics

| File status                                | Outcome |
|--------------------------------------------|---------|
| No frontmatter at all                      | pass (fail-open) |
| Frontmatter present, malformed YAML        | fail (fail-closed) |
| Frontmatter present, `stale_after` missing  | pass (fail-open) |
| `stale_after: <YYYY-MM-DD>` >= today       | pass |
| `stale_after: <YYYY-MM-DD>`  < today       | fail with a per-file violation |
| `status: deprecated`                       | pass (exempt, §5.4) |

The fail-open absent-field rule is documented in the proposal as
§limitations 4: the field is opt-in for new docs and is only enforced
once an author has committed to a refresh cadence. `rules/index.md` is
a navigation page, not a doc — it is reserved out by the checker.

## Refresh-cadence contract

Each `rules/*.md` that opts into the gate names its refresh trigger in
the proposal table `| 갱신 트리거 | 제안 만료 |`. The current opt-ins:

| File                          | Trigger                                | Expiry |
|-------------------------------|----------------------------------------|--------|
| `rules/token-pricing.md`      | quarterly `/dev-kit:llm-refresh`       | 2026-11-30 |
| `rules/git-workflow.md`       | hooks/ADR cadence                       | 2027-05-31 |
| `rules/skill-authoring.md`    | SKILL.md schema cadence                 | 2027-05-31 |
| `rules/session-hygiene.md`    | model tier cadence                     | 2027-05-31 |
| `rules/test-files.md`         | test runner cadence                    | 2027-08-19 |

When the expiry alarm fires, the failure is the system's only signal
that the doc is stale — operators should refresh the field (and the
underlying content) rather than defer the alarm three times in a row
(per proposal §롤백 condition 1).

## Running the checker locally

```bash
# Default — uses today's date
python3 tools/check_doc_lifecycle.py

# Override "today" for date-arithmetic tests / dry-run
python3 tools/check_doc_lifecycle.py --today 2026-08-19
```

Exit codes:

- `0` — all files within date or exempt
- `1` — at least one expired file or frontmatter parse failure

The checker is unit-tested in `tests/test_doc_lifecycle.py` (12
cases — date arithmetic, fail-open absent, deprecated exempt,
malformed fail-closed, mixed-run violation list). PyYAML is a
runtime dep; both `validate:` and `test:` jobs in `ci.yml` install it
via `pip install -r requirements.lock`.

## Rollback conditions

Per proposal §롤백, revert this gate if any of:

1. Expiry alarm triggers date-only deferral commits 3+ times instead
   of catching real stale docs.
2. `/dev-kit:docs-maintenance` cannot catch stale docs without the
   heuristic this gate replaced.
3. OKF redefines lifecycle family in a breaking way (e.g. renames
   `stale_after`).

## Related

- `tools/check_doc_lifecycle.py` — checker entry point.
- `tests/test_doc_lifecycle.py` — regression cases.
- `skills/docs-maintenance/SKILL.md` — the LLM staleness heuristic
  this gate replaced (now a 1-line pointer).
- `iron-laws/index.md` — L7 (`알파는 모델이 스스로 부과할 수 없는 부분에 쓴다`).
- `docs/proposals/okf-adoption/` — proposal, design record.
