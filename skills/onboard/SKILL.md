---
name: onboard
category: onboard
description: 0-arg new team member onboarding (MUST-47). 30 min productive. CLAUDE.md + .dev-kit + eval baseline auto.
alpha: state
when_to_use: |
  - User types /dev-kit:onboard <github_username>
allowed-tools: Read Write Bash Glob Grep
model: opus
disable-model-invocation: false
---

# /dev-kit:onboard — New team member 30-min productive

## Auto actions

1. Update CLAUDE.md (§0 "team member: <name>")
2. Update codebase-map §3
3. Auto-delegate first task (`build-tdd`)
4. Auto PR (hand-off attached)
5. Capture eval baseline (add user signature to golden set)

> Note: when the new member is the only owner (single-operator repo),
> `/dev-kit:babysit-pr` waits for human review by default; they can
> pass `--operator-is-only-human --rationale "<text>"` to opt out and
> auto-merge their own green PRs (the bypass is refused if CODEOWNERS
> or the collaborators API lists any alternate owner).

## Output

- `.dev-kit/onboarding-<username>.md` (progress guide)
- First PR auto-created