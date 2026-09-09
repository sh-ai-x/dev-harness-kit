---
name: skill-usage
category: shortcuts
description: Run the skill usage telemetry CLI and inspect turns, invocations, and per-project usage.
when_to_use: |
  - User types /dev-kit:skill-usage
  - User wants to see per-skill invocation counts
  - User is planning a /dev-kit:prune pass and wants usage data first
argument-hint: ""
allowed-tools: Read Bash
disallowed-tools: Edit WebFetch Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
alpha: analysis
---
> [← Skills index](../../README.md)

# /dev-kit:skill-usage — usage telemetry CLI

## What it does

Run the usage report from the repository root:

```bash
python3 tools/skill_usage.py $ARGUMENTS
```

Useful examples:

```text
/dev-kit:skill-usage
/dev-kit:skill-usage --top 0
/dev-kit:skill-usage --days 0 --json --per-cwd
/dev-kit:skill-usage --cwd /path/to/project
```

The CLI reports `turns` and `invocations` separately. Use
`python3 tools/skill_usage.py --help` for the complete option list.
