---
name: plan
category: plan
description: 0-arg plan stage. Take 1-line idea → PRD.md + phases/<name>/{index.json, step<N>.md} in 5 gates. Quantified value (cost/LTV) + ambiguity loop (0-10) replace the old 5-question grill-me.
alpha: state
when_to_use: |
  - User types /dev-kit:plan with an idea
  - User wants PRD regenerated
  - Resume from .dev-kit/decision-log.md (HOLD after pause)
allowed-tools: Read Write Glob AskUserQuestion Skill
disallowed-tools: Bash Edit NotebookEdit WebFetch
model: opus
disable-model-invocation: true
user-invocable: true
safety:
  safety_valve: 8
  convergence: composite (ambiguity_score <= 3 AND value_score >= 3.0)
  narrowed_delta: bool
  dedup_metric: identical-ambiguity-cycle=2
  user_interrupt: true
---
> [← Skills index](../../README.md)

## Worktree precondition (REQUIRED — fail-closed)

Before answering Gate 1, verify the cwd is inside a worktree, not the main repo
checkout. `hooks/worktree-guard.sh` hard-blocks every `Write` from the main
checkout; if you proceed without checking, gate answers are captured but
`PRD.md` is never emitted and the failure surfaces only when `/dev-kit:build`
later fails on missing phase.

Detect via `Read` on `./.git`:

| Result | Meaning | Action |
|---|---|---|
| file content starts with `gitdir:` | inside a worktree | proceed to Gate 1 |
| `Read` fails because `./.git` is a directory | main checkout | **STOP**. Do NOT ask Gate 1. Tell the parent agent: "Worktree required. Per `rules/git-workflow.md`, every task = new worktree + client handoff + new branch. Run: `git fetch origin main && git worktree add -b <type>/<slug> .worktrees/<slug> origin/main` — Claude Code opens a new session in that path; Codex spawns/hand-offs a subagent with that path as cwd, then re-invokes `/dev-kit:plan`." |
| `Read` fails for any other reason (no repo, file missing) | outside any git repo | proceed (no worktree rule applies) |

This mirrors the discriminator in `hooks/lib/worktree-detect.sh`
(`--git-dir == --git-common-dir`) but uses `Read` instead of `Bash` because
plan's `disallowed-tools: Bash` blocks the shell form.

# /dev-kit:plan — Idea → PRD.md + phases (5 gates, 1 Ralph loop)

