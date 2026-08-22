# Hook coverage gaps -- P4 Bucket B audit

> Generated as part of issue #264 (Bucket B investment). This doc is a
> deliberate counterpart to the parent thread's recent commits #259
> (provider source -- vars.CI_REVIEW_PROVIDER) and #265 (provider sink
> -- .env:CI_REVIEW_PROVIDER). Gap #8 below is restated in light of
> those changes; the rest of the matrix is unchanged.
>
> Each row maps a real-world scenario to the hook(s) that gate it today.
> The right-most column flags the gap; the summary at the bottom picks
> the top 3 to close in this PR.

Severity scale (severity = residual blast radius when the rule fires
wrong OR fails to fire):

- **HIGH** -- user can ship a broken PR / lose data / bypass a contract.
- **MEDIUM** -- workaround exists but the hook should have caught it.
- **LOW** -- quality-of-life fix; rule already has a backstop elsewhere.

## Scenarios x hooks

| # | Scenario | Existing hook(s) | Gap | Severity | Proposed fix |
|---|----------|------------------|-----|----------|---------------|
| 1 | User starts a new task in the main checkout (Claude) | worktree-guard.sh (hard block) | resolved by ACP M-tier (PR-2) | -- | -- |
| 2 | User starts a new task in the main checkout (Codex) | same hooks, routed via .codex-plugin/hooks/hooks.json | partial -- see #4 | MEDIUM | wire parity, see below |
| 3 | Edit/Write inside main checkout | worktree-guard.sh | none | -- | -- |
| 4 | Commit + push non-main branch | git-guard.sh + review-yml-isolation.sh (Claude only) | **MISSING in Codex runtime** -- .codex-plugin/hooks/hooks.json does NOT register review-yml-isolation.sh, so a Codex sub-agent can land review.yml + unrelated edits in the same commit. The CI gate verdict becomes unreadable. | **HIGH** | Mirror the review-yml-isolation.sh entry into .codex-plugin/hooks/hooks.json (PreToolUse::Bash). Single-entry add. |
| 5 | force-push a feature branch | bash-guard.sh (advisory) + git-guard.sh (always-on for -f / --force) | partial -- force-with-lease allowed per spec | -- | -- |
| 6 | SessionStart inside main checkout | session-start-check.sh | none | -- | -- |
| 7 | SessionStart in a fresh worktree without dev-kit hooks | log-on-session-start.sh | none | -- | -- |
| 8 | User .env:CI_REVIEW_PROVIDER is off the allowlist OR diverges from .env.example default | nothing | **MISSING** -- bin/set-provider.sh refuses off-list writes (T4) but a hand-edited .env with `CI_REVIEW_PROVIDER=openai` slips through silently. The CI review workflow then dispatches with the wrong / no provider. Companion gap: when the local value drifts from .env.example (a tracked template that documents the repo-wide default), there is no per-session reminder. | MEDIUM | New SessionStart hook hooks/provider-divergence-check.sh that emits additionalContext when (a) .env CI_REVIEW_PROVIDER is off-list OR (b) on-list but != .env.example. No mutation, no commit. (Original gap target referenced the now-deleted .github/ci-review-provider.txt; this row re-states for the post-#265 contract.) |
| 9 | Write contains credential pattern | secret-scan.sh (PostToolUse, advisory) | none (intentional) | -- | -- |
| 10 | Write contains LLM-tell | slop-detector.sh (PostToolUse, advisory) | none (intentional) | -- | -- |
| 11 | Babysit-pr loop runs while a stale babysit.lock is on disk | nothing checks TTL/PID | **MISSING** -- SKILL.md lock-file protocol only checks `[ -f .dev-kit/babysit.lock ]`. SIGKILL / OOM / network-partition leaves the lock forever and every future babysit-pr exits 1 with "already running". | MEDIUM | Ship lib/babysit_pr_reliability.py::is_stale_lock(path, ttl_seconds=1800). SKILL.md recovery text references the helper. |
| 12 | Babysit-pr waits forever on a ghost workflow check (server-side workflow deleted, check stays null/pending) | nothing classifies ghosts | **MISSING** -- gh pr checks returns the check with conclusion=null + state=pending long after the underlying workflow was removed. The babysit-pr wait loop (Algorithm step 4) sleeps and retries until MAX_ITERS -- pure busy-loop with no early exit. | MEDIUM | Ship lib/babysit_pr_reliability.py::classify_check(check, now_epoch) returning "ghost" when the check has been pending beyond the threshold OR has no databaseId (GitHub's prune signal). SKILL.md describes the classification + recovery path. |
| 13 | Stop-hook completion claim with no exit code | stop-verify.sh | none | -- | -- |
| 14 | git worktree add auto-cuts but no tools/save_log.py in new tree | worktree-log-auto-install.sh | none | -- | -- |

## Top 3 to close (this PR)

### 1. #4 -- Review.yml isolation missing in Codex runtime.

Real dual-runtime hole: the rule that review.yml edits must be
commit-only is enforceable by both clients because both read the same
git tree, but the hook is only wired on Claude. A Codex babysit-pr run
that fixes a failing review.yml test will silently bundle unrelated
edits.

**Fix**: mirror the entry into .codex-plugin/hooks/hooks.json. The
hook script (hooks/review-yml-isolation.sh) already exists; only the
wiring is missing. Regression test extension in
tests/test_hooks_status.py -- the existing
test_codex_manifest_registers_shared_hook_definition now also asserts
that the Codex PreToolUse::Bash inventory contains
review-yml-isolation.sh.

### 2. #8 -- .env provider off-list / divergence has no surface.

The current allowlist is enforced at the set-provider.sh write path
(T4 test_set_provider.py). But (a) a manual edit to .env (or a copy
from an outdated .env.example) can place a non-allowlist value there
without anyone noticing until CI fails, and (b) the tracked template
.env.example:CI_REVIEW_PROVIDER documents the repo default but no hook
reminds an operator that their local value drifts from it. Both are
silent failure paths today.

**Fix**: new SessionStart hook
hooks/provider-divergence-check.sh that (i) reads .env and .env.example
via the same parser as bin/set-provider.sh, (ii) validates the local
value against the allowlist, and (iii) compares against the .env.example
default. Either mismatch emits a SessionStart additionalContext --
never mutates either file. Regression tests in
tests/test_provider_divergence_hook.py (failing-before the hook,
passing-after) and tests/test_provider_divergence_wiring.py (asserts
the hook is registered in BOTH .claude-plugin/hooks/hooks.json and
.codex-plugin/hooks/hooks.json, the dual-runtime contract enforced for
review-yml-isolation in #1 above).

### 3. #11/#12 -- Babysit-pr stale-lock + ghost-workflow classification.

Two reliability gaps in one helper module
(lib/babysit_pr_reliability.py). SKILL.md gains explicit recovery
text + references the helper.

**Fix**: pure-function helpers, deterministic, no I/O time-of-day
randomness (callers pass `now_epoch` so tests are reproducible):

- is_stale_lock(path, ttl_seconds=1800) -> bool
  True when mtime > TTL ago, OR the parsed pid= field no longer
  refers to a running process (Linux /proc scan; macOS kill(0) probe).

- classify_check(check_dict, now_epoch, ghost_threshold_seconds=300) -> str
  Returns one of {approved, failing, pending, ghost}.
  Ghost is when conclusion is None AND databaseId is missing OR the
  check's startedAt/updatedAt is older than the threshold.

Regression: tests/test_babysit_pr_reliability.py with synthesized
inputs for both helpers. Fails before the helper ships (ImportError);
passes after.

## Out of scope for this PR (deferred)

- Server-side enforcement of review-yml isolation via protected branch
  rulesets (operational concern, lives in repo Settings).
- Slop / secret strict-mode UX (separate workstream; both hooks exist
  in advisory mode today by design).
- Hook for force-push to a branch you do not own (cross-cutting;
  separate security audit workstream).
- Per-session reminder when .env has no CI_REVIEW_PROVIDER at all --
  noisy on first clone; deferred to a setup hint.

## Closed by followup — lifecycle producer wiring (issue #702)

`hooks/trace-session-end.sh` (added by `fix/event-coverage-observable`,
issue #702) closes the producer-half of the measurement-integrity
coverage gap: every Claude Code / Codex session now emits a matched
`step.started` (in `hooks/session-start-check.sh:71-81`, BEFORE the
`case "$WORKTREE_DETECT"` short-circuit so it fires on main + worktree
+ outside-repo) and a `step.completed` (in `hooks/trace-session-end.sh`,
on `Stop` + `SessionEnd` with an idempotency guard so multi-turn
re-fires do not duplicate the terminal event).

Wired into all four manifests — `hooks/hooks.json` (plugin),
`.claude/settings.json` (Claude runtime SessionEnd + Stop),
`.codex/hooks.json` (Codex Stop), `.codex-plugin/hooks/hooks.json`
(Codex plugin mirror). Sibling-hook additions, no existing hooks
touched; portability parity with the Codex mirror is enforced by
`tools/portability_check.py` (hard contract — `test_portability_loop.py`
regression).

The `_subject_observability` submetric in `lib/harness_effectiveness.py`
symmetric-ratios `|started ∩ terminal| / |started ∪ terminal|` so
empty worktrees report a meaningful `measurement_integrity.score` with
an actionable finding string (missing producer / orphan started /
partial coverage) instead of collapsing to `INSUFFICIENT_EVIDENCE`.
`schema_version` bumps 2 → 3 (issue #663 precedent for the
nested-submetric pattern).
