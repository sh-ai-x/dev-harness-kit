# Changelog

All notable changes to dev-harness-kit are documented here.

## [Unreleased]
- **fix(reducer):** add `subject_observability` submetric + parent-score fallback
  so `measurement_integrity.event_coverage` is observable in worktrees that
  have not yet run a `lib/execute.py` build step. Closes #702. Schema version
  bumps 2 -> 3. The session-scoped `step.started` / `step.completed` pair is
  emitted by the hook stack at SessionStart and Stop/SessionEnd so the metric
  has a baseline in every Claude Code session.

### Fixed — fork-PR review comments now post (dispatched-run workaround)

Fork PRs that the maintainer approved via the `fork-pr-review`
GitHub Environment previously sat through a green-but-empty AI
review: the dispatched `review.yml` / `maintenance.yml` runs
completed with `conclusion=success` but posted NO AI review
comments. Every audit log recorded `verdict=MISSING`. Observed
against PRs #682 and #687 in August 2026.

Root cause: `anthropics/claude-code-action@v1` (pinned at
`558b1d6cab4085c7753fe402c10bef0fbb92ac7a`) silently no-ops on
`workflow_dispatch` events. Agent mode writes only
`claude-prompt.txt`, NOT `claude-user-request.txt`, so the SDK
treats the slash command `/dev-kit:review --diff <PR>` as literal
text. Combined with the `isEntityContext()` gate that disables
`mcp__github_inline_comment__create_inline_comment` for
workflow_dispatch, the dispatched run exits with `num_turns: 0,
duration_ms: 21, is_error: false` — the run is GREEN but no AI
review comments are posted. Upstream issues
`anthropics/claude-code-action#635` + `#1644`.

Workaround: skip `claude-code-action` on `workflow_dispatch` and
invoke `claude -p` directly via the new helper
`bin/ci-claude-p.sh <skill> <pr_number>`. The script installs
Claude Code CLI if missing, then runs `claude -p` with the
provider-specific env vars the workflow controls. The existing
`claude-code-action` step's `if:` is tightened to
`&& github.event_name == 'pull_request'` so the broken path is
skipped on dispatch but still runs for same-repo `pull_request`.

The `fork-pr-review.yml` gate itself is INTENTIONALLY unchanged: it
still gates on the `fork-pr-review` Environment (manual approval
required), still dispatches review.yml / maintenance.yml via
`workflow_dispatch`, and still writes the aggregate
`fork-pr-review/ai-judges` commit status. Only the downstream
workflows' judge steps changed.

- `bin/ci-claude-p.sh` (new) — single helper for the `claude -p`
  invocation shape; 9 call sites (3 judges x 3 providers) all
  share it. Header comment explains the upstream issue references.
- `.github/workflows/review.yml` — `pull_request_target` added to
  triggers; 6 new `claude -p` workaround steps (review + security,
  one per provider); 6 existing `claude-code-action` steps tightened
  with `&& github.event_name == 'pull_request'`.
- `.github/workflows/maintenance.yml` — same treatment for the
  `maintenance_judge` job (3 workaround steps + 3 tightened
  `claude-code-action` steps).
- `.github/workflows/fork-pr-review.yml` — header comment expanded
  to document the workaround; gate behavior unchanged.
- `tests/test_ci_claude_p_sh.py` (new) — 14 hermetic subprocess
  tests for the helper (static checks, arg validation, required
  env vars).
- `tests/test_dispatched_run_uses_claude_p.py` (new) — 5
  YAML-parsing pin tests for the workflow shape (9 workaround
  steps present, 9 `claude-code-action` steps tightened, gate
  unchanged, helper executable, helper references upstream
  issues).
- `docs/quality/maintenance-gate.{md,ko.md}` — "Fork PRs" section
  documents both the gate (unchanged) and the dispatched-run
  workaround (new), with a pointer to the upstream issues.

### Breaking — slim sweep (PR-1)

