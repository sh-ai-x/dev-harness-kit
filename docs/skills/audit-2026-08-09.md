# Skill audit 2026-08-09 — Linear SHO-142

Audit goal: decide whether `/dev-kit:ship` and any other low-usage skills in this
plugin should remain public, merge with another workflow, or be removed.

Constraint (from Linear SHO-142):
- Do not duplicate PR #537 (feat-remove cleanup, merged 2026-08-02).
- Do not recreate PR #536 (report/inspect consolidation, merged 2026-08-02).
- Verify current release automation before changing ship.
- Record evidence and migration references before any deletion.

## Hard ambiguity — telemetry unavailable

`tools/skill_usage.py --days 90` was run from the repo root on 2026-08-09. The
default glob (`tools/skill_usage.py:265-275` `_default_logs_glob()`) returned
`logs/{claude-code,codex}/**/*.jsonl` because `logs/codex/` is present on this
checkout; the literal stderr was:

```
[skill-usage] no skills found under logs/{claude-code,codex}/**/*.jsonl
```

`~/.claude/logs/` contains only `.gitkeep` + `.gitignore`. The 41 entries under
`skills/` (40 non-ship) all show `turns=0 invocations=0` because there is no
log data to mine.

Per the task's hard-ambiguity rule, no cut proposals are made in this audit.
The project's standing rule, captured here verbatim from two pieces of
session memory that are not committed to this repo, is:

> "DO NOT delete any skill whose 90-day usage exceeds a sensible threshold
> without explicit user confirmation." — `feedback-dont-cut-heavily-used-skills`
>
> "Before proposing any skill cut/merge: read SKILL.md + lib/ + scripts/ +
> 30d usage telemetry; don't pattern-match on review alone." — `feedback-read-code-before-cut-proposal`

Both rules require observed-usage evidence before a cut. Without telemetry,
that evidence is unobtainable, so the burden-of-proof rule forces a
**no-cut** default.

## Verdict table

| Skill | Verdict | Evidence line |
|---|---|---|
| `/dev-kit:ship` | KEEP | Referenced 12+ times as canonical release endpoint in user-facing docs; tag emit is automated by `.github/workflows/version-bump.yml` (lines 154-184, `name: Emit annotated tag`) and marketplace refresh by `bin/devkit-refresh.sh`; no PR history of redundancy (`gh pr list --search "ship" --state all --json number` returns no removal PRs); SKILL.md has no lib/scripts/tests so the slash is a human gate-check anchor, not a code wrapper. |
| 40 other catalog skills | NO CHANGE | Cannot propose any cut without telemetry. All 41 entries under `skills/` return 0 turns / 0 invocations in 90d because logs are absent, not because they are unused. Per the burden-of-proof rule (see *Hard ambiguity* below), cuts without observed-usage evidence are rejected. |

## Constraint verification

- **PR #537 (feat-remove cleanup)**: re-read; it removed a deprecated skill
  that was already superseded by `prune --target`. This audit does NOT touch
  `prune` or any equivalent.
- **PR #536 (report/inspect consolidation)**: re-read; it merged HTML
  rendering behind `inspect --html`. This audit does NOT touch `inspect`,
  `report`, or HTML rendering.
- **Release automation verification**: `.github/workflows/version-bump.yml`
  handles tag emission + manifest bump (lines 154-184: annotated tag at
  HEAD, push to origin); `bin/devkit-refresh.sh` handles cache refresh
  (marketplace git pull + rsync to `~/.claude/plugins/cache/dev-kit/dev-kit/
  <version>/`). Both run independently of `/dev-kit:ship`. Removing ship
  would not break the release pipeline (but we are NOT removing it).

## Why KEEP `/dev-kit:ship`

1. **Telemetry unavailable** — cannot confirm low usage. Per the
   burden-of-proof rule (see *Hard ambiguity* below, which inlines the rule
   text verbatim), the proposer needs observed-usage evidence to cut; without
   telemetry, the threshold cannot be observed in either direction. The same
   rule applies symmetrically: an "unused" skill is not cut-eligible when
   logs are absent, because the absence of evidence is not evidence of
   absence.
2. **High doc-graph coupling** — referenced as the canonical next-step in
   12+ user-facing docs:

   - README + stage pages (8 hits): `README.md:127,144,780`,
     `README.ko.md:123,140,739`, `docs/home/00-index.md:18`,
     `docs/home/00-index.ko.md:18`, `docs/stages/STAGES.md:78`,
     `docs/stages/STAGES.ko.md:155`, `docs/observability/token-efficiency.md:305`.
   - User-facing skill docs (`docs/skills/*.md`, 8 files): `docs/skills/{build,
     review, security, prune, refactor, docs-maintenance, bump, ship}.md`.
     Note: `docs/skills/{evaluate, research, babysit-pr}.md` exist but do
     **not** reference `/dev-kit:ship` (verified 2026-08-09 via
     `grep -l "/dev-kit:ship" docs/skills/*.md`).
   - Skill bodies (`skills/*/SKILL.md`, 11 files): the same 8 above plus
     `skills/{evaluate, research, babysit-pr}/SKILL.md`, which each contain a
     `Next step: /dev-kit:ship` hand-off reference.

   Removing ship would require a coordinated purge across ~19 doc files for
   no measured gain.
3. **No PR history of redundancy** — no PR has ever proposed removing ship.
   PRs #537 and #536 cleaned up two *different* deprecated skills
   (`feat-remove`, `report`), not ship.
4. **Distinct human-facing role** — `/dev-kit:ship` is the only place where
   the release-gate state (Review verdict + main-block pass) is exposed as a
   single human-checkpoint slash. The release pipeline is layered:

   - **Auto-tag layer**: `.github/workflows/version-bump.yml` lines 154-184
     emit an annotated tag at HEAD whenever a `chore(release): bump dev-kit
     to vX.Y.Z` PR merges. This is the *real* tag emitter on the normal
     release path and runs without any slash invocation.
   - **Slash layer**: `skills/ship/SKILL.md` documents the slash as a
     "Release Gate" with `Read Bash` only, `model: haiku`, and a 4-step
     *Behavior* (verify main-block, verify Review verdict, CHANGELOG entry,
     `git tag + push`). It is **not** the primary emitter — `version-bump.yml`
     is — but it remains the only place that exposes the *gate state* (the
     Review verdict + the main-block pass) as one human-checkpoint slash
     anchored to Stage 6 in `docs/stages/STAGES.md`.

   Both layers exist independently today; removing the slash does **not**
   break tag emission. The slash's value is the *human gate-check anchor*,
   not the tag itself. (A future cleanup could trim the slash's behavior
   steps to *only* the gate-check; this audit does not propose that.)
5. **Test coupling** — `tests/test_smoke.py:140` asserts `"ship"` is in
   `ahc.DEFAULT_MATRIX` (the 7 active-hook stages). Removing ship would
   require updating the smoke test alongside the skill deletion, which is
   exactly the kind of cross-file change that should be evidence-led, not
   pattern-matched.

## Open follow-up

If a future audit has telemetry, the candidates worth a second look are the
slash commands that appear in `prune-propose` 0-invocation output AND have no
internal (model) invocations. Without logs, we cannot draw that line today —
re-run this audit in 30 days once logs have accumulated.
