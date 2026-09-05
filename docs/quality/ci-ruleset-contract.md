# CI ruleset workflow job-name contract

**Issue #774 — regression guard for the
`.required_status_checks.contexts` <-> workflow job `name:` contract.**

When a GitHub branch-protection ruleset (or legacy
`required_status_checks.contexts[]`) demands a status check, GitHub
matches the context name **literally** against the `name:` of every
workflow job in the PR run. Prefix matching, substring matching, and
regex matching are all **NOT supported**. A job renamed in
`.github/workflows/*.yml` MUST be renamed in the ruleset in the same
PR; otherwise the ruleset treats the new context as "not yet satisfied"
and the PR sits in `mergeStateStatus: BLOCKED` even though
`mergeable: MERGEABLE`.

## Why prefix-matching fails (the 33 / 50 example)

PR #763 (`feat(security): prompt-injection defense`, merged
2026-08-30) added a prompt-injection pre-gate and renamed the severity
gate job in `.github/workflows/review.yml` from:

```
severity gate (review + security)         # 33 chars
```

to:

```
severity gate (review + security + injection_scan)   # 50 chars
```

The ruleset (`.github/rulesets/<id>` file in the GitHub-managed
ruleset, id `20232367`, name `protect main (admin PAT bypass)`) was
NOT updated in the same PR. The required context was still the
**original 33-char string**. GitHub's required-status-check matcher
treats the ruleset's required context as a literal token; the new
50-char workflow job name is a different token even though position
33 is ` ) ` -> `   ` (33rd character of the new name is space, not
`) `), so the prefix-pretense collapses one character into the
suffix.

The PR Checks UI showed the longer-name job passing, so the
"no checks failed" view looked clean. The ruleset gate was actually
BLOCKED for the entire window between #763 and the rename revert in
#773. Only a forced re-read of the ruleset view surfaced it.

## Contract

The ruleset's `required_status_checks.contexts[]` strings must equal
a `name:` (or the bare job-key fallback) of some job in some
`.github/workflows/*.yml` of the same repo. The match must be:

- **Exact case-sensitive string equality.** No prefix, no substring,
  no regex, no slug normalization.
- **Or:** the ruleset context equals the bare job key (the YAML
  `jobs.<key>:` identifier) when the job lacks a `name:`. Bare-key
  fallback is allowed but is brittle: a later rename of the bare
  key immediately breaks the contract again. New workflows should
  always set `name:` to disambiguate.

## How to rename safely

When renaming a workflow job (or adding a pre-gate that changes the
job's logical scope), update BOTH sides in the same PR:

1. Edit `.github/workflows/<file>.yml` to change the `name:` field.
2. Run `gh api /repos/<owner>/<repo>/rulesets/<id>` to fetch the
   current ruleset JSON.
3. Replace every reference to the old `name:` in the ruleset's
   `rules[].parameters.required_status_checks[].context` array.
4. PATCH the ruleset back: `gh api --method PATCH
   /repos/<owner>/<repo>/rulesets/<id> -H 'Accept: application/vnd.github+json'
   -F rules=...` or use the GitHub UI ("Rulesets" → "Protect main").
5. Commit both the workflow yml change and (if you author the
   ruleset locally) the `.github/rulesets/*.json` change in one PR.

A one-liner drift probe (run from the repo root):

```bash
python3 -c "
import sys, yaml, json
from pathlib import Path
named = set()
for p in sorted(Path('.github/workflows').glob('*.yml')):
    for k, v in (yaml.safe_load(p.read_text()) or {}).get('jobs', {}).items():
        if isinstance(v, dict) and isinstance(v.get('name'), str) and v['name'].strip():
            named.add(v['name'])
        else:
            named.add(k)
print('match surface:', sorted(named))
" && gh api /repos/<owner>/<repo>/rulesets/<id> \
     --jq '.rules[] | select(.type=="required_status_checks")
           | .parameters.required_status_checks[].context'
```

Any context in the output that is not in the match surface is a
drift. Issue #774 in three lines of bash.

If you DO author the ruleset as a local file (`.github/rulesets/*.json`),
the same logic is encoded in `lib/ci_ruleset.py` and exercised by
`tests/test_ci_ruleset_contract.py` and by the `/dev-kit:ci-doctor`
cross-check (see below). When the local ruleset file is absent the
checks emit an INFO row telling you to fall back to the
`gh api` probe above.

## Diagnostics

### `gh pr view <N> --json statusCheckRollup`

The PR Checks UI can hide ruleset-context mismatches behind the
"checks passed" view. The raw JSON surfaces both:

- The actual workflow job names that ran.
- The ruleset's required-status-check contexts.

Diff the two and the divergence is plain text.

### `gh api /repos/<owner>/<repo>/rulesets/<id>`

Returns the ruleset's full JSON (legacy form:
`rules[].parameters.required_status_checks.contexts[]`; current form:
`rules[].parameters.required_status_checks[].context` and
`integration_id`). Compare directly with the workflow job
`name:` values to find drift.

### `/dev-kit:ci-doctor`

Adds one new row to the audit summary, labelled
`ruleset workflow contract`. States:

- `INFO` — no `.github/rulesets/*.json` author-side; the
  contract is unreadable from disk, fall back to `gh api` above.
- `PASS` — every required-status-check context in the local
  ruleset matches a workflow job `name:` (or a bare key).
- `WARN` — a ruleset JSON file was unparseable (surfaces the file
  path; probably a hand-edit gone wrong).
- `FAIL` — at least one required context has no matching workflow
  job. The detail names every offending pair plus a remediation
  hint pointing at this doc.

## Reference

- `lib/ci_ruleset.py` — shared loader + cross-check used by both
  the regression test and the ci-doctor row.
- `tests/test_ci_ruleset_contract.py` — Iron Law L1 regression
  test; pins every supported ruleset JSON shape (legacy
  `parameters.required_status_checks.contexts[]`, modern
  `parameters.required_status_checks[].context`,
  top-level array-of-rulesets) plus the contract itself
  (match / mismatch / bare-key / no-ruleset cases).
- `tests/fixtures/ci_ruleset_contract/` — fixture trees
  (match / mismatch / empty_job_name / parameters_form / array_form)
  exercised by the regression test.
- `skills/ci-doctor/SKILL.md` — documents the new
  `ruleset workflow contract` row alongside the rest of the audit.
- Issue #774 — original root-cause writeup; the issue body
  contains the full chain (#763 -> #773 -> #774).