Self-contained. The earlier `plan-ralph` dispatch was absorbed (issue #58). No
sub-skill invocation; everything below runs inside this single skill invocation.

## Interview consume gate (REQUIRED unless `--skip-interview`)

Before Gate 1, consume the Phase 6 5-field safety contract from the
interview hand-off at `.dev-kit/hand-off/<step>.md`. Read it with the
`Read` tool (the LCS viewer was on `allowed-tools: ... Skill ...`
but the LCS substrate was dropped in #463; the contract now lives in
plain markdown that any consumer can read with `Read`).

Decision table (read `.dev-kit/hand-off/<step>.md` frontmatter):

| `status` (hand-off frontmatter) | Plan action |
|---|---|
| `ok` | Proceed to Gate 1; treat interview answers as canonical PRD §1 inputs. |
| `best-effort` | Proceed with a 1-line WARN to `.dev-kit/decision-log.md`; Gate 2 evidence-gate still applies. |
| `user-acknowledged` | Proceed; treat as `best-effort` for downstream gating. |
| `held` (or missing file / no `status` field) | **STOP.** Do NOT ask Gate 1. Tell the user: "Interview contract not clear. Per Phase 6 (issue #385), plan refuses to emit PRD while the interview hand-off is `held`. Run `/dev-kit:interview <plan-file>` first, then re-invoke `/dev-kit:plan`." |
| `--skip-interview` flag present | Skip the consume entirely (backward compat). Write a SKIPPED line to `.dev-kit/decision-log.md` so the audit trail is honest. |

Defence-in-depth: if the hand-off file is missing or frontmatter is
malformed, treat as `held`.

## Core goal

Planning artifacts only. No code, build, or deploy. Take a 1-line idea → run 5
gates in one Ralph loop → emit `PRD.md` + `phases/<name>/{index.json, step<N>.md}`
+ `.dev-kit/hand-off/plan→build.md`.

## Optional Linear preflight

At the start of a new plan task, invoke `/dev-kit:linear` once when Linear is
enabled or available. Continue normally on `LINEAR_SKIP` or an implicit
`LINEAR_ERROR`; do not invoke it inside the gates or ambiguity loop. See
`skills/linear/SKILL.md` for the reconciliation contract.

## Inputs / outputs

- **Input**: 1-line idea (from user prompt) + 1-5 AC + 1-3 non-goals.
- **Flag**: `--skip-interview` bypasses the interview consume gate
  below (Phase 6 backward compat).
- **Output**:
  - `PRD.md` — 6-section plan
  - `phases/<name>/index.json` — phase state machine (see "Phase JSON schema")
  - `phases/<name>/step<N>.md` — one per step (N=0..K-1)
  - `.dev-kit/decision-log.md` — accumulated Q&A + score deltas
  - `.dev-kit/loop-log.json` — narrowing per cycle (MUST-16)
  - `.dev-kit/hand-off/plan→build.md`
- **Cumulative**: every iteration appends to `decision-log.md` + `loop-log.json`.

## 5 gates (1 Ralph loop)

```
[1/5] frame        — goal + target user + 1-line situation
       ↓
[2/5] validate     — evidence (≥3 sources) + value_score + ambiguity loop
       ↓
[3/5] non-goals    — 3+ non-goals with rationale + breach-response
       ↓
[4/5] decompose    — phases/<name>/index.json + step<N>.md (per-step status)
       ↓
[5/5] emit         — PRD.md 6-section DoD pass + hand-off
```

The 8-gate structure (frame → evidence → diff → non-goals → socratic → phase-decompose → seed-convergence → prd-writer) was collapsed: G2 evidence, G3 diff-profit, and G5 socratic-deepen all probed "is this idea worth building?" with overlapping asks, and G7 seed-convergence was a numeric re-check of what G2 already asked for. The new G2 `validate` runs them once, with quantified inputs, in a single ambiguity loop.

## Gate 1/5 — frame

Ask the user (single message, in order):

| # | Field | Pass criterion |
|---|---|---|
| 1 | **goal** | one sentence: what we ship and what changes for the user |
| 2 | **target user** | one named persona (role + context) — not "everyone" |
| 3 | **situation** | one sentence: where the user is today, before this exists |

Accept whatever the user types. If any field is empty, ask once, then proceed
with `"<unspecified>"`. Write all 3 fields to `.dev-kit/decision-log.md` under
`# frame`. No exit code, no test count — this is input capture, not work.

## Gate 2/5 — validate (evidence + value + ambiguity loop)

This gate replaces the old "5-question grill-me" with a quantified loop. Three
numeric inputs feed one composite convergence test; the loop iterates only on
the dimension that is failing.

### 2.1 — Evidence (≥3 sources)

Ask once: "Cite 3 independent signals the target user actually wants this.
Independent = different origin (e.g. user interview, market data, analogue
product, prior failed attempt, paying-customer ask)."

Write each signal as `{source, claim, date}` to `decision-log.md`. If the user
gives <3, the gate fails. **No more "if vague, sharpen once"** — count, not
quality, gates this input.

### 2.2 — Value score (cost / LTV)

Compute, do not ask:

```
value_score = (LTV_per_user × reachable_users_year1) / total_cost
```

| Field | Source | Unit |
|---|---|---|
| `LTV_per_user` | user-declared (ask if missing) | $ or "value unit" |
| `reachable_users_year1` | user-declared or top-of-funnel estimate | integer |
| `total_cost` | sum of eng-hours × rate + infra $ + GTM | $ |

Write the 3 numbers + the formula result to `decision-log.md`. Threshold:
`value_score >= 3.0`. Below 3 → gate fails; tell the user the gap and the
single biggest lever to close it (cheaper, more reachable, or higher LTV — pick
one, do not enumerate).

### 2.3 — Ambiguity loop (0-10)

Compute, do not ask:

```
ambiguity_score_0 = 10
```

Each loop iteration asks **exactly one** question targeting the highest-leverage
unknown. After the answer, re-score:

| Knob | Question topic | Score impact |
|---|---|---|
| user | "Who is the first user, and what do they click/pay for first?" | -2 to -3 |
| pain | "What breaks for them today, and how often?" | -2 |
| scope | "What is the smallest version that pays for itself in 2 weeks?" | -2 |
| metric | "What single number moves if this works?" | -1 |
| kill | "What would make you kill it after launch?" | -1 |

The re-score is the model's call, but **must** be lower than the previous
iteration (narrowed_delta). If two iterations in a row produce the same score,
`dedup_metric: identical-ambiguity-cycle=2` fires → break out, mark "best
effort", and accept the current score.

### 2.4 — Convergence test

```
PASS  iff  evidence_count >= 3
        AND value_score >= 3.0
        AND ambiguity_score <= 3
```

On FAIL, loop on the failing dimension. Cap at `safety_valve=8` iterations.
On cap with no pass, write `"status": "held"` to `loop-log.json` and surface
the remaining gap to the user; do not auto-emit PRD.md.

### 2.5 — Decision-log entry

```markdown
# gate-2 cycle N
- evidence: N sources (need 3)
- LTV: $X × Y users = $Z / cost $W = value_score V.V
- ambiguity: 10 → 7 (asked: who is the first user)
- next: ask about X (next highest-leverage unknown)
```

## Gate 3/5 — non-goals

Ask once: "List 3 things this PRD will NOT do. For each, name a one-line
rationale and the breach-response (what we do if a reviewer asks us to add it)."

If the user gives <3, generate 3 candidates from `decision-log.md` context and
ask the user to confirm or replace. Write to `PRD.md` §3 (Non-goals).

## Gate 4/5 — decompose (phases JSON + step files)

Emit `phases/<name>/index.json` using the schema below. One step = one
shippable layer / module. Order: dependency-first (e.g. data model before API
before UI).

The top-level `worktree` field carries the branch base from which every per-step
worktree is derived (`<branch-base>-step<N>`). Emit it as `<prefix>-<phase>`,
where `<prefix>` follows the worktree-cut convention (typically `plan/<slug>`,
e.g. `plan/plugin-harness-v3`). The build runner reads it; if absent it falls
back to `feat/<phase>`, which is a defense-in-depth default, not the contract.

For each step in order:
1. Call `lib/execute.py:register_step(root, phase, step=N, name=<slug>)` —
   this creates the index.json entry with `status="unimplemented"`.
2. Write `phases/<name>/step<N>.md` using the **pinned template** below
   (`Status` / `Read first` / `Task` / `Acceptance Criteria` /
   `Verification & Status Update` / `Don't`). Plan only writes the
   `Status: pending` line; the runner and the executing sub-agent own
   the rest of the status lifecycle.
3. The runner transitions `unimplemented → pending` automatically when
   `step<N>.md` exists and the step number matches the index. If not,
   explicitly call `update_step_status(root, phase, step=N, status="pending")`.

### Phase JSON schema + step-file template + marker contract

**SSOT lives in `lib/execute.py`.** Do not redefine it here.

- Phase JSON schema → `lib/execute.py:register_step` + the `IndexSchema`
  dataclass. Plan only emits via `register_step(root, phase, step=N, name=...)`;
  runtime state transitions (`unimplemented → pending → in_progress →
  completed | error | blocked`) are owned by the runner.
- Step-file template (section order, headings, marker block) → read
  `lib/execute.py:_step_file_template()` and follow its output verbatim.
  Section order and marker placement are part of the plan ↔ build SSOT;
  the build runner's parser reads the marker block, the agent reads the
  sections.
- Marker contract → `lib/execute.py:parse_status_marker()` + the
  `VALID_STATUSES` / `SKIPPABLE_STATUSES` / `RESUMABLE_STATUSES`
  constants. Status values: `completed | error | blocked`. Companion
  fields: `summary | error_message | blocked_reason`. Marker value MUST
  match `index.json[step].status`; runner trusts `index.json` on
  disagreement.
- Korean / mixed-language content is forbidden — see `rules/skill-authoring.md`.

For a worked example, see `lib/execute.py:examples/plan_step_template.md`.

### team mode — dependency-aware step docs

When `DEV_KIT_MODE=team` (read via `os.environ.get("DEV_KIT_MODE")` or
`.claude/settings.json` `env.DEV_KIT_MODE`), Gate 4/5 emits extra
structure that the existing dispatcher / integrity layers already
consume:

1. **Per-step dependency prompt.** After the operator has chosen the
   step titles (the same multi-step picker that runs in `full` / `lite` /
   `undev`), run one `AskUserQuestion` per step with `multiSelect: true`
   over the upstream step numbers. The question text per step N is:
   "Which earlier steps must complete before step N starts?". Empty
   answer = leaf.
2. **Validate via `lib/plan_dependency.py:compute_dag`.** Build the
   step list and call `compute_dag(steps)`. If `result["valid"]` is
   `False`, refuse to emit: surface `result["missing"]` (unknown refs)
   or `result["cycles"]` (one cycle per offending SCC) and ask the
   operator to fix the edges before re-running. The validator runs
   BEFORE the index.json / step<N>.md writes so a broken graph never
   lands on disk.
3. **Write `dependencies:` block into `step<N>.md`.** For each step,
   add a top-level `dependencies: [N, ...]` field to the step file
   body. `lib/intent_integrity.py:_parse_step_file` already reads this
   field (coerces to int via `_to_int()`); the existing IC-3 gap check
   then enforces step.md ↔ index.json consistency for free.
4. **Skip the `dependencies:` block in non-team modes.** This section is
   gated on `DEV_KIT_MODE=team`. In `full` / `lite` / `undev`, Gate 4/5
   emits exactly as today (no extra prompt, no extra block).
5. **Build-runner fan-out.** `lib/dispatch_classifier.py:_has_dependency_edge`
   already reads `depends_on` / `consumes` from each step dict in
   `phases/<phase>/index.json`. Once the plan skill persists the
   per-step dependency list into the step dict (the same prompt that
   drives `step<N>.md`), the dispatcher classifies the phase as
   `sequential` for any non-trivial dependency edge — without any
   further wiring on the dispatch side.

The `## Dependencies` block in `step<N>.md` is the documented
extension to the step-file template (alongside `Status`, `Read first`,
`Task`, `Acceptance Criteria`, `Verification & Status Update`,
`Don't`). Keep the section order stable — `lib/intent_integrity.py`
parses these by exact key.

## Gate 5/5 — emit

Write `PRD.md` with the 6 sections below. DoD 5 conditions (all required):

1. §1 Frame includes the 3 fields from Gate 1 verbatim.
2. §2 Validate shows `value_score >= 3.0` and `ambiguity_score <= 3` (or
   `status: held` if cap was hit — in which case, stop and ask the user).
3. §3 Non-goals has ≥3 entries each with rationale + breach-response.
4. §4 Phase plan points at `phases/<name>/index.json` and lists every step
   title.
5. §5 AC list (1-5 items) maps 1:1 to step AC commands in `phases/<name>/step<N>.md`.
6. §6 Hand-off names `/dev-kit:build` as the next invocation.

Then:
- Append final cycle to `.dev-kit/loop-log.json` (MUST-16).
- Write `.dev-kit/hand-off/plan→build.md` summarizing the plan for the build
  stage.
- **Auto-render the design proposal** (see "Proposal auto-invoke" below) —
  the final cleanup step of Gate 5/5 is to materialize the proposal HTML
  the reviewer will see before `/dev-kit:build` is invoked.
- Pre-build integrity check is enforced by `/dev-kit:build`; the plan emit
  is complete when `phases/<name>/index.json` and `step<N>.md` are written.

### team mode — PRD §4 Dependency DAG subsection

When `DEV_KIT_MODE=team`, extend PRD §4 (Phase plan) with a "Dependency
DAG" subsection listing each edge from the per-step `dependencies:`
block collected in Gate 4/5:

```markdown
### Dependency DAG
- step2 → step1 (data model must exist before API layer)
- step3 → step1, step2 (config + API before UI)
- step4 → step3 (UI consumes config)
```

Edge order follows `compute_dag(steps)["topo"]` so the subsection reads
in execution order. Skip the subsection entirely when `dependencies` is
empty for every step (a fully-leaf graph). This is the DoD condition for
team mode; in `full` / `lite` / `undev`, omit the subsection.

The DoD item list above (1-6) does not gain a 7th bullet in team; the
subsection lives inside the existing §4 bullet, not as a separate DoD
check — the gate enforcement is on the per-step `dependencies:` block
existing in each `step<N>.md` (which `lib/intent_integrity.py` IC-3
already enforces at build time).

## Proposal auto-invoke (Gate 5/5 final step)

Gate 5/5 ends with a single, deterministic handoff to the
`/dev-kit:proposal` skill so the design record is auto-rendered at
`docs/proposals/<main>/<sub>.html`. The chain becomes
**plan → proposal → build**; the user no longer has to remember to run
`/dev-kit:proposal` manually.

### Slug derivation

The proposal topic is `<main>/<sub>`:

- `<main>` = the umbrella. For this project the umbrella is hardcoded
  to the design-domain name (a future PR can externalize it to a
  project-level config if a different umbrella is needed). The
  `harness-architecture` umbrella was removed in #463 along with its
  proposal bundle; the umbrella currently resolves to a per-PR topic.
- `<sub>` = the phase directory name (the `<name>` in `phases/<name>/`
  emitted in Gate 4/5). One source of truth — same name as the
  phase directory, same name as the proposal sub-topic, same name
  as the worktree branch base's `<phase>` segment.

If the phase name violates the proposal-slug regex
(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`),
the proposal skill will reject the render and Gate 5/5 surfaces the
slug error in the hand-off — the user is asked to rename the phase,
not the proposal.

### YAML emission

Write `docs/proposals/<main>/<sub>.yaml` with the canonical
shape (see `skills/proposal/SKILL.md` "Authoring a proposal"). Each
PRD § becomes one proposal `section` (title = PRD heading, body = PRD
section text, reduced to the markdown-lite subset the proposal
renderer understands). Frontmatter:

- `title`: PRD §1 goal (1-line, from Gate 1 `goal`).
- `status`: `design-discussion` (plan just finished, review not yet
  started).
- `issue`: the issue number from the plan context if known; omit
  otherwise.
- `date`: today's date in KST.
- `tags`: `[<phase-name>]` plus any tag the user supplied in Gate 1.

Do NOT edit or move existing proposal files; the auto-emit only
writes `<main>/<sub>.yaml` for the current phase's sub-topic.
If the sub-topic already exists with a different content, the
proposal skill must refuse to overwrite (it has no overwrite flag)
and Gate 5/5 surfaces the conflict for the user to resolve.

### Render invocation

Call the proposal skill with the topic slug:

```text
Skill("proposal", topic="<main>/<sub>")
```

The proposal skill reads `<main>/<sub>.yaml` and writes
`<main>/<sub>.html` via
`python3 -m lib.render_proposal_html <main>/<sub>`. The plan skill's
`disallowed-tools: Bash` deliberately does not block this — `Skill`
is its own tool and the proposal skill internally has Bash
permission.

### Hand-off chain

On success, the chain emitted in
`.dev-kit/hand-off/plan→build.md` becomes:

1. `plan` (this skill) — `PRD.md` + `phases/<name>/`
2. `proposal` (auto-invoked) — `docs/proposals/<main>/<sub>.html`
3. `build` (user-invoked next) — implementation

`§6 Hand-off` in PRD.md must list `/dev-kit:proposal <main>/<sub>` as
the "review artifact" link in addition to `/dev-kit:build` as the
next stage. This makes the proposal visible from the PRD itself, not
only from the skill chain.

## Rules (no exceptions)

- 5-field loop declared (MUST-15): `safety_valve=8`, composite convergence,
  `narrowed_delta`, `dedup_metric`, `user_interrupt`.
- **Interview consume gate (Phase 6)**: plan MUST read
  `.dev-kit/hand-off/<step>.md` frontmatter via `Read` before Gate 1.
  `status: held` → refuse to plan; `ok | best-effort | user-acknowledged`
  → proceed. The `--skip-interview` flag bypasses the gate for backward
  compat only and MUST be logged to `.dev-kit/decision-log.md`.
- No artifacts other than PRD.md, phases/<name>/, .dev-kit/decision-log.md, .dev-kit/hand-off/.
- No code, no `package.json`, no `Dockerfile`, no test code.
- "Just write the code" before PRD.md is complete → still no code.
- After HOLD, user re-invokes `/dev-kit:plan` to resume from
  `.dev-kit/decision-log.md`.
- `loop-log.json` appends narrowing per cycle (MUST-16).

## Hook alignment

Plan stage:
- `slop-detector=OFF` (planning docs tolerate LLM-typical phrasing)
- `stop-verify=ON`
- Others OFF

## Hand-off

On PRD.md complete:
- `state_codec.transition_stage(root, "build")`
- `state_codec.append_hand_off(root, "plan", "build", "...")` auto
- Write `.dev-kit/hand-off/plan→build.md`
- **Auto-invoke `/dev-kit:proposal <main>/<sub>`** (see "Proposal
  auto-invoke") to materialize the design record at
  `docs/proposals/<main>/<sub>.html`.
- Wait for `/dev-kit:build` invocation

## Next step

`/dev-kit:build` — converts `phases/<name>/step<N>.md` into per-step
implementation via harness-runner. The design record is at
`docs/proposals/<main>/<sub>.html` (auto-rendered by Gate 5/5's
proposal step), so reviewers can read the proposal before the build
starts.
