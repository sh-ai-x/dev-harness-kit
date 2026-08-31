---
name: harness-mode
category: config
description: Session-scoped local-hook mode picker — fast (all optional local hooks off), full (default, all on), or custom (interactive per-local-hook picker via AskUserQuestion).
alpha: state
when_to_use: |
  - User types /dev-kit:harness-mode fast|full|custom|show
  - User wants a quick local iteration loop without the full local-hook stack
  - User wants to opt individual optional local hooks (TDD judge, slop-detector, pre-commit review, maintenance, security depth, babysit-pr mode) on or off for this session only
allowed-tools: Read Bash AskUserQuestion
model: opus
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:harness-mode — session-scoped local-hook mode

## What it does

Writes `.dev-kit/harness-mode.session.json` via `lib/harness_mode_state.py`,
which every gated skill and local hook (`lib/tdd_scope_judge.py`,
`hooks/slop-detector.sh`, `lib/execute.py`'s intent-integrity gate) reads on
each invocation. Four **correctness local hooks** — `stop_verify`,
`secret_scan`, `intent_integrity` (high), `gh_ci_required` — are hardcoded
in `lib/harness_mode_state.resolved_gate()` to always resolve `"on"`; no
mode, and no hand-edited state file, can turn them off.

**Session-scoped, not project-level.** A SessionStart hook
(`hooks/session-start-harness-mode-reset.sh`) resets the state file to
`{"mode": "full"}` at the start of every session. A new window always starts
strict — `fast` or `custom` must be chosen explicitly every session.

**This skill controls *local hooks only*.** The GH-Actions workflow gates
(review / security / maintenance / severity gate / lint / test / validate /
merge-queue-ready-check / ...) are out of scope — see
[CI workflow gates (not affected by harness-mode)](#ci-workflow-gates-not-affected-by-harness-mode)
below.

## Sub-commands

```bash
/dev-kit:harness-mode fast     # instant: every optional local hook OFF, correctness local hooks stay ON
/dev-kit:harness-mode full     # instant: everything ON (this is also the SessionStart default)
/dev-kit:harness-mode custom   # interactive: pick each optional local hook individually
/dev-kit:harness-mode show     # print the resolved local-hook table for this session, no prompts
```

### `fast` / `full`

Run directly, no confirmation needed:

```bash
python3 -m lib.harness_mode_state write fast
python3 -m lib.harness_mode_state write full
```

Then print the resolved table (`python3 -m lib.harness_mode_state show`) so the
user sees exactly what changed.

### `custom`

Issue exactly two `AskUserQuestion` calls, batched 3 questions each so no
single prompt exceeds a comfortable choice count. **Call 1 must complete before
Call 2 is issued** — do not batch all 6 into one call (the tool caps at 4
questions per call, and 3-per-screen stays legible).

Each row's `Question` cell now explicitly names the local hook (so the user
knows they are toggling a *local* switch, not a CI workflow gate) and the
`Options` cell names the trade-off (e.g. "skipping defers to CI" for the
gates CI re-checks anyway).

**Call 1:**

| Question | Options |
|---|---|
| Run the **TDD scope judge** local hook before each build step? | Keep it ON (Recommended) / Skip locally (CI `test` re-runs) |
| Run the **slop-detector** local hook on each write? | Keep it ON (Recommended) / Skip locally (CI l4-todo-scan re-runs) |
| Run the **pre-commit codex:review** local hook before each commit? | Keep it ON (Recommended) / Skip locally (no CI equivalent) |

**Call 2:**

| Question | Options |
|---|---|
| Run the **maintenance** local hook (CC/OE/VM) before each commit? | Keep it ON (Recommended) / Skip locally (CI `/dev-kit:maintenance` always re-runs) |
| **security_owasp** local hook depth this session? | Full 10-dim OWASP fan-out (Recommended) / Quick single-dim summary / Skip locally |
| **babysit_pr** local hook behavior? | Full auto-fix polling loop (Recommended) / Manual: one `gh pr checks` dump + exit |

Map each answer to a gate key and write them all in one call:

```bash
python3 -m lib.harness_mode_state write custom --gates '{
  "tdd_scope_judge": "off",
  "slop_detector": "on",
  "pre_commit_review": "off",
  "maintenance": "on",
  "security_owasp": "quick",
  "babysit_pr": "manual"
}'
```

Correctness gates must never appear as `AskUserQuestion` options and must
never be written into `--gates` — `lib/harness_mode_state.write_state()`
silently drops any correctness-gate key passed to it as defense in depth, but
the picker itself should not offer them at all.

**Non-interactive fallback**: if `AskUserQuestion` is unavailable (e.g. a
headless/CI invocation), print the same 6 questions as a markdown table and
tell the caller to run `fast` or `full` instead of hanging on interactive
input.

### `show`

```bash
python3 -m lib.harness_mode_state show
```

Prints the current `mode` plus the resolved value of every local hook grouped
by category (correctness / quality / style / process), with a one-line
`description` per hook so readers know what each one does. Example:

```json
{
  "mode": "full",
  "local_hooks": {
    "correctness": {
      "stop_verify":     {"value": "on",   "type": "local_hook", "description": "L3 evidence: refuse 'done' without quoted exit code + test count + log"},
      "secret_scan":     {"value": "on",   "type": "local_hook", "description": "credential-leak detection on every Write/Edit"},
      "intent_integrity":{"value": "on",   "type": "local_hook", "description": "plan-vs-execution drift detection"},
      "gh_ci_required":  {"value": "on",   "type": "local_hook", "description": "refuse edits that would break GH-Actions"}
    },
    "quality": {
      "tdd_scope_judge": {"value": "on",   "type": "local_hook", "description": "TDD scope judge before each build step"},
      "security_owasp":  {"value": "full", "type": "local_hook", "description": "local security scan depth (full 10-dim / quick / off)"}
    },
    "style": {
      "slop_detector":   {"value": "on",   "type": "local_hook", "description": "TODO/stub/placeholder markers on every Write/Edit"},
      "maintenance":     {"value": "on",   "type": "local_hook", "description": "code-sanity (CC/OE/VM) gate, locally"}
    },
    "process": {
      "pre_commit_review":{"value": "on",  "type": "local_hook", "description": "codex:review before each commit"},
      "babysit_pr":      {"value": "full", "type": "local_hook", "description": "/dev-kit:babysit-pr auto-fix loop (full / manual)"}
    }
  },
  "gates": {...},  /* legacy flat alias; new code should read .local_hooks.<category>.<gate>.value */
  "ci_gates_notice": "CI workflow gates (.github/workflows/*.yml) are NOT toggled by harness-mode..."
}
```

Use `--json` for a single-line compact form (script-friendly for `jq`):

```bash
python3 -m lib.harness_mode_state show --json | jq '.local_hooks.style.slop_detector.description'
```

## What each mode flips

| Classification | Local hook | `full` | `fast` | `custom` |
|---|---|---|---|---|
| Correctness | `stop_verify` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `secret_scan` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `intent_integrity` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `gh_ci_required` | on | **on (always)** | **on (always, not offered)** |
| Quality | `tdd_scope_judge` | on | off | picker |
| Quality | `security_owasp` | full | quick | picker (full/quick/off) |
| Style | `slop_detector` | on | off | picker |
| Style | `maintenance` | on | off | picker |
| Process | `pre_commit_review` | on | off | picker |
| Process | `babysit_pr` | full | manual | picker (full/manual) |

Correctness local hooks are the ones whose failure CI cannot recover from
(sub-agent declaring success on no-diff writes, a leaked credential, a
high-severity intent-integrity finding, or the merge gate itself). Everything
else is a local-only convenience CI re-checks on push — see
`docs/proposals/workflow-fast-mode-lean/main.yaml` §1 for the full rationale.

## CI workflow gates (not affected by harness-mode)

The 9 GH-Actions workflows in `.github/workflows/*.yml` are NOT controlled by
harness-mode. They run on every push regardless of the local mode and are the
only thing blocking merge via branch protection. Examples:

| Workflow file | Role |
|---|---|
| `.github/workflows/review.yml` | `/dev-kit:review` + `/dev-kit:security` LLM judges, severity gate, auto-approve |
| `.github/workflows/maintenance.yml` | `/dev-kit:maintenance` LLM judge (CC/OE/VM) |
| `.github/workflows/ci.yml` | `branch-policy`, `lint`, `test`, `validate` |
| `.github/workflows/merge-queue-ready-check.yml` | re-runs `lint` / `validate` / `scope` on `merge_group` |
| `.github/workflows/auto-fix-pr.yml` | repair adapter on `changes_requested` reviews |
| `.github/workflows/fork-pr-review.yml` | maintainer-approval gate for fork PRs |
| `.github/workflows/version-bump.yml` | PATCH++ on `.claude-plugin/plugin.json` for merge queue |
| `.github/workflows/cost-flag.yml` | aggregates Cost-gate trailers + applies `cost-flag` label |
| `.github/workflows/linear-pr-sync.yml` | non-blocking Linear sync |

Why the separation:

- **Local hooks can catch a defect before the developer pushes** (cheaper,
  faster feedback).
- **CI workflow gates are authoritative**: they re-check on every push and
  are the only thing blocking merge (via branch protection).
- Some local hooks have **no CI counterpart** (`stop_verify`, `secret_scan`,
  `intent_integrity`, `gh_ci_required` — these catch things CI cannot, e.g.
  a sub-agent declaring "done" without evidence).
- Some CI workflow gates have **no local counterpart** (`severity gate`,
  `merge-queue-ready-check`, `version-bump`, `linear-pr-sync` — these
  require GH-Actions infrastructure or merge-queue semantics).
- Toggling a local hook `off` in `fast` / `custom` mode does **not** weaken
  CI: every CI gate above still runs on every push.

The `ci_gates_notice` field in `show` output is the terse reminder of this
separation. The full per-job inventory (trigger events + `needs:` chains +
skip matrices) lives in the workflow files themselves — `ls
.github/workflows/*.yml` is the entry point.

## Related

- `lib/harness_mode_state.py` — the state module (`read_state`, `write_state`, `resolved_gate`, CLI, `_build_show_output`, `GATE_CATEGORIES`).
- `hooks/session-start-harness-mode-reset.sh` — the SessionStart reset hook.
- `hooks/slop-detector.sh`, `lib/tdd_scope_judge.py` — current local-hook consumers.
- `.github/workflows/review.yml`, `maintenance.yml`, `ci.yml` — the CI workflow gates this skill does NOT control.
- `skills/build/SKILL.md` — documents the harness-mode-aware sub-agent preamble.
- `skills/babysit-pr/SKILL.md` — documents `babysit_pr=manual` behavior.

## Next step

Run `/dev-kit:build` (or any gated skill) — it reads the session state on
every invocation, no extra flag needed.
