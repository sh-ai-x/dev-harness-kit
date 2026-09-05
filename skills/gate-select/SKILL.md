---
name: gate-select
category: config
description: Unified 3-dimension picker for project / session / AI-judge gates. Shows what's installed and dispatches to the right installer (ci-setup / harness-mode / AI-judge skills).
alpha: state
when_to_use:
  - User types /dev-kit:gate-select after /dev-kit:bootstrap
  - User wants one-pick visibility into which CI workflows, local hooks, and AI-judge skills are active
  - User wants to add CI gates without running /dev-kit:ci-setup directly
  - User wants to switch session local-hook mode without remembering /dev-kit:harness-mode sub-commands
allowed-tools: Read Write Bash AskUserQuestion
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:gate-select — Unified 3-dimension gate picker

## What it does

One skill that exposes the three gate dimensions the rest of dev-kit fragments across separate pickers:

| Dimension | SSOT file | Picker today | What gate-select does |
|---|---|---|---|
| **Project** (CI workflow gates) | `.dev-kit/ci-config.json` | `/dev-kit:ci-setup` | Reports which of `ci.yml` / `auto-fix-pr.yml` / `review.yml` are installed; dispatches to `ci-setup` to install missing ones. |
| **Session** (local-hook gates) | `.dev-kit/harness-mode.session.json` | `/dev-kit:harness-mode` | Reports mode + per-gate values; dispatches to `harness-mode` to change. |
| **AI-judge** (LLM-judge skills) | (skill-shipped; CI wiring in `.github/workflows/review.yml`) | `/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance` | Reports which judge skills are enabled and whether they are wired into CI; dispatches to the right skill. |

**No new state file** — gate-select reads the three existing sources above. `pick` dispatches writes to the same writers `ci-setup` / `harness-mode` / judge skills already use.

## Sub-commands

| Sub-command | Effect |
|---|---|
| `show` | Print current state of all 3 dimensions, no edits |
| `pick` (default) | Interactive 3-question multiSelect + apply |
| `install-project` | Dispatch to `/dev-kit:ci-setup` (idempotent marker-driven install) |
| `install-session <fast\|full\|custom>` | Dispatch to `/dev-kit:harness-mode` |

```bash
/dev-kit:gate-select show            # current state, no edits
/dev-kit:gate-select                # = pick
/dev-kit:gate-select pick           # explicit pick
/dev-kit:gate-select install-project
/dev-kit:gate-select install-session full
```

## `show` output

```
PROJECT GATES (CI workflows, .dev-kit/ci-config.json marker)
  ci.yml (branch-policy + test + validate):   installed | /dev-kit:ci-setup
  auto-fix-pr.yml:                            installed | /dev-kit:ci-setup
  review.yml (review + security LLM judge):   installed | /dev-kit:ci-setup
  maintenance.yml (maintenance LLM judge):    NOT INSTALLED — template not shipped yet

SESSION GATES (.dev-kit/harness-mode.session.json, reset every SessionStart)
  mode: full
  optional (picker-toggled):
    tdd_scope_judge, slop_detector, pre_commit_review,
    maintenance, security_owasp, babysit_pr
  correctness (always on): stop_verify, secret_scan, intent_integrity, gh_ci_required

AI-JUDGE GATES (skills shipped with the plugin; wired into CI by ci-setup)
  /dev-kit:review        enabled (CI: review.yml, local: bin/review-local.sh)
  /dev-kit:security      enabled (CI: review.yml, local: bin/review-local.sh)
  /dev-kit:maintenance   enabled (CI: not shipped, local: /dev-kit:maintenance or bin/review-local.sh)
```

### How `show` reads each dimension

```bash
# Project — marker presence + per-workflow file presence
[ -f .dev-kit/ci-config.json ] && echo "ci-config marker: present"
for f in ci.yml auto-fix-pr.yml review.yml; do
  [ -f .github/workflows/$f ] && echo "$f: installed" || echo "$f: missing"
done

# Session — delegated to the existing CLI
python3 -m lib.harness_mode_state show

# AI-judge — static; the 3 judge skills are always loaded with the plugin
echo "/dev-kit:review: enabled"; echo "/dev-kit:security: enabled"; echo "/dev-kit:maintenance: enabled"
```

## `pick` flow

Two `AskUserQuestion` calls, 3 questions each (mirrors `harness-mode` lines 61–110).

