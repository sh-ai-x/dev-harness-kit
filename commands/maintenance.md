---
description: Run the code-sanity (CC-1..8 / OE-1..8 / VM-1..4) gate on a PR or arbitrary file set. Mirrors .github/workflows/maintenance.yml. Emits a single **Verdict:** line + per-axis breakdown.
argument-hint: --pr N [--diff <files>]
alpha: state
user-invocable: true
---

# /dev-kit:maintenance

Code-sanity gate. Judges the PR diff (or `--diff <paths>`) against the 20-checkbox rubric (CC-1..8 clean code + OE-1..8 over-engineering + VM-1..4 value/meaning).

The body of this command lives in
[`skills/maintenance/SKILL.md`](../skills/maintenance/SKILL.md). The
command itself is a thin wrapper that forwards `$ARGUMENTS` to the
skill.

## Output

A single-line verdict:

```
**Verdict:** Approve
**Verdict:** Changes Requested
**Verdict:** Blocked
```

Followed by per-axis scores (code_sanity 0-10, docs_coverage 0-10,
scope_discipline 0-10), per-finding inline comments, and the
docs-updated sub-gate verdict.

## Mapping

| `code_sanity_score` | CI Verdict |
|---|---|
| ≥ 8.0 | **Approve** |
| 5.0 – 7.99 | **Changes Requested** |
| < 5.0 | **Blocked** |

`Blocked` short-circuits the docs check.

## When to use

- PR touches production code and the operator wants the maintenance gate verdict before merge.
- Operator audits an arbitrary file set (`--diff lib/foo.py bin/bar.sh`) outside of a PR.
- The repo's GH-Actions workflow is at minute cap and the operator needs a local mirror verdict.

## Related

- `skills/maintenance/SKILL.md` — the spec / judge prompt body.
- `.github/workflows/maintenance.yml` — the CI gate (claude-code-action invocations).
- `lib/maintenance_gate.py::combine_verdict` — verdict-extraction helper used by `bin/review-local.sh` and `lib/pr_verify.py`.
