---
name: ship
category: ship
description: 0-arg. Release tag emit. Gate check only (hooks auto). Requires Review verdict=Approve + main-block pass.
alpha: state
when_to_use: |
  - User types /dev-kit:ship
  - Release cutoff
allowed-tools: Read Bash
disallowed-tools: Write Edit WebFetch
model: haiku
disable-model-invocation: false
---
> [← Skills index](../../README.md)

# /dev-kit:ship — Release Gate

## Behavior

1. Verify pre-push main-block pass (gh-autoswitch).
2. Check Review verdict=Approve (separate security scan pass also OK).
3. CHANGELOG entry auto.
4. git tag + push.

## Iron Law

- No direct push to main ❌. PR only.
- No --no-verify abuse ❌.
- No auto-merge (after user review).

## Hook integration

`stop-verify=ON`. main-block hook validation.