### Call 1

| Question | Options |
|---|---|
| Install **project gates** (CI workflow + pre-push + scripts + `.dev-kit/ci-config.json`)? | Install via `/dev-kit:ci-setup` (Recommended) / Skip — bootstrap stays minimal |
| Set **session gates** mode? | `full` (Recommended) / `fast` / `custom` (delegate to `/dev-kit:harness-mode custom`) |
| Wire **AI-judge gates** into CI? | `review + security` via review.yml (Recommended, ships today) / `review + security + maintenance` (blocked until template lands) / Skip — leave AI-judge skill-only |

### Call 2 (only if project pick = Install)

| Question | Options |
|---|---|
| Run **Phase 3 verify** after ci-setup install? | Run verify (Recommended) / `--skip-verify` (faster, no `bash -n` / `validate.py` / `ci-local.sh`) |
| Persist **session mode** choice across sessions? | Write `.dev-kit/harness-mode.session.json` (Recommended, SessionStart will reset to `full` next session) / One-shot — leave as-is |
| Print **post-install checklist** after ci-setup? | Print 5-step checklist (Recommended) / Skip checklist |

### Dispatch script (read each AskUserQuestion answer, then run)

The script bodies below are documentation of what `pick` does at runtime.
`lib.ci_setup` is a Python module (no argparse CLI), so the project pick
dispatches by `Skill` invocation rather than a shell call. `harness-mode`
*does* expose a real CLI (`lib.harness_mode_state write <mode>`), so it
can be invoked directly.

```bash
# Project pick — delegate to the existing installer via Skill tool.
# (lib/ci_setup.py is a Python module, not a CLI; do NOT shell out.)
if [ "$project_pick" = "Install" ]; then
  /dev-kit:ci-setup ${force:+--force} ${skip_verify:+--skip-verify}
fi

# Session pick — harness-mode is a real CLI; invoke directly.
case "$session_pick" in
  full|fast) python3 -m lib.harness_mode_state write "$session_pick" ;;
  custom)    /dev-kit:harness-mode custom ;;
esac

# AI-judge pick — no installer yet; just report the resulting state.
case "$ai_judge_pick" in
  "review + security") echo "✓ Wired via .github/workflows/review.yml (installed by ci-setup)" ;;
  "review + security + maintenance") echo "✗ maintenance.yml template not shipped — out of scope here" ;;
  "Skip") echo "✓ AI-judge skills remain skill-only (no CI wiring)" ;;
esac
```

## `install-project` and `install-session` sub-commands

Convenience aliases that wrap a single existing installer. They are for the case where the operator wants to chain one fragment of the picker without going through the full 6-question `pick`:

```bash
# Same as /dev-kit:ci-setup (idempotent marker-driven install)
/dev-kit:gate-select install-project

# Same as /dev-kit:harness-mode full
/dev-kit:gate-select install-session full
```

These are not new installers — they exist so gate-select can serve as a single discovery + dispatch surface for all three gate dimensions.

## What is out of scope

| Surface | Today | Why |
|---|---|---|
| `templates/ci/.github/workflows/maintenance.yml` | Not shipped | `gate-select`'s "review + security + maintenance" pick stays blocked until a separate PR adds the template + tests. |
| `lib/config_state.py` / `.dev-kit/.enabled.json` | Not used | Referenced only by `skills/config/SKILL.md` + `hooks/linear-*.sh`; gate-select reads from `ci-config.json` + `harness-mode.session.json`. |
| Skill-disable mechanism for AI-judge skills | None exists | `/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance` are always-on with the plugin; gate-select only orchestrates their **CI wiring**, not their skill-level enablement. |

## Rules (no exceptions)

- **0-arg UX (MUST-21)**: zero args. Branching via `when_to_use` auto-match + sub-commands.
- **No new state file**: `gate-select` reads the three existing sources. `pick` dispatches writes to the same writers `ci-setup` / `harness-mode` / judge skills already use.
- **HOTL (MUST-29)**: every edit (sub-command with side effects) asks before writing. `show` is read-only.
- **No option prompts on `show`** (MUST-NOT-13): `show` prints state and exits.

## Next step

After installing gates, run `/dev-kit:build <first-feature>` to start the canonical plan → build loop. `/dev-kit:ci-doctor` is also available for post-install drift verification.
