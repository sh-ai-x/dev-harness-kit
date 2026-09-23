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

The ruleset itself lives at
**`.github/rulesets/protect-main.json`** (the local SSOT for ruleset
id `20232367`, name `protect main (admin PAT bypass)`). The SSOT
covers both the required-status-check contract above AND the
bypass-actors contract below — see [Bypass actors SSOT](#bypass-actors-ssot)
for the half this file added in the same commit. `bin/ruleset-sync.sh`
PATCHes the live GitHub ruleset from the local SSOT so the two stay
in lock-step.

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

## L3 evidence sub-gate PR comment (issue #803)

The `severity gate (review + security)` job in
`.github/workflows/review.yml` contains a deterministic
**L3 evidence sub-gate** step (`L3 evidence gate (PR body must
quote test count)`, lines 841-890). When `touches_prod=true` and
the PR body lacks a quoted pytest tail line, the step fails with
`::error::PR body lacks a quoted pytest tail line.`.

Prior to issue #803, the only trace of *why* the gate failed lived
in the GitHub Actions log. A contributor reading the PR saw two
"Approve" verdict comments + a red check with no attached reason
and had to open the Actions log to discover the cause (observed
on PR #797).

Starting with PR #805, the L3 step ALSO posts a PR-visible comment
with the same diagnostic. The body lives in
`.github/review-yml/l3-evidence-fail.md` (a separate file, NOT
inline in the YAML — multi-line bash strings inside a YAML literal
block trip the YAML scanner on `**` and `<<` patterns) and is
`cat`-fed to `gh pr comment --body-file`. The body enumerates the
four accepted pytest summary forms, points the author at the PR
template's Iron Law L3 section, and links issue #803 so a
contributor who finds the comment can read the full bug context.

The companion regression guards pin both contracts:

- `tests/test_review_yml_l3_pr_comment.py` — pins the L3 step's
  PR-comment behavior (body file path, body content, fail-only-in-
  prod-branch, advisory branch remains silent).
- `tests/test_fork_pr_review_combined_status.py` — pins the
  `fork-pr-review/ai-judges` combined status description shape so a
  deterministic sub-check failure isn't reported as a judge
  failure.

Why this matters here: the `severity gate` job name in
`.github/workflows/review.yml` is part of the ruleset contract
above. The L3 step is inside that job and runs BEFORE the
`Combined verdict gate` step that produces the job's overall
conclusion. A failure in L3 therefore flips the job's check to
`failure` even when both LLM judges returned Approve — the
combined status now distinguishes these two cases so the
branch-protection gate doesn't blame the judges.

## Bypass actors SSOT

The same ruleset carries a second contract: the
`bypass_actors[]` block renders in the GitHub UI as the
**"Allow specified actors to bypass required pull requests"**
checkbox list (Settings → Rules → selected ruleset →
"Bypass list"). Each entry is one checkbox in that UI; clearing
the list — or removing the only `RepositoryRole:ADMIN`
entry — silently blocks admin/maintain emergency merges and
also kills the `DEV_KIT_GITHUB_TOKEN` PAT push path that
`.github/workflows/version-bump.yml` relies on for merge-queue
bump commits (see `version-bump.yml:38-47` for the rationale).

`.github/rulesets/protect-main.json` is the local SSOT for
this block; the regression test
`tests/test_ruleset_bypass_actors.py` pins it; `/dev-kit:ci-doctor`
surfaces it as a dedicated `ruleset bypass actors` row so the
operator sees drift before opening a PR.

### Contract

| Field              | Required           | Notes |
|--------------------|--------------------|-------|
| `bypass_actors[]`  | non-empty          | empty list = "checkbox cleared" state, surfaces FAIL |
| At least one entry | `actor_type=RepositoryRole`, `repository_role=ADMIN`, `bypass_mode=always` | The whole point of the SSOT; without it admins can't push to main |
| Optional entries   | `MAINTAIN` / `WRITE` (any role), `User`, `Team`, `Integration` | Mirrors the GitHub REST `bypass_actors[]` enum verbatim |

`bypass_mode` semantics (from the GitHub REST API):

- `always` — the actor can push directly to the protected ref
  AND bypass required checks on PR branches. Required for the
  admin/maintain role entries; anything else keeps the bypass
  checkbox effectively unchecked.
- `pull_request` — bypass only on PR branches. Used by
  `RepositoryRole:WRITE` for the bot push path (pushes to PR
  branches, not main).
- `exempt` — actor is exempt from the ruleset entirely. Rare;
  not currently used by the SSOT.

### Restoring the checkbox after a ruleset wipe

1. Restore the SSOT to its committed shape:
   `git checkout origin/main -- .github/rulesets/protect-main.json`
2. Verify locally:
   `bin/ruleset-sync.sh --dry-run`
3. Apply to GitHub:
   `bin/ruleset-sync.sh`
4. Confirm the drift check passes (idempotency guard):
   `bin/ruleset-sync.sh --check` (exit 0 = in sync)

`bin/ruleset-sync.sh --check` is safe to wire into a pre-PR hook
or a `/dev-kit:ci-doctor` row — it exits 0 when the SSOT is in
sync and 1 with a remediation hint otherwise.
