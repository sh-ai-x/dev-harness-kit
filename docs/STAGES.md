# STAGES — dev-harness-kit per-stage harness spec

> Reference: ADR-0011, ADR-0020. 6 stages × must/must-not/AC unified.

## Stage B — Bootstrap (`/dev-kit:bootstrap`)

- **Goal**: First entry into a new project, 0 → 30 min productive. Minimal setup: exactly 3 files written to a fresh repo.
- **Must**: (a) sanity read-only audit (printed to stdout; `--persist-audit` writes `.dev-kit/sanity-report.md`) (b) codebase-map lazy-loading index in CLAUDE.md §3 (c) active-hooks.json SSOT init (d) CLAUDE.md §1~§5 unified record (e) AGENTS.md full duplicate of CLAUDE.md, byte-for-byte (f) `--full-claude-md` opt-in writes `docs/CODEBASE-MAP.md`
- **Must-Not**: modify files (sanity read-only). Modify lockfiles. Speculation. Persist hand-off file (CLAUDE.md §5 pointer is enough).
- **AC**: On fresh repo: CLAUDE.md + AGENTS.md + `.dev-kit/.active-hooks.json` exist. CLAUDE.md §3 = lazy-loading index referencing canonical source files. `.dev-kit/` directory auto-created.
- **Active Skills**: `bootstrap-sanity`, `bootstrap-codebase-map`, `bootstrap-active-hooks`, `write_project_md`
- **Active Hooks**: `secret-scan`=read-only. Others OFF.
- **Hand-off out**: §5 hand-off pointer in CLAUDE.md (no separate `.dev-kit/hand-off/` file from bootstrap)

## Stage B.5 — CI Setup (`/dev-kit:ci-setup`)

- **Goal**: Replicate dev-kit's CI shape (workflows + pre-push hook + local runner) into the target repo. One-command CI parity.
- **Must**: (a) Idempotent install via `.dev-kit/ci-config.json` marker. (b) Mirror of `.githooks/pre-push` + 3 GitHub Actions workflows. (c) `validate.py` extracted from dev-kit's own `ci.yml` 5-step validate job. (d) `--force` flag for refresh; otherwise refuse overwrite.
- **Must-Not**: Modify dev-kit's own repo. Drop the marker. Delete user-created files in target.
- **AC**: All 8 expected files exist post-install. `python3 scripts/validate.py` exits 0. `.dev-kit/ci-config.json` has correct schema.
- **Active Skills**: `ci-setup` (0-arg orchestrator; hidden `--force`, `--target DIR`)
- **Active Hooks**: same as Bootstrap (`secret-scan`=read-only)
- **Hand-off out**: gates `build` via marker file

## Stage 1 — Plan+Design (`/dev-kit:plan`)

