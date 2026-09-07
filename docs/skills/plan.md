# Plan skill — `mod` mode behaviour delta

The `/dev-kit:plan` skill behaves the same in `full` / `lite` / `undev`
and emits additional structure only in `mod`. This page documents the
delta.

## Detection

Read `DEV_KIT_MODE` once at the top of Gate 4/5:

- `os.environ.get("DEV_KIT_MODE")` — Layer 1 (per-session shell override)
- `.claude/settings.json` `env.DEV_KIT_MODE` — Layer 2 (committed project choice)

If the resolved value is `mod`, Gate 4/5 takes the dependency-aware
path. Any other value preserves the pre-mod emit shape exactly.

## Gate 4/5 — decompose (mod path)

After the operator has chosen the step titles (the multi-step picker
that runs in all four modes), run **one `AskUserQuestion` per step**
with `multiSelect: true`:

> "Which earlier steps must complete before step N starts?"

The choices are the step numbers + titles for every earlier step. An
empty answer marks the step as a leaf.

After collecting the per-step lists:

1. Build the step list (one dict per step) and call
   `lib/plan_dependency.py:compute_dag(steps)`.
2. If `result["valid"]` is `False`, refuse to emit:
   - `result["missing"]` lists upstream step numbers the operator
     referenced that don't exist. Show the list and ask the operator to
     fix.
   - `result["cycles"]` is one entry per offending SCC. Show the first
     cycle and ask the operator to break the edge.
3. Otherwise write the per-step `dependencies:` block into each
   `step<N>.md` body and persist the same list into the step dict in
   `phases/<phase>/index.json` so the dispatcher consumes it without
   further wiring.

## `step<N>.md` template extension

The mod-only `dependencies:` block sits next to the canonical sections:

```markdown
# step<N>.md

dependencies: [1, 2]

## Status
pending

## Read first
...

## Task
...

## Acceptance Criteria
...

## Verification & Status Update
...

## Don't
...
```

`lib/intent_integrity.py:_parse_step_file` already coerces the
`dependencies:` values to `int` via `_to_int()`. The IC-3 gap check
then enforces `step.md.dependencies ⊆ index.json.steps[*].step` at
build time. Non-mod modes do NOT write the `dependencies:` line, so
IC-3 stays silent for them (consistent with today).

## Gate 5/5 — emit (mod path)

PRD §4 (Phase plan) gains a "Dependency DAG" subsection listing each
edge from `compute_dag(steps)["topo"]`:

```markdown
### Dependency DAG
- step2 → step1 (data model before API)
- step3 → step1, step2
- step4 → step3
```

Skip the subsection when every step is a leaf (empty `dependencies:`
across the board).

## DoD impact

The 6-bullet DoD in `skills/plan/SKILL.md` Gate 5/5 does NOT gain a
7th bullet for mod. The new structure is enforced by:

- `lib/plan_dependency.py:compute_dag` — pre-write cycle / missing check
- `lib/intent_integrity.py` IC-3 — step.md ↔ index.json consistency at
- `lib/dispatch_classifier.py:_has_dependency_edge` — classifies the
  phase as `sequential` whenever any step declares an edge

The DAG subsection in PRD §4 is informational, not a gate.

## Out of scope (intentionally)

- **Hard runtime gate** refusing to start a step whose `dependencies`
  are not yet `completed`. `dispatch_classifier` already favours
  sequential when an edge is present; that is sufficient for the
  common parallelism regression. A future PR can add a hard gate if
  data shows it's needed.
- **Cross-step `verifies` / `consumes` fields.** Only `dependencies:`
  (list of step numbers) is wired today. Other implicit-dep fields
  (already supported by `dispatch_classifier.py` for `depends_on` /
  `consumes` / `writes` / `partition`) can be added in a follow-up.

## Related

- `lib/plan_dependency.py` — pure DAG validator (`compute_dag`)
- `lib/intent_integrity.py:_parse_step_file` + IC-3 — consumes the
  emitted `dependencies:` block
- `lib/dispatch_classifier.py:_has_dependency_edge` — reads
  `depends_on` / `consumes` from `phases/<phase>/index.json`
- `docs/scopes/modes.md` — `mod` mode rationale + role-config section
- `lib/role_config.py` — the role half of `mod` (independent of the
  dependency half)