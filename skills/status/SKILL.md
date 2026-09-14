---
name: status
category: status
description: HOTL visualization. Current loop progress + cumulative cycles + hand-off chain + eval score on one screen.
alpha: state
when_to_use: |
  - User types @dev-kit status
  - User asks for the current loop, stage, drift, or hand-off summary
allowed-tools: Read Grep Skill
disallowed-tools: Bash Edit Write
model: haiku
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# @dev-kit:status — HOTL visualization

Read-only. Current stage + cumulative cycles + drift score + hand-off pointer.

No push notifications ❌. Only on user invocation.

## Output shape

Emit exactly one `## status` block, then one line per row below. Do not add commentary or follow-up sentences after the block. If `hand-off:` points at a skill, invoke it via the Skill tool; that invocation is the next step, not prose in the output.

```text
stage:       <stage-name>     # plan | build | eval | ship | idle
cycles:      <int>            # cumulative loop count
drift:       <ok|warn|alert>  # latest drift verdict
hand-off:    <pointer-or-/>   # next skill to invoke, or "/" if none
eval-score:  <float-or-/>     # latest eval-score, or "/" if not run
```

If `.dev-kit/` is absent or contains no state files, emit a single `no state found` line and stop. Do not invent stage / cycle numbers when there is no state to read.

## Next step

If `hand-off:` points at a skill (not `/`), invoke it via the Skill tool. Otherwise the loop is idle — wait for the next user prompt.
