# Commands index

Slash-command wrappers in this directory. Each `commands/<name>.md`
is the source of truth; `bin/install-commands.sh` (and the SessionStart
hook) copies them into `.claude/commands/` and `.codex/commands/` so
Claude Code and Codex can resolve `/dev-kit:<name>`.

The body of every wrapper forwards to `skills/<name>/SKILL.md` (the
implementation). If you are adding a new slash command, mirror
`commands/mode.md` — the smallest legal wrapper is ~20 lines.

## All commands

| Slash | Source wrapper | Skill impl | What it does |
|---|---|---|---|
| [`/dev-kit:babysit-pr-local`](babysit-pr-local.md) | `commands/babysit-pr-local.md` | `skills/babysit-pr-local/SKILL.md` | Local-mode PR babysitter (review + security + maintenance verdicts via `bin/review-local.sh`). |
| [`/dev-kit:ci-update`](ci-update.md) | `commands/ci-update.md` | `skills/ci-update/SKILL.md` | Detect + selectively apply drift between installed CI templates and current dev-kit source. |
| [`/dev-kit:harness-effectiveness`](harness-effectiveness.md) | `commands/harness-effectiveness.md` | `skills/harness-effectiveness/SKILL.md` | Print the 5-component scorecard (prevention / first-pass / recovery / learning / measurement-integrity) without running the full eval. |
| [`/dev-kit:maintenance`](maintenance.md) | `commands/maintenance.md` | `skills/maintenance/SKILL.md` | Code-sanity gate (CC-1..8 / OE-1..8 / VM-1..4). Mirrors `.github/workflows/maintenance.yml`. |
| [`/dev-kit:mode`](mode.md) | `commands/mode.md` | `skills/mode/SKILL.md` | Pick / show the active `DEV_KIT_MODE` (`full` / `lite` / `undev`). |
| [`/dev-kit:pr-verify`](pr-verify.md) | `commands/pr-verify.md` | `skills/pr-verify/SKILL.md` | Deterministic PR verification — fresh `gh pr view` + check + comment fetches on every call. |
| [`/dev-kit:ralph`](ralph.md) | `commands/ralph.md` | `skills/ralph/SKILL.md` | End-to-end autonomous loop with 4 user gates + unattended build/babysit/ship (`attended_lock` forbids AskUserQuestion during `ATTENDED_RUN`). |
| [`/dev-kit:review-local`](review-local.md) | `commands/review-local.md` | `skills/review-local/SKILL.md` | Local equivalent of the GH-Actions review workflow (`bin/review-local.sh`). |
| [`/dev-kit:skill-usage`](skill-usage.md) | `commands/skill-usage.md` | `skills/skill-usage/SKILL.md` | Skill usage telemetry CLI (turns, invocations, per-project usage). |
| [`/dev-kit:worktree-prune`](worktree-prune.md) | `commands/worktree-prune.md` | `skills/worktree-prune/SKILL.md` | Count worktrees, list them oldest-first, remove the N oldest on demand. |

## When the index drifts from `bin/install-commands.sh`

The install script globs `commands/*.md`; any file added here is
auto-discovered. To rename a command: rename the `commands/<name>.md`
file + update the matching `skills/<name>/SKILL.md` directory. To
retire a command: delete the wrapper file (and confirm `bin/install-commands.sh --verify` no longer expects it).