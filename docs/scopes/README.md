# Scope Reference

Three scopes × three modes = nine cells. Pick the one that matches your situation.

| You are...                       | Mode  | File you edit                          |
|----------------------------------|-------|----------------------------------------|
| Setting up a new dev project      | full  | `<proj>/.claude/settings.json`         |
| Setting up a new MVP sprint       | lite  | `<proj>/.claude/settings.json`         |
| Personalizing Claude Code         | n/a   | `~/.claude/settings.json`              |
| Personal override in a repo      | n/a   | `<proj>/.claude/settings.local.json`   |
| Random non-dev repo               | undev | nothing (silent default)               |

See [`decision-tree.md`](decision-tree.md) for the full mapping.

## Why this directory exists

dev-harness-kit has plugins, hooks, iron laws, and templates — but until now, nothing dedicated to **which settings file you should edit** or **which mode your project is in**. This directory is the single source of truth for those two questions.

**Single GitHub address:**

```
https://github.com/sh-ai-x/dev-harness-kit/tree/main/docs/scopes/
```

If you remember nothing else, remember that URL.

## What's in here

| File | Purpose |
|---|---|
| [`README.md`](README.md) | This file — landing page + matrix |
| [`decision-tree.md`](decision-tree.md) | Operator-facing flowchart for "which scope do I edit?" |
| [`user-scope.md`](user-scope.md) | Reference for `~/.claude/settings.json` |
| [`project-scope.md`](project-scope.md) | Reference for `<project>/.claude/settings.json` |
| [`local-scope.md`](local-scope.md) | Reference for `<project>/.claude/settings.local.json` |
| [`modes.md`](modes.md) | Reference for `full` / `lite` / `undev` mode selection |
| [`troubleshooting.md`](troubleshooting.md) | FAQ for "why is X firing in Y?" leakage questions |
| [`templates/`](templates/) | Six canonical JSON / dotfile templates |

## Quick start

1. **Decide which scope** you need to edit → read `decision-tree.md`.
2. **Read the per-scope doc** for that file's purpose and anti-patterns.
3. **Copy the matching template** from `templates/` and customize.
4. **Verify** with the audit commands in `troubleshooting.md`.
