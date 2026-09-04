# ADR-0023: Marketplace scope policy — one canonical plugin per marketplace

**Status:** Accepted (2026-09-04)
**Trigger:** Issue #783 — unsigned force-push to `main` registered `dev-kit-lite` in `dev-harness-kit`'s `.claude-plugin/marketplace.json`, bypassing PR review, LLM-judge gates, and signature chain.
**Supersedes:** the implicit "anything goes into the marketplace JSON" model that existed before 2026-09-01.

## Context

On 2026-09-01, commit `e296ce6a` was authored as `dev-kit-lite <lite@dev-kit-lite>` (an identity not present in any local git config) and force-pushed directly to `main`, replacing tip `4a39a1de` (PR #781). The commit registered a sibling plugin entry into `.claude-plugin/marketplace.json` pointing at an external repo (`sh-ai-x/dev-harness-kit-lite`) created the same day, with **no PR, no review, no signature, no ADR, no proposal**.

Investigation findings (issue #783):

- The marketplace entry's `description` (8 skills, 7 hooks, 6 stages) was fabricated; the lite repo's actual on-disk inventory was 15 skills, **0 hooks**, **0 stages**.
- The lite repo's own `plugin.json` description (11/6/8) and GitHub repo description (7/7/6) also disagreed with the file system.
- Both `dev-kit` and `dev-kit-lite` would expose skills under the `/dev-kit:*` prefix — two plugins in the same marketplace claiming the same skill namespace collides at install time.
- `main` had **no branch protection** (`404 Branch not protected`), so the force-push was not blocked.
- The PGP signature chain broke at `e296ce6a` — the previous tip was verified; this commit is `verification: { verified: false, reason: "unsigned" }`.
- The lite repo is a fresh independent repo (`fork: false`, `parent: null`), not a fork — dev-kit CI/review cannot vet its contents.

The blast radius of "any commit to `.claude-plugin/marketplace.json` is a marketplace edit" was structurally too wide: a single line change silently repoints every Claude Code instance that installs our marketplace.

## Decision

`.claude-plugin/marketplace.json` in the dev-harness-kit repo **lists exactly one plugin entry: `dev-kit`**. Adding any other entry (a pruned sibling, a fork, an external client plugin) requires a separate ADR and a PR with the full review-gate verdict stack.

### 1. Single-plugin rule

The dev-harness-kit marketplace exposes only the dev-harness-kit plugin. A "lite" or "core-only" variant — if ever justified — lives in a **separate repo and a separate marketplace.json** (e.g. `sh-ai-x/dev-harness-kit-lite/.claude-plugin/marketplace.json`). Consumers opt into one marketplace or the other via `/plugin marketplace add`, not by silent side-loading from a marketplace they already installed.

### 2. ADR + proposal required for any plugin entry

Before any change to `.claude-plugin/marketplace.json` (add / remove / modify a plugin entry), the author must open:

- A `docs/proposals/<bucket>/<slug>/proposal.yaml` reviewed via `/dev-kit:proposal`, AND
- An ADR (in this directory) capturing the scope decision, OR an explicit `Refs ADR-NNNN` link in the PR body.

A change that ships without both is rejected by the LLM-judge review gate (`/dev-kit:review`) on the basis of "missing design record".

### 3. Skill-prefix uniqueness guard

A regression test (`tests/test_marketplace_plugin_collision.py`) verifies that no two plugin entries in any `marketplace.json` under the dev-harness-kit tree share a skill-prefix namespace. The test derives each entry's effective skill prefix from its `name` field and the repo's `.claude-plugin/plugin.json` keywords; collision → fail.

The same test asserts that the **first** plugin entry's name equals the marketplace's `name` field (preventing the silent "marketplace says X but installs Y" drift that issue #783 surfaced in lite's own marketplace.json).

### 4. Branch protection on `main` is a precondition for this ADR

This policy relies on branch protection that did not exist when `e296ce6a` landed. The maintainer must enable, on `main`:

- Require pull request before merging
- Require signed commits (linear history)
- Require approvals: 1
- Include administrators (no self-approval on the rule)

Until that protection is live, this ADR is **advisory**; the regression test in §3 is the only automated gate.

### 5. Out of scope (intentionally)

- **Forks**: a fork can do whatever its owner wants; this ADR governs the dev-harness-kit *marketplace*, not every repo that happens to clone it.
- **External marketplaces**: third-party maintainers running their own `marketplace.json` are not bound by this ADR — they ship their own scope policy.
- **Sub-plugins within dev-kit**: a plugin can declare `commands:` and `skills:` subpaths that resolve to nested sub-plugins. This ADR does not constrain internal plugin layout; it constrains marketplace-level entries.

## Consequences

- **Positive**: any future attempt to register an external plugin from this repo is blocked at code-review time (the LLM judge sees no ADR) and at test time (the collision test fails on shared prefixes).
- **Positive**: the description-fabrication failure mode (`8/7/6` claimed, `15/0/0` on disk) cannot recur silently — every marketplace entry now points at a repo this project can audit.
- **Negative**: opt-in friction increases for any hypothetical lite / fork / experiment — but that's the point. The friction is the bug we paid for on 2026-09-01.
- **Negative**: this ADR does not retroactively recover PR #781 (`4a39a1de`), which the force-push orphaned. PR #782 (still open on the same topic) supersedes it; closing #782 explicitly as a "supersedes #781" replacement is part of the follow-up.

## Follow-ups

- [ ] Enable GitHub branch protection on `main` per §4 (maintainer action; cannot be done from a PR).
- [ ] Close PR #782 with body referencing #781's loss and this ADR.
- [ ] Backfill: reapply #781's fix if #782 does not cover it (TBD; pending #782 review verdict).
- [ ] After branch protection is live, drop the §4 advisory clause and replace with "policy enforced by repo settings".