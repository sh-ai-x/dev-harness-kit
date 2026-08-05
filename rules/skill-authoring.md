---
paths:
  - "skills/**/SKILL.md"
---

# SKILL.md authoring rules (dev-harness-kit)

These rules apply when creating or editing any `SKILL.md` in this repo.

## File location (mandatory)

- **Flat**: `skills/<skill-name>/SKILL.md` — one level, no category subdir.
- Directory name MUST match `name:` frontmatter.

## Frontmatter (mandatory fields)

```yaml
---
name: <skill-name>            # MUST match directory name (kebab-case)
category: <category>          # MUST be one of 14 allowed values
description: <one-line English summary>
when_to_use: |
  - User types /dev-kit:<skill>
  - <other trigger 1>
  - <other trigger 2>
allowed-tools: Read Write Glob   # space-separated
disallowed-tools: Bash Edit     # space-separated (optional)
model: opus                    # default sonnet, override here
disable-model-invocation: false # true for HUMAN-USE only
user-invocable: true           # false for MODEL-USE only
---
```

### Human-use frontmatter example

```yaml
---
name: build
category: build
description: 0-arg build stage. Run per-step sub-agents via harness-runner.
disable-model-invocation: true   # prevent self-invocation
user-invocable: true             # expose as /dev-kit:build
---
```

### Model-use frontmatter example (contrast)

```yaml
---
name: build-tdd
category: build
description: Red-Green-Refactor cycle. Internal sub-skill of /dev-kit:build.
disable-model-invocation: false  # model may auto-invoke
user-invocable: false            # hidden from /dev-kit: skill list
safety:
  safety_valve: 8
  convergence: composite
  dedup_metric: identical-answer-cycle=2
---
```

## Human-use vs Model-use (mandatory classification)

| Class | `user-invocable` | `disable-model-invocation` | Slash exposed? |
|---|---|---|---|
| **Human-use** (stage commands, utilities, shortcuts) | `true` | `false` (default) or `true` | ✅ Yes |
| **Model-use** (internal building blocks) | **`false`** | `false` (default) | ❌ No |

The current human-use and model-use inventories are defined by each skill's
`user-invocable` frontmatter. Do not duplicate those inventories or their
counts in this rule; inspect `skills/*/SKILL.md` when needed.

> Note: `simplify` → `refactor` rename (this PR) and `build-simplify` → `build-refactor` rename. The verb `simplify` still appears in the human-facing description of `refactor` (e.g., "refactor everything" is a common user phrase) but the skill name is `refactor`. For the deletion counterpart, see `/dev-kit:prune`.

> Note: `plan-ralph` was merged into `plan` (issue #58) — the plan skill is
> now self-contained and does not delegate to a non-invocable sub-skill.

## Description as trigger phrase (mandatory)

The `description:` frontmatter field is **not** a summary of what the skill
does — it is the **trigger phrase** Claude uses to decide whether to
auto-invoke this skill from a user prompt. Treat it accordingly.

### Rules

1. **Write the description as the user would phrase the request**, not as
   a docstring. If the user says "fix the broken build," the description
   should contain words that match — "fix", "build", "broken" — not
   abstract nouns like "build error remediation workflow."
2. **Front-load the trigger verbs**. The first 8–12 words are what the
   skill-discovery index exposes; the rest is padding. Put the strongest
   match terms in the lead.
3. **Avoid jargon unique to dev-kit internals** unless the user is
   expected to know them. The trigger index doesn't reward
   `must_classify_dispatch_mode`; it rewards "parallel" / "sequential" /
   "auto-classify" because those are words the user actually types.
4. **No marketing copy.** "The world's best debugging skill" wins zero
   triggers; "use when encountering any bug, test failure, or unexpected
   behavior" wins the bugs.
5. **One sentence, ≤ 20 words** is the sweet spot. Two sentences is the
   ceiling; if you need three, the trigger is too broad and the skill
   should split.

### Why this matters

Claude Code's skill auto-invocation matches user prompts against each
skill's `description:` field. A description that reads like internal
documentation will never fire, regardless of how useful the skill is.
A description that mirrors how the user phrases the request fires
reliably and saves the user from typing `/dev-kit:foo` for the common
case.

### Counter-example

Bad:
```yaml
description: Comprehensive code quality enforcement skill that integrates
  static analysis, type checking, and linting workflows for the
  dev-harness-kit harness runner.
```

Good:
```yaml
description: Use when code review flags a quality issue, lint fails,
  or you want to enforce typing/lint before commit.
```

The bad version describes the skill's purpose; the good version
describes when the user would invoke it.

## Body (mandatory style)

- **All text in English** (no Korean, even in code comments).
- `description:` ≤ 1 line.
- `when_to_use:` as bullet list, 2-5 items.
- Section headers `## H2`, `### H3` (no H1 — title is in frontmatter).
- Code blocks tagged with language (` ```ts `, ` ```bash `, etc.).
- First section: **what it does** in 1 paragraph.
- Last section: **next step** (which other skill to invoke).

## Forbidden patterns

- ❌ `it.only` / `it.skip` / `console.log` debugging in skill body.
- ❌ References to deleted files (`INTEGRATION.md`, `AX.md`).
- ❌ Hard-coded paths (use `skills/<name>/SKILL.md` style references).
- ❌ Claims without evidence ("works", "passes", "fast") — quote test counts, durations.

## Validation

- `tests/test_naming.py` enforces: `name` == directory name; `category` ∈ 13 values.
- `tests/test_smoke.py` enforces the repository's internal skill-layout invariant. Update its test fixture when adding a skill, but do not copy the resulting count into documentation.

## L6 skill gate — the alpha must be enforceable

See `iron-laws/index.md` L6 + L7 for the rule + the `state | enforcement | analysis`
allow-list. This file adds only the SKILL.md-authoring specifics:

- **Every new `SKILL.md` (added after `origin/main`) MUST declare
  `alpha:`** in frontmatter. The gate is enforced by
  `tests/test_skill_governance.py` against `git ls-tree -d origin/main skills/`
  as the baseline.
- **`analysis` requires justification** — what distinct user intent does
  this slash serve that the existing `analysis` set doesn't, and why does
  it need its own entrypoint instead of being a flag on an existing one?
  The justification goes in the PR body.
- **Lint**: `python3 -m pytest tests/test_skill_governance.py -v`. Falls
  back to local `main` then `git log --diff-filter=A main -- skills/` if
  `origin/main` is unreachable.

**Out of scope** (gate does not cover):
- Renames / moves — the lint compares directory *names*, so a `foo → bar`
  rename in the same PR appears as a remove + add and the new skill must
  declare `alpha:`.
- Sub-skill splits — both `foo` and `foo-sub` must declare `alpha:`
  independently; sub-skills inherit no alpha from their parent.
- Documentation-only SKILL.md files — still violations if `alpha:` is
  missing; the gate does not distinguish skill-shaped docs from real skills.
- Existing skills — out of the gate's scope by design (migration is a
  separate effort; the gate applies only to skills added after `origin/main`).