- **Goal**: idea → PRD.md + `phases/<name>/{index.json, step<N>.md}`
- **Must**: 5 gates (frame → validate → non-goals → decompose → emit) in **one Ralph loop, safety_valve=8** (MUST-15). The `validate` gate fuses the old evidence / diff-profit / socratic gates into one composite convergence test: `evidence_count >= 3` AND `value_score = LTV × reachable_users / cost >= 3.0` AND `ambiguity_score <= 3`. Phase index.json written via `lib/execute.py:register_step()` so every step carries an explicit `status` (`unimplemented` → `pending` → `in_progress` → `completed`, plus `error` / `blocked` for runtime).
- **Must-Not**: Write code, build, or deploy. Write artifacts other than PRD.md + phases/ + .prd/ + .dev-kit/hand-off/. Set runtime-only statuses (`in_progress`, `completed`, `error`, `blocked`) — those belong to harness-runner. Run the old 5-question grill-me (replaced by the ambiguity loop).
- **AC**: PRD.md 6-section DoD pass. `phases/<name>/step<N>.md` 4 fields (must-read / instruction / AC / Don't). `phases/<name>/index.json` schema valid. `value_score >= 3.0` AND `ambiguity_score <= 3` OR `loop-log.json` shows `status: held` + user acknowledgement. `loop-log.json` narrowing appended per cycle.
- **Active Skills**: `plan` (self-contained, formerly `plan-ralph`), `build-harness-engine`
- **Active Hooks**: `stop-verify`=ON. `slop-detector`=OFF (planning doc allowed). Others OFF.
- **Hand-off out**: `plan→build.md`

## Stage 3 — Build (`/dev-kit:build`)

- **Goal**: Per-step code completion per `phases/<name>/step<N>.md` + regression GREEN.
- **Must**: (a) Follow `phases/<name>/step<N>.md` exactly. (b) Run AC commands and quote output. (c) Bug → reproduce → root-cause → regression test → minimal fix (4-phase debug via `build-debug`). (d) 2-commit protocol (feat + chore).
- **Must-Not**: Speculate on AC ("should work", "probably fine"). Delete `output.json`. Batch multiple changes.
- **AC**: All steps `status=completed`. `pytest` exit code 0 + count quoted. 2-commit protocol followed.
- **Active Skills**: `build-engine`, `build-tdd`, `build-debug`, `build-verify`, `build-refactor`, `build-prune`, `build-methodology`
- **Active Hooks**: `tdd-guard`, `bash-guard`, `secret-scan`, `slop-detector`, `stop-verify` — all ON
- **Sub-agent**: Phase 3 (planned). Currently sequential-only.
- **Hand-off out**: `build→review.md`

## Stage 5a — Review (`/dev-kit:review`)

- **Goal**: Find correctness + security + architecture defects in the diff + PR-style verdict.
- **Must**: Every finding has `failure_scenario` + `confidence`. **Single-message 3-dim fan-out**. Separate verifier pass.
- **Must-Not**: Skip verifier pass. Report unverified critical.
- **AC**: PR summary `**Verdict:**` + sorted inline findings. Per-severity count.
- **Active Skills**: `review`
- **Active Hooks**: `slop-detector`, `secret-scan`, `stop-verify` = ON. `review-pre-commit` (git) + `dev-kit-review.yml` (CI).
- **Hand-off out**: `review→ship.md`

## Stage 5b — Security (`/dev-kit:security`)

- **Goal**: OWASP Top 10 2025 (A01~A10) audit.
- **Must**: Per-category breakdown table. Single-message 10-dim fan-out. Verifier CONFIRMED ≥ 5.
- **Must-Not**: Skip A0X IDs. Unverified critical.
- **AC**: Per-category table. Per-severity verdict.
- **Active Skills**: `security`
- **Active Hooks**: Same as Review.
- **Hand-off out**: `security→ship.md` (independent of Review)

## Stage 6 — Ship (`/dev-kit:ship`)

- **Goal**: Issue release-ready tag.
- **Must**: Review verdict=Approve + Verify AC passed + Pre-push main-block passed.
- **Must-Not**: Direct push to main. `--no-verify` abuse.
- **AC**: git tag + CHANGELOG entry + pre-release smoke.
- **Active Skills**: (none, manual gate only)
- **Active Hooks**: `stop-verify`=ON.

## Cross-cutting — Audit (`/dev-kit:audit`)

- **Goal**: Batch audit of slop + secrets.
- **Must**: Output HIGH/MEDIUM/LOW buckets. Banned-phrase regex SSOT.
- **Must-Not**: Modify files (read-only).
- **AC**: HIGH ≥ 5 = warning. 0 findings = clean.
- **Active Skills**: `audit-slop`, `audit-secret`

## Cross-cutting — Inspect (`/dev-kit:inspect`)

- **Goal**: Whole-codebase (not per-PR, not per-diff) health audit
  across 6 dimensions in parallel: `dead`, `dup`, `smell`, `overeng`,
  `cleancode`, `slop`. Produces one markdown report at
  `.dev-kit/inspect-report.md`.
- **Must**: Single-message 6-dim fan-out. Verifier pass on survivors.
  HIGH/MED/LOW bucketing in the report. `failure_scenario` required per
  finding.
- **Must-Not**: Modify source files (read-only invariant). Post PR
  comments. Edit the report from outside the skill.
- **AC**: Report exists at `.dev-kit/inspect-report.md`. Verdict is one
  of `Critical | Major drift | Minor drift | Healthy`. Per-dimension
  summary table present.
- **Active Skills**: `inspect`

## Cross-cutting — Report (`/dev-kit:report`)

- **Goal**: Render the latest `eval-report.md` + `inspect-report.md` as
  one self-contained HTML file at `.dev-kit/report.html`. Distinct
  human action ("view the report in a browser") with a distinct
  artifact; not an option on `/dev-kit:eval` or `/dev-kit:inspect`.
- **Must**: Self-contained HTML (no `<script>`, no external CSS/IMG,
  inline styles only). Dark-mode aware. Defensive HTML escaping on
  every interpolated value.
- **Must-Not**: Read source files (only the two report artifacts). Make
  network calls.
- **AC**: `.dev-kit/report.html` exists. Either contains both report
  sections (when both source artifacts are present) or a "not found"
  banner (when one or both are missing). Open in a browser with no
  console errors.
- **Active Skills**: `report`

## Cross-cutting — Eval (`/dev-kit:eval`)

- **Goal**: Asset freshness eval (CLAUDE.md / skill / hook / Iron Law).
- **Must**: 4-axis score (semantic_drift / completeness / correctness / consistency). 2-judge cross-check.
- **AC**: ≥ 8 OK. < 5 ROT → CI fail.
- **Active Skills**: `audit-eval`, `audit-a2a` (Phase 3)

## Cross-cutting — Repair (`/dev-kit:repair`)

- **Goal**: Eval-Repair 8-step loop. Last step = user 1× approve.
- **Must**: 7 steps auto. Step 8 Human Review is the only sync STOP.
- **Must-Not**: Auto commit diff. Change review / design / build state itself.
- **AC**: Human `approve|reject|defer` is the only commit.
- **Active Skills**: 9 Specialized Fixers (one per category).