- **chore(skills)!:** Slim sweep — drop `user-invocable: true` from `/dev-kit:valuate` (kept as model-use); cut `/dev-kit:audit` slash (folded into `/dev-kit:inspect --secrets` / `--slop`); merge `/dev-kit:bootstrap-full` into `/dev-kit:bootstrap` with runtime Y/n prompt for ci-setup (pass `--skip-ci` to decline); document MCP integration as intentionally out of scope ([decision 0001](docs/decisions/0001-no-mcp.md)). `/dev-kit:config` picker drops the non-functional MCP option. **Breaking:** `/dev-kit:audit` and `/dev-kit:bootstrap-full` removed; `/dev-kit:inspect` gains `--secrets` and `--slop` flags.

- **chore(lcs)**: Drop the LCS substrate entirely (#463). Measured rationale: in CLI invocation mode, every LCS call pays ~250 ms of Python startup tax against a single `git`/`gh` fork. PR #462 (merged at `63fa66f`) trimmed the two LCS reads from `hooks/git-guard.sh` and `hooks/worktree-guard.sh`; the slot-version read was net-negative by 220 ms per push and the worktree-list read was net-negative by a similar margin. After that trim LCS had zero production consumers in this repo — the substrate was preserved only as a substrate. The cost of keeping that substrate exceeded its option value, so we drop it. Rebased onto the docs-restructure (PR #464) reorganization so file paths in this entry match the current `docs/home/`, `docs/stages/`, `docs/quality/` layout.

  Deleted: `bin/dev-kit-lcs.py`, `bin/dev-kit-lcs-route.py`, `lib/lcs_server.py`, `lib/lcs_resources/` (8 handlers), `tests/test_lcs_*.py` (14 test files), `tests/test_dev_kit_lcs_cli.py`, `skills/lcs/SKILL.md`. Removed the Phase 4 valuation auto-gate in `lib/execute.py:_enforce_valuation_gate` (the build stage now proceeds unless the operator flags a non-PROCEED verdict manually). Renamed `lib/valuation_engine.py:decision_persists_to_lcs` → `decision_is_canonical_envelope` (the function was always a pure shape validator; the LCS-suffixed name lied about a persistence that never existed in the engine). Removed `audit_lcs` from `tools/harness_audit.py` (5 harnesses remain: hooks / eval / plan_value / research / interview). Stripped stale LCS narrative from `hooks/git-guard.sh` / `hooks/worktree-guard.sh` comments and from `lib/{interview,research,runtime_adapters,valuation}_engine.py` docstrings.

  Deleted narrative docs: `docs/lcs/` (4 files), `docs/lcs-in-normal-workflow.{md,html}`, `docs/home/00-index.ko.{md,html}`, `docs/home/00-index.html`, `docs/skills/lcs.md`, `docs/proposals/harness-architecture/` (26 files), `docs/planning/PROPOSAL-IMPLEMENTATION-PLAN.{md,html}`. Rewrote `docs/home/00-index.md` to drop the LCS marketing narrative. Updated `docs/stages/STAGES.{md,html}` (Phase 4 gate narrative gone), `docs/skills/{valuate,interview}.md` (LCS URI → on-disk file references), `README.md` (rewrote the LCS section as a "Skill composition" table).

  Verification grep: `grep -rn 'lcs://\|dev-kit-lcs' hooks/ bin/ lib/ docs/ tools/ --include='*.sh' --include='*.py' --include='*.md' --include='*.yaml'` returns zero matches in shipped paths.

  Closes the substrate work; PRs #457 #458 #459 #460 #461 closed with pointers to this entry. The historical branches (`feat/lcs-perf-evidence`, `feat/lcs-ux-proposal`, `feat/lcs-ux-discovery`, `feat/lcs-ux-summary-blocks`, `feat/lcs-ux-nl-router`, `feat/lcs-ux-reserved-routes`) remain on disk for archaeology but are not targeted at this repo.

## [0.1.4] - 2026-07-07

### Changed — split into PR A and PR B (this PR = A)

This release rolls up three pending PRs (#38, #39, #40) but is split
into two PRs because GitHub's self-trigger block prevents the workflow
file from firing on PRs that modify it. **PR A (this one)** drops the
bootstrap/body phase split and fixes the doc/test drift; **PR B**
applies the `pull_request_target` migration + fork-safety guards to
the local workflow files (PR B can't get auto-reviewed, but is
mechanical and well-tested).

### PR A — drops #38 bootstrap/body phase split

The split was intended to work around `anthropics/claude-code-action@v1`'s
workflow-validation gate by landing the 3 workflow files in their own PR.
The `pull_request_target` migration (PR B) solves the same problem
more cleanly without forcing consumers into a 2-PR install. The
bootstrap-body split also introduced a critical regression: the
bootstrap-only install state could not pass `scripts/validate.py`
(the marker was intentionally absent, so the validator saw
`phase='missing'` and checked ALL_REQUIRED, reporting 8 spurious
missing files).

- `lib/ci_setup.py:install_ci_config()` is back to its single-shot signature.
  `BOOTSTRAP_PATHS` / `BODY_PATHS` / `_resolve_paths` / `phase=` kwarg /
  marker-skip-during-bootstrap are removed.
- `templates/ci/scripts/validate.py` reverted to flat `REQUIRED_FILES`
  (8 entries) with no `phase` parameter.
- `skills/ci-setup/SKILL.md` Two-phase install section removed;
  Iron-Law flag list restored; Files Installed table back to single
  15-row list.
- `tests/test_ci_setup_split_install.py` deleted (194 lines of tests
  for the dropped phase split).
- `tests/test_ci_setup.py::test_post_install_checklist_is_complete`
  needle list restored (removed `'/dev-kit:ci-setup'` added by #38).
- `tests/test_review_gate.py` now reads the consumer template SSOT
  (`templates/ci/.github/workflows/review.yml`) instead of the local
  `.github/workflows/review.yml`. The two were drift-prone: a future
  edit to one copy would silently pass tests against whichever copy
  was in lockstep.
- `docs/quality/ci-setup.md` FAQ rewritten to describe the new gate-tolerance
  contract (Approve + warning, not hard fail).

### PR B (separate, follow-up) — extends #40 to local workflow

PR #40 only migrated the consumer template; the dev-kit repo's OWN
`.github/workflows/review.yml` still had `on: pull_request:` and the
same workflow-validation skip bug. PR B applies the full migration
to the local workflow (pull_request_target trigger, concurrency
group, per-job fork-safety guard on review/security/gate), adds the
fork guard to `.github/workflows/auto-fix-pr.yml`, and adds a visible
`gh pr comment` signal when the gate defaults to Approve on missing
verdict (so silent skips aren't invisible to the PR author).

### Notes
- Bootstrap trade-off (unchanged from PR #40): a PR that ADDS
  `review.yml` for the first time cannot be triggered under
  `pull_request_target` (file isn't yet on main). The fix assumes
  `review.yml` is already on the consumer repo's main.
- No schema or marker version bump — `MARKER_SCHEMA_VERSION` is
  unchanged.

## [0.1.3] - 2026-07-07

### Fixed
- **`templates/ci/.github/workflows/review.yml` Combined verdict gate**: PR mode and `workflow_dispatch` mode now share symmetric tolerance. Previously PR mode `exit 1`'d on missing verdicts (`Missing verdict (review='' security='')`) whenever the `/dev-kit:*` agents skipped posting a `**Verdict:**` comment as the first line of a PR comment, while `workflow_dispatch` mode defaulted to Approve. The hard-fail contradicted the gate's own documented tolerance contract (lines 354-358) for unparseable verdicts. The patched gate surfaces a `::warning::` in both modes when a verdict is missing and defaults the missing dim to `Approve`. The human gate (`REVIEW_REQUIRED` / `CHANGES_REQUESTED`) remains authoritative for merge-block.

### Added
- **`lib/ci_setup.py:lint_installed_workflows()`** + **`_KNOWN_STALE_PATTERNS` tuple**: a non-fatal scan over installed `EXPECTED_PATHS` for known-stale patterns whose root cause previously slipped past local smoke tests (`scripts/validate.py` + `scripts/ci-local.sh` both pass on stale installs because they don't exercise the GitHub Actions gate). The first known-stale pattern is the pre-0.1.3 gate hard-fail. Findings populate `InstallReport.warnings` and the skill body prints them in the install summary table.
- **`InstallReport.warnings: List[str]`** field (defaults to `[]`); backward-compatible with existing test contracts.
- **`install_ci_config(..., lint: bool = True)`** kwarg: lint runs by default on every install, including no-op idempotent re-installs (so a user running ci-setup with no `--force` still gets the warning if their previously-installed `review.yml` is stale). Set `lint=False` to suppress.
- **`skills/ci-setup/SKILL.md`**: new `## Phase 1.7 -- Lint pass` section and Iron-Law paragraph documenting the warning-class output.

### Notes
- `MARKER_SCHEMA_VERSION` unchanged (`1.0.0`); consumers who re-run `ci-setup --force` get the gate-tolerance fix without any version gate.
- The new lint pass is the surface area for adding more known-stale patterns in future: add a tuple to `_KNOWN_STALE_PATTERNS`, ship a release.

## [0.1.2] - 2026-07-07

### Fixed
- **`templates/ci/.github/workflows/review.yml` verdict regex** (and the sibling `.github/workflows/review.yml`): anchor with `^` so prose lines containing the substring `**Verdict:**` mid-sentence are NOT picked by `tail -1` as the verdict header. Without the anchor, the gate's `severity_gate` reads `verdict=""` on PRs where the agent's review output mentions the verdict keyword mid-sentence, and exits 1 with `Missing verdict (review='' security='')`. Regression test: `tests/test_review_gate.py` (6 regex + 2 byte-equality tests). Same patch applied to `.github/workflows/review.yml` so the two files stay in lockstep.

### Added
- **`lib/ci_setup.py:POST_INSTALL_CHECKLIST`** (5 items) and **`lib/ci_setup.py:_print_post_install_checklist()`**: rendered opt-in via the new `print_checklist: bool = False` kwarg on `install_ci_config()`. Covers the secrets, hooks activation, and the first-PR validation-skip rule. `<OWNER>/<REPO>` is auto-filled from `git remote get-url origin` when available.
- **`lib/ci_setup.py:preflight_probe()`** + **`ProbeResult` dataclass**: 5-line `gh` probe (auth status, repo reachable, three secret checks). All read-only; the skill never prints secret values. Returns `[SKIP]` for every probe when `gh` is absent or unauthenticated -- the install still proceeds.
- **`skills/ci-setup/SKILL.md`**: new `## Phase 1.5 -- Pre-flight probe` and `## Phase 4 -- Post-install checklist` sections. Refreshed the "Files Installed (8 expected paths)" table to 15 entries (was stale since 0.1.1).
- **`docs/quality/ci-setup.md`**: new `## Post-install checklist` section near the top + `## FAQ` section that documents the bootstrap-first-PR validation-skip rule.
- **`tests/test_ci_setup.py`**: 3 new tests (`test_post_install_checklist_is_complete`, `test_preflight_probe_skips_on_missing_gh`, `test_print_checklist_kwarg_does_not_break_existing_callers`).

### Notes
- `MARKER_SCHEMA_VERSION` unchanged (`1.0.0`); the marker stays content-only per the comment at `lib/ci_setup.py:73-77`. There is no marker-shape change in 0.1.2, so consumers running `ci-setup --force` get the verdict-regex fix without any version gate.
- Known issue (not fixed in 0.1.2): `skills/build/SKILL.md` pre-flight gate still references `ci_setup_version < "0.1.0"` while the marker no longer writes that field. The default `data.get("ci_setup_version", "0.0.0") < "0.1.0"` resolves to `False` (passes), so today's behaviour is silently permissive -- but the docs reference is misleading and should be aligned in a follow-up. Tracked separately.

## [0.1.1] - 2026-07-07

### Added
- **Worktree enforcement** (PR #22): 3 new hooks (`worktree-guard.sh`, `task-detector.sh`, `session-start-check.sh`) + `hooks/lib/worktree-detect.sh` shared discriminator + `.claude/rules/git-workflow.md` rule doc + 14 regression tests. Rule: every task = new worktree + new session + new branch; no edits in the main checkout.
- **`bin/devkit-refresh.sh`**: one-shot script that pulls the marketplace clone + rsyncs the cache. Used after PR merge to keep the plugin cache current without running `claude plugin install`.
- **ci-setup consumer-install** (PR #23 + #27): `lib/ci_setup.py` now ships 15 files (was 8). EXPANDED `EXPECTED_PATHS` with 4 worktree-rule files. New tests in `tests/test_ci_setup.py` for the new files.
- **Self-aware `review.yml`** (PR #27): the template's install step detects self-install vs consumer-install at runtime. Same workflow file works in both dev-harness-kit's own CI and consumer repos via `ci-setup`.
- **`tests/test_review_install.py`** (9 tests) + **`tests/test_worktree_guard.py`** (14 tests) + **`tests/test_ci_setup.py`** expanded (4 new tests for the 0.1.1 files).

### Changed
- **`.claude-plugin/marketplace.json`** (PR #28): `source` is now the schema-valid `url` object form (`{"source": "url", "url": "...", "ref": "main"}`) instead of a bare URL string. Fixes the "source type your Claude Code version does not support" install error.
- **`.claude-plugin/plugin.json`**: version `0.1.0` → `0.1.1`.
- **`lib/ci_setup.py`**: `MARKER_SCHEMA_VERSION` `1.0.0` → `1.2.0`; `DEFAULT_CI_SETUP_VERSION` `0.1.0` → `0.1.2`. Forces consumer repos to refresh templates on next `ci-setup --force`.
- **`tests/test_ci_setup.py`**: bumped version constants; added 4 new tests for the new files.

### Removed
- **Auto-update SessionStart hook** (PR #24 → reverted in PR #26): the SessionStart auto-update that pulled marketplace + ran `claude plugin install` was found to have a session-specific CLI bug and marginal value. Replaced with `bin/devkit-refresh.sh` (manual one-shot) for explicit opt-in refresh.

### Fixed
- **`claude plugin install` failure** (PR #28): bare string source was invalid per the marketplace schema. `url` object form makes install work.
- **Plugin cache 0.1.0 reference breakage** (post-#22 cleanup): an over-eager cache cleanup broke in-flight sessions keyed to `0.1.0`. The cache now keeps both version directories (`0.1.0/` + `0.1.1/`) so in-flight references resolve cleanly.

## [0.1.0] - 2026-07-04

### Added
- Plugin skeleton: `.claude-plugin/{marketplace, plugin/{plugin, hooks}}.json`
- 17 skills across 9 categories (bootstrap, plan, design, build, review, security, audit, shortcuts, ship)
- 15 commands (0-arg): `bootstrap / plan / design (alias) / build / review / security / ship / audit / eval / repair / config / status` + 2 shortcuts
- 5 hook scripts: `tdd-guard, bash-guard, secret-scan, slop-detector, stop-verify` (all `exit 0` advisory by default; `--strict` enables hard-block)
- lib modules: `state_codec, active_hooks_codec, write_project_md, execute, methodology/{abc,tdd,__init__}, install.sh`
- Iron Laws SSOT in `CLAUDE.md §1` (5 laws)
- `.dev-kit/` state files (state.json, .active-hooks.json, hand-off/*.md)
- Pre-impl gate (`docs/planning/PRE-IMPL-CHECK.md`) + 8-dimension cost analysis (`docs/quality/COST-ANALYSIS.md`)


