---
name: harness-mode
category: config
description: Session-scoped local-gate mode picker — fast (all optional gates off), full (default, all on), or custom (interactive per-gate picker via AskUserQuestion).
alpha: state
when_to_use: |
  - User types /dev-kit:harness-mode fast|full|custom|show
  - User wants a quick local iteration loop without the full gate stack
  - User wants to opt individual optional gates (TDD judge, slop-detector, pre-commit review, maintenance, security depth, babysit-pr mode) on or off for this session only
allowed-tools: Read Bash AskUserQuestion
model: opus
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:harness-mode — session-scoped local gate mode

## What it does

Writes `.dev-kit/harness-mode.session.json` via `lib.harness_mode_state`, which
every gated skill and hook (`lib/tdd_scope_judge.py`, `hooks/slop-detector.sh`,
`lib/execute.py`'s intent-integrity gate) reads on each invocation. Four
correctness gates — `stop_verify`, `secret_scan`, `intent_integrity` (high),
`gh_ci_required` — are hardcoded in `lib.harness_mode_state.resolved_gate()` to
always resolve `"on"`; no mode, and no hand-edited state file, can turn them off.

**Session-scoped, not project-level.** A SessionStart hook
(`hooks/session-start-harness-mode-reset.sh`) resets the state file to
`{"mode": "full"}` at the start of every session. A new window always starts
strict — `fast` or `custom` must be chosen explicitly every session.

## Sub-commands

```bash
/dev-kit:harness-mode fast     # instant: every optional gate OFF, correctness gates stay ON
/dev-kit:harness-mode full     # instant: everything ON (this is also the SessionStart default)
/dev-kit:harness-mode custom   # interactive: pick each optional gate individually
/dev-kit:harness-mode show     # print the resolved gate table for this session, no prompts
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

**Call 1:**

| Question | Options |
|---|---|
| Run the TDD scope judge locally before each build step? | Keep it ON (Recommended) / Skip locally (defer to CI) |
| Run slop-detector locally on each write? | Keep it ON (Recommended) / Skip locally |
| Run pre-commit `codex:review` before each commit? | Keep it ON (Recommended) / Skip locally |

**Call 2:**

| Question | Options |
|---|---|
| Run the maintenance (CC/OE/VM) gate locally? | Keep it ON (Recommended) / Skip locally |
| Local security scan depth this session? | Full 10-dim OWASP fan-out (Recommended) / Quick single-dim summary / Skip locally |
| `/dev-kit:babysit-pr` behavior this session? | Full auto-fix polling loop (Recommended) / Manual: one `gh pr checks` dump + exit |

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
never be written into `--gates` — `lib.harness_mode_state.write_state()`
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

Prints the current `mode` plus the resolved value of every gate (correctness
gates always `"on"`).

## What each mode flips

| Classification | Gate | `full` | `fast` | `custom` |
|---|---|---|---|---|
| Correctness | `stop_verify` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `secret_scan` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `intent_integrity` | on | **on (always)** | **on (always, not offered)** |
| Correctness | `gh_ci_required` | on | **on (always)** | **on (always, not offered)** |
| Quality | `tdd_scope_judge` | on | off | picker |
| Style | `slop_detector` | on | off | picker |
| Process | `pre_commit_review` | on | off | picker |
| Style | `maintenance` | on | off | picker |
| Quality | `security_owasp` | full | quick | picker (full/quick/off) |
| Process | `babysit_pr` | full | manual | picker (full/manual) |

Correctness gates are the ones whose failure CI cannot recover from (sub-agent
declaring success on no-diff writes, a leaked credential, a high-severity
intent-integrity finding, or the merge gate itself). Everything else is a
local-only convenience CI re-checks on push — see
`docs/proposals/workflow-fast-mode-lean/main.yaml` §1 for the full rationale.

## Related

- `lib/harness_mode_state.py` — the state module (`read_state`, `write_state`, `resolved_gate`, CLI).
- `hooks/session-start-harness-mode-reset.sh` — the SessionStart reset hook.
- `hooks/slop-detector.sh`, `lib/tdd_scope_judge.py` — current gate consumers.
- `skills/build/SKILL.md` — documents the harness-mode-aware sub-agent preamble.
- `skills/babysit-pr/SKILL.md` — documents `babysit_pr=manual` behavior.

## Next step

Run `/dev-kit:build` (or any gated skill) — it reads the session state on
every invocation, no extra flag needed.
