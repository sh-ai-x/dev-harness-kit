---
name: maintenance
category: audit
description: Code-sanity gate. Judges the PR diff against the 20-checkbox rubric from eval/prompts/judge-code-sanity.md (CC-1..8 clean code, OE-1..8 over-engineering, VM-1..4 value/meaning). Mirrors .github/workflows/maintenance.yml's gate logic. Verdict mapping: code_sanity_score >= 8.0 -> Approve; 5.0..7.99 -> Changes Requested; < 5.0 -> Blocked. Plus the docs-updated sub-gate (PR must touch docs/ OR carry a `docs-not-required:` marker in the body OR not touch production code).
alpha: enforcement
when_to_use:
  - User types /dev-kit:maintenance
  - PR touches production code and the operator wants the maintenance gate verdict before review
  - Operator asks to audit a PR for clean-code / over-engineering / value-axis concerns
allowed-tools: Read Grep Glob Bash
model: opus
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

Code-sanity gate. Runs the 20-checkbox rubric (CC-1..8 + OE-1..8 + VM-1..4) from `eval/prompts/judge-code-sanity.md` on the consolidated PR diff. Three axes are scored 0-10 (`code_sanity_score`, `docs_coverage_score`, `scope_discipline_score`); composite maps to a verdict.

## What it does

1. Resolves the target PR (`$ARGUMENTS` → `--diff <repo>/pull/<N>`, or `--diff <files>` for ad-hoc review).
2. Identifies the production-touch surface: which top-level dirs does the diff actually change?
3. Judges CC-1..8 (vague names, oversized functions, dead code, magic constants, copy-paste, swallowed errors, type unsafety, stale comments).
4. Judges OE-1..8 (single-implementer abstract base classes, YAGNI flags, premature optimization, excessive layering, factory/strategy/DI for one impl, deep inheritance, file-per-class sprawl).
5. Judges VM-1..4 (stated purpose, no noise, scope discipline, "diff earns its lines").
6. Runs the docs-updated sub-gate (path-level + registry-index). The **registry-index** check fires when the PR adds a new `skills/<name>/SKILL.md` or `commands/<name>.md` (status `added`); the operator must also touch one of the manually maintained registry docs (`README.md`, `docs/skills/README.md`, `docs/skills/README.ko.md`, `commands/README.md`), or carry `docs-not-required:` in the PR body. The auto-generated `skills/README.md` does NOT count — it's a near-no-op for the gate. The path-level check still applies for non-skill/command prod changes that touch `lib/` / `bin/` / `tools/` / etc. without any `docs/*` update. FAIL otherwise — downgrades `Approve` to `Changes Requested`.
7. Emits a single-line verdict at the top of the response in the exact form below; the gate's verdict-extraction helper (`lib/maintenance_gate.py`) parses this.

## Verdict mapping

| `code_sanity_score` | Verdict |
|---|---|
| ≥ 8.0 | **Approve** |
| 5.0 – 7.99 | **Changes Requested** |
| < 5.0 | **Blocked** |

`Blocked` short-circuits the docs check — even a perfectly-documented PR fails if the judge flags critical over-engineering or clean-code violations.

## Output format

The summary MUST begin with a single line exactly of the form:

```
  **Verdict:** Approve
  **Verdict:** Changes Requested
  **Verdict:** Blocked
```

Followed by the per-axis breakdown (code_sanity, docs_coverage, scope_discipline), then per-finding inline comments for every CC/OE/VM item flagged.

## When NOT to use

- The PR is purely a docs/ change with no production code touched — skip the gate.
- The PR is a `chore(release): bump dev-kit to vX.Y.Z` auto-bump — `.github/workflows/maintenance.yml:75` short-circuits; no need to invoke the LLM judge.
- The diff exceeds ~3000 lines — split into per-file audits or run `tools/inspect.py` first.

## Related

- `eval/prompts/judge-code-sanity.md` — the canonical 20-checkbox rubric.
- `.github/workflows/maintenance.yml` — CI gate (uses `claude-code-action` to invoke this skill at L208-209).
- `lib/maintenance_gate.py` — `combine_verdict` + `extract_verdict_from_stdin` helpers.
- `commands/maintenance.md` — slash-command wrapper.
