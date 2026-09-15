---
name: gate-select
category: config
description: Unified 3-dimension picker for project / session / AI-judge gates. Reads .dev-kit/gates.json + .dev-kit/ci-config.json + .dev-kit/harness-mode.session.json and dispatches writes through `python -m lib.gates_state ...`.
alpha: state
when_to_use:
  - User types /dev-kit:gate-select after /dev-kit:bootstrap
  - User wants one-pick visibility into which CI workflows, local hooks, and AI-judge skills are active
  - User wants to toggle a CI gate (review/security/maintenance) WITHOUT editing review.yml
  - User wants to switch session local-hook mode without remembering /dev-kit:harness-mode sub-commands
allowed-tools: Read Write Bash AskUserQuestion
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

## What it does

Single pane of glass + dispatch surface for three gate dimensions the rest of
dev-kit fragments across separate pickers. The project dimension is now
JSON-driven (issue TBD): `.dev-kit/gates.json` is the SSOT for which judge
workflows are on, and `gate-select` is its canonical writer. Workflows
read `vars.GATES_<NAME>_ENABLED` at runtime — toggling a gate is a JSON
edit + `gate-select sync`, not a YAML edit + isolation-hook bypass.

| Dimension | SSOT file | Picker today | What gate-select does |
|---|---|---|---|
| **Project** (CI workflow gates) | `.dev-kit/gates.json` (NEW) + `.dev-kit/ci-config.json` | `/dev-kit:ci-setup` (install) | Reads gates.json + marker; `enable/disable/sync/init` write gates.json; dispatches ci-setup to install based on it. |
| **Session** (local-hook gates) | `.dev-kit/harness-mode.session.json` | `/dev-kit:harness-mode` | Reads mode + per-gate values; dispatches to `harness-mode` to change. |
| **AI-judge** (LLM-judge skills) | (skill-shipped; CI wiring in `.github/workflows/{review,security,maintenance}.yml`) | `/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance` | Reports which judge skills are enabled and whether their CI workflow is wired; dispatches to the right skill. |

## Sub-commands

| Sub-command | Effect |
|---|---|
| `show` (default) | Read gates.json + marker; print the 3 dimensions. No edits. |
| `classify` | Run `python -m lib.actor_classifier` for the current HEAD; print the actor type + recommended gate. Read-only. |
| `route [--json]` | Run `classify` + write `.dev-kit/.pr-route.json` (the breadcrumb `hooks/pr-create-route.sh` consumes on every `gh pr create`). |
| `pick` | Legacy 6-question picker; threads writes to gates.json + ci-setup. Kept as compat for the original AI-judge picker UX. |
| `enable <gate>` | `python -m lib.gates_state enable <gate>` — flips `gates.<gate>.enabled` to true. |
| `disable <gate>` | `python -m lib.gates_state disable <gate>` — flips `gates.<gate>.enabled` to false. |
| `set <gate> <key> <value>` | Generic field writer (for `enabled`, `workflow`, `var`). |
| `sync` | Push enabled flags to `gh variable set GATES_<NAME>_ENABLED`. |
| `init` | Synthesize `.dev-kit/gates.json` from the current `marker.runners` so a consumer that previously used `--exclude security.yml` upgrades in one step. |
| `install-project` | Dispatch to `/dev-kit:ci-setup` (idempotent marker-driven install). |
| `install-session <fast\|full\|custom>` | Dispatch to `/dev-kit:harness-mode`. |

```bash
/dev-kit:gate-select                       # = show
/dev-kit:gate-select show                  # explicit read-only
/dev-kit:gate-select classify              # one-shot: actor_type + recommended_gate
/dev-kit:gate-select route --json          # same, plus writes .dev-kit/.pr-route.json
/dev-kit:gate-select enable review         # turn review gate on
/dev-kit:gate-select disable security      # turn security gate off
/dev-kit:gate-select set review enabled true
/dev-kit:gate-select sync                  # push to gh variable set
/dev-kit:gate-select init                  # synthesize gates.json from marker
/dev-kit:gate-select pick                  # legacy 6-question picker
/dev-kit:gate-select install-project       # = /dev-kit:ci-setup
/dev-kit:gate-select install-session full  # = /dev-kit:harness-mode full
```

## `show` output

