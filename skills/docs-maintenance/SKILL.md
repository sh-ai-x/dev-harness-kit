---
name: docs-maintenance
category: audit
description: Audit repository documentation with the project README as the highest-priority document. The README is always audited AND verified every run, and updated when needed.
alpha: analysis
when_to_use: |
  - User types /dev-kit:docs-maintenance
  - User asks to clean outdated docs, update the README, or "fix the README"
  - A plugin, skill, cache-refresh workflow, or repository layout has changed
  - Documentation contains hard-coded counts, exhaustive inventories, or stale commands
allowed-tools: Read Write Edit Glob Bash
disallowed-tools: WebFetch Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

## What it does

The project README is the most important document in any repository.

This skill treats it as such: every run **always audits and verifies the README** for false-positive (claims that don't exist) and false-negative (capabilities the README misses) drift, and **updates the README when needed** to reflect the current source of truth. A correct no-change run still produces a per-entry `kept | updated | removed` audit trail — the audit is the deliverable, not a forced edit. It then audits the rest of the documentation, removes or marks superseded operational guidance, and keeps volatile inventory details out of prose. It is intended for documentation changes only; it must not silently change product behavior.

## Reusable meta prompt

Use this prompt when delegating the same maintenance task to another agent:

```text
Audit this repository's documentation against the current implementation. The project README is the highest-priority document — always audit AND verify it every run, and update it when needed.

1. README first (mandatory, every run):
   a. Update the README so its commands, paths, installation flow, cache-refresh workflow, and verification steps match the current source of truth (scripts, manifests, tests, recent commits).
   b. Verify the README: every path, command, skill name, script, flag, manifest field, and workflow claim must exist and behave as described. Surface anything that is missing, stale, or undocumented.
2. Find documents that are superseded, contradictory, or refer to removed files and remove them only when they have no historical or policy value. Preserve ADRs and historical records, but correct current-state claims or label them as historical.
3. Update the directly related operational docs to describe the current commands, paths, cache-refresh workflow, installation flow, and verification steps. Prefer the repository's scripts, manifests, tests, and recent commits as sources of truth.
4. Do not record volatile inventory facts in prose: skill counts, exhaustive skill lists, generated cache versions, commit SHAs, or other values that change whenever the repository evolves. Describe how to discover them instead.

Check both directions: every documented path/command/feature must exist (false-positive check), and every current user-facing capability must be represented where needed (false-negative check). Treat each README claim as a first-class entry in both checks. Record `documented → verified`, `documented → missing`, and `exists → undocumented` evidence, resolving the latter two or explaining an intentional internal/historical exception. Run the narrow documentation/skill validation plus the relevant test suite. Report deleted files, updated files, validation commands, and quoted exit codes or test counts.
```

## Workflow

### 1. Build the documentation map (README first)

- Open the project `README.md` (or whichever README the repo uses) and read it end-to-end before touching anything else. Every claim in it is a candidate for the validation pass in step 4.
- Read the documentation index files, relevant rules, manifests, and the scripts the README names.
- For staleness, rely on `tools/check_doc_lifecycle.py` (CI). The `rg`/`git log` heuristic that used to live here now lives only in the proposal that introduced the gate; this skill is the audit, not the staleness sniff.

### 2. Classify before changing

- **Remove** a document only when it is an obsolete operational document with no unique policy, rationale, or historical value.
- **Update** current guides when their commands, paths, or behavior no longer match the source of truth.
- **Preserve** ADRs and changelogs as historical records; revise only misleading present-tense claims and state the historical scope when needed.
- Do not create a second source of truth for generated metadata. Point to the manifest, filesystem discovery, or validation command instead.

### 3. Refresh the README (mandatory)

This step is not optional. If the README does not need changes, the run still records "no change" against each entry below — that is the audit trail, not a reason to skip.

Keep the README answer-first and task-oriented. For cache updates, document the maintained updater script, its dry-run mode, environment overrides, the manifest/cache verification output, and the required client restart. Explain Claude and Codex paths separately when their commands or cache locations differ.

Replace exhaustive lists and fixed counts with stable concepts and discovery commands. Do not add current versions, commit identifiers, skill totals, or manually maintained inventories merely to make the README look complete.

For every README entry touched, record one row in the report: `entry → kept | updated | removed` with the source-of-truth file that justified the change.

### 4. Validate the result (README first)

Run `rg` searches for removed paths and stale commands, verify Markdown links and code examples against the filesystem, validate every changed skill with the repository's skill validator, and run the focused tests plus the full relevant suite. Report the exact commands and quoted exit codes/test counts.

README verification runs before the rest of the documentation check. If the README fails false-positive or false-negative, fix it and re-run before touching the rest of the docs.

Perform a bidirectional documentation check before declaring success:

- **README false positive check:** extract every path, command, script, flag, env var, manifest field, link, and workflow claim from the README and confirm each one exists and behaves as described. A README must not claim a file or command that is absent from the repository or unavailable in the stated client.
- **README false negative check:** enumerate the repository's user-facing CLI commands, scripts, public skills, configuration files, and recent user-facing changes; confirm each one is reachable from the README (directly or via a single link). Anything a new user would need to find by reading the README must be there.
- **Repo-wide false positive check:** extend the same extraction to every other document and confirm existence.
- **Repo-wide false negative check:** inspect current manifests, executable scripts, user-invocable skill frontmatter, README-referenced workflows, and recent user-facing changes, then confirm that each required current capability has a suitable documentation entry. Do not hide a real feature merely to avoid a stale claim.
- **Evidence rule:** record each checked claim as `documented → verified`,
  `documented → missing`, or `exists → undocumented`. README rows are
  reported first; the rest follow. Resolve the latter two before
  completion, except for intentionally internal or historical items;
  explain those exceptions in the report.

Use the filesystem, manifests, executable `--help` output, tests, and recent
commits as evidence. Do not treat a prior README, a generated cache, or an
unverified agent assertion as proof that a documented item exists.

## Next step

After this skill completes, invoke `/dev-kit:review` for a change review, or `/dev-kit:ship` once the repository's normal review and CI gates are satisfied.
