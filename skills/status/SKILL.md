---
name: status
category: status
description: HOTL visualization. Current loop progress + cumulative cycles + hand-off chain + eval score on one screen.
alpha: state
when_to_use: |
  - User types @dev-kit status
allowed-tools: Read Grep
disallowed-tools: Bash Edit Write
model: haiku
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# @dev-kit:status — HOTL visualization

Read-only. Current stage + cumulative cycles + drift score + hand-off pointer.

No push notifications ❌. Only on user invocation.

## Output shape

Emit exactly one `## status` block, then one line per row below. Stop after the block — do not add commentary, follow-ups, or "next step" prose.

```text
stage:       <stage-name>     # plan | build | eval | ship | idle
cycles:      <int>            # cumulative loop count
drift:       <ok|warn|alert>  # latest drift verdict
hand-off:    <pointer-or-/>   # next skill to invoke, or "/" if none
eval-score:  <float-or-/>     # latest eval-score, or "/" if not run
```

If `.dev-kit/` is absent or contains no state files, emit a single `no state found` line and stop. Do not invent stage / cycle numbers when there is no state to read.

## Next step

If `hand-off:` points at a skill (not `/`), invoke it. Otherwise the loop is idle — wait for the next user prompt.