```text
ACTOR ROUTE (lib/actor_classifier, deterministic — never calls gh)
  actor_type:        consumer_fork
  repo_kind:         dev_harness_kit
  recommended_gate:  fork_pr_review_environment
  head_branch:       feat/actor-route-gate-select
  base_branch:       main
  remote:            eve/dev-harness-kit
  reason:            fork PR without maintainer signal — route via
                     fork-pr-review.yml Environment gate (manual approval required)

PROJECT GATES (.dev-kit/gates.json + .dev-kit/ci-config.json marker)
  review.yml     enabled=true   var=GATES_REVIEW_ENABLED
  security.yml   enabled=false  var=GATES_SECURITY_ENABLED   # disabled via gates.json
  maintenance.yml enabled=true  var=GATES_MAINTENANCE_ENABLED
  marker.runners: [ci.yml, auto-fix-pr.yml, review.yml, maintenance.yml]
  marker.gates_source: gates.json

SESSION GATES (.dev-kit/harness-mode.session.json, reset every SessionStart)
  mode: full
  optional (picker-toggled):
    tdd_scope_judge, slop_detector, pre_commit_review,
    maintenance, security_owasp, babysit_pr
  correctness (always on): stop_verify, secret_scan, intent_integrity, gh_ci_required

AI-JUDGE GATES (skills shipped with the plugin; scheduled by .github/workflows/*.yml)
  /dev-kit:review        enabled (CI: review.yml,  if: vars.GATES_REVIEW_ENABLED    != 'false')
  /dev-kit:security      enabled (CI: security.yml, if: vars.GATES_SECURITY_ENABLED  != 'false' — but gates.json says disabled; CI gate job SKIPPED)
  /dev-kit:maintenance   enabled (CI: maintenance.yml, if: vars.GATES_MAINTENANCE_ENABLED != 'false')
```

### How `show` reads each dimension

```bash
# Actor route — deterministic; never calls gh.
python3 -m lib.actor_classifier --root . --json

# Project — gates.json is the SSOT; marker.gates_source is the audit breadcrumb.
python3 -m lib.gates_state show --json --root .
python3 -c "import json; print(json.load(open('.dev-kit/ci-config.json'))['runners'])"
python3 -c "import json; print(json.load(open('.dev-kit/ci-config.json'))['gates_source'])"

# Session — delegated to the existing CLI.
python3 -m lib.harness_mode_state show --json --root .

# AI-judge — static; the 3 judge skills are always loaded with the plugin.
echo "/dev-kit:review:    enabled"
echo "/dev-kit:security:  enabled (CI gate follows gates.json)"
echo "/dev-kit:maintenance: enabled (CI gate follows gates.json)"
```

## `pick` flow (legacy AI-judge picker, kept as compat)

The `pick` flow ends by **writing gates.json** (via `lib.gates_state enable/disable`)
and dispatching `ci-setup` (no `--exclude` — gates.json is the SSOT). The
`pick` mapping is:

| AI-judge pick | gates.json writes | Result |
|---|---|---|
| `review` | `enable review`, `disable security`, `disable maintenance` | review.yml only |
| `review + security` | enable review + security, disable maintenance | review + security |
| `review + security + maintenance` | enable all three | all three judges wired |
| `Skip` | no writes | no install |

After writing gates.json, the `pick` flow dispatches `ci-setup` which:
1. Reads gates.json → derives the install set.
2. Copies the matching workflow templates.
3. Runs `gate-select sync` (prompted) so the GH repo variables are pushed.

