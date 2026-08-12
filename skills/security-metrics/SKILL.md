---
name: security-metrics
category: security
description: Calculate a deterministic 0-100 security scorecard for the current repository and render an evidence-backed Markdown table for OWASP Top 10 categories.
alpha: enforcement
when_to_use:
  - User types /dev-kit:security-metrics
  - User asks for a repository security score or Markdown security metric
allowed-tools: Read Grep Glob Bash
disallowed-tools: Write Edit
model: sonnet
user-invocable: true
---

## What it does

Use this skill only when explicitly requested. It complements `/dev-kit:security`:
the OWASP skill performs a deep evidence review, while this skill produces a
repeatable scorecard from repository facts and lightweight static checks.

## Run

From the repository root:

```bash
python3 skills/security-metrics/scripts/score_security.py . \
  --output security-metrics.md
```

The command prints the overall score and writes a Markdown report containing a
0-100 score for A01-A10, evidence, and deductions. It does
not install dependencies, call an external scanner, modify source files, or
run arbitrary project commands.

## Scoring contract

- Every category starts at 100 and applies only deterministic deductions.
- Scores are clamped to 0-100; the overall score is the arithmetic mean of the
  ten category scores, rounded to the nearest integer.
- `PASS` means no rule fired and `REVIEW` means one or more rules fired. All
  built-in checks are local and produce one of those two statuses.
- A score is a triage metric, not proof of security. Use `/dev-kit:security`
  for the evidence-backed OWASP review before release or a major refactor.
- Do not suppress a deduction in the report. If a rule is a false positive,
  record the reason and address the rule in a follow-up change to the scorer.

## Output

Report the generated table and call out the lowest-scoring categories first.
Include the command, repository path, overall score, and whether the report was
generated from a clean working tree when that information is available.

## Next step

Use `/dev-kit:security` for the full OWASP evidence review, then
`/dev-kit:ship` when the repository is ready for release.