The legacy `--exclude=` argument is no longer threaded by gate-select — the
SSOT moved to JSON. A `ci-setup --exclude security.yml` on a consumer WITH
gates.json is logged as `::notice::` and ignored (defensive — a stale
caller doesn't silently regress).

## `enable` / `disable` / `set`

```bash
# Atomic JSON write; idempotent re-runs are stable.
python3 -m lib.gates_state enable review
python3 -m lib.gates_state disable security
python3 -m lib.gates_state set review enabled false
```

`enable` and `disable` are sugar for `set <gate> enabled true|false`. The
`enabled` field accepts `true|false|1|0|yes|no` (case-insensitive). Other
fields (`workflow`, `var`) require their canonical values — `set` calls
`validate` and exits 2 on shape violation.

## `sync`

```bash
python3 -m lib.gates_state sync [--root PATH] [--repo OWNER/REPO]
```

For each gate in `DEFAULT_GATES` order, run one `gh variable set
GATES_<NAME>_ENABLED --repo OWNER/REPO --body true|false`. Behavior:

- `gh` not on PATH or unauthenticated → `::warning::` + exit 3; gates.json
  is the SSOT and remains updated so a follow-up `sync` after `gh auth
  login` catches up.
- No GitHub remote → exit 3 with a hint to set one.
- Per-gate failure → captured in the report; exit 1 if any failed.
- Idempotent (`gh variable set` overwrites); re-running is safe.

## `init` — synthesize gates.json from a pre-refactor consumer

When a consumer installed dev-kit before this refactor, their
`.dev-kit/ci-config.json` has `runners` listing only the workflows that
were actually installed (e.g. `[ci.yml, auto-fix-pr.yml, review.yml]` if
they previously used `--exclude security.yml`). One-step migration:

```bash
python3 -m lib.gates_state init [--root PATH]   # reads marker.runners, writes gates.json
```

`init` synthesizes: every workflow in `marker.runners` that's NOT
`ci.yml` or `auto-fix-pr.yml` becomes a gate with `enabled=True`; every
workflow in the EXPECTED set NOT in marker.runners becomes a gate with
`enabled=False`. The operator can spot-check the output, then run
`gate-select sync` to push the flags to GH.

## `classify` / `route` — actor-aware PR routing

The `actor_classifier` module lifts the maintainer-vs-consumer
classification out of the four duplicated YAML `if:` predicates in
`review.yml` / `maintenance.yml` / `fork-pr-review.yml`. It reads
three local signals (no `gh` calls):

| Signal | Source | Indicates |
|---|---|---|
| `git remote get-url origin` | local git | owner/repo pair |
| `.claude-plugin/plugin.json:owner` | plugin manifest | "this repo IS the dev-harness-kit source" |
| `.dev-kit/team.json:maintainers` | optional hand-maintained file | GH logins trusted as maintainers |

Plus an optional `--gh-author-association` injection for callers that
already verified the value via `gh api`. The five scenario outcomes map
to one of three recommended gates:

| Scenario | `actor_type` | `recommended_gate` |
|---|---|---|
| dev-harness-kit, maintainer | `maintainer_self` | `standard_gates` |
| dev-harness-kit, no maintainer signal | `consumer_self` | `standard_gates` (server-side trusted-author filter still applies) |
| dev-harness-kit fork, maintainer | `maintainer_fork` | `standard_gates` (server-side `pull_request_target` + `author_association` filter) |
| Any fork, no maintainer signal | `consumer_fork` | `fork_pr_review_environment` |
| No git origin | `unknown` | `manual_review` (fail-closed) |

```bash
# Read-only — print the classification for the current HEAD branch.
python3 -m lib.actor_classifier --root .

# Same payload, machine-readable JSON.
python3 -m lib.actor_classifier --root . --json

# Persist the breadcrumb `hooks/pr-create-route.sh` consumes on
# every `gh pr create`. The hook runs the same command internally;
# this sub-command is the operator's escape hatch to inspect or
# pre-populate the route before opening a PR.
python3 -m lib.actor_classifier --root . --write-breadcrumb --json
```

The `hooks/pr-create-route.sh` PreToolUse:Bash hook fires on every
`gh pr create` invocation and runs this classifier inline. Default
mode is silent (one-line stderr summary); opt-in ask mode is toggled
via `fork_pr_confirm=on` in `.dev-kit/guard-mode.session.json` (same
shape as the existing `push_confirm` field). See
`skills/guard-mode/SKILL.md` for the picker surface.

## What is out of scope

| Surface | Today | Why |
|---|---|---|
| `lib/config_state.py` / `.dev-kit/.enabled.json` | Not used | Referenced only by `skills/config/SKILL.md` + `hooks/linear-*.sh`; gate-select reads from `gates.json` + `ci-config.json` + `harness-mode.session.json`. |
| Skill-disable mechanism for AI-judge skills | None exists | `/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance` are always-on with the plugin; gate-select orchestrates their **CI wiring** + **on/off** via `gates.json`, not their skill-level enablement. |

## Rules (no exceptions)

- **0-arg UX (MUST-21)**: zero args. Branching via `when_to_use` auto-match + sub-commands.
- **No marker-only state file**: gate-select WRITES `.dev-kit/gates.json` (the SSOT) and dispatches `ci-setup` for the marker; the marker records `gates_source` (`gates.json` | `ci-setup`) as the audit breadcrumb.
- **HOTL (MUST-29)**: every edit (sub-command with side effects) asks before writing. `show` and `init --dry-run` are read-only.
- **No option prompts on `show`** (MUST-NOT-13): `show` prints state and exits.

## Next step

After a `pick` or `enable/disable + sync` that changed gates, run
`/dev-kit:ci-setup` (or `install-project`) to land the matching workflow
files. For drift verification, `/dev-kit:ci-doctor` now checks the
gates.json ↔ vars consistency.
