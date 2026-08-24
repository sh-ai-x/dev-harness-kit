---
name: ci-triage
category: audit
description: Triage failing GitHub Actions runs across recent commits, dedupe against a persisted case store, judge new failures against a model/context/harness taxonomy with a required repro + regression test, and record them without re-analyzing repeats.
alpha: enforcement
when_to_use:
  - User types /dev-kit:ci-triage
  - User asks "why does CI keep failing on main" / wants recent CI failures classified
  - After noticing a recurring red check and wanting a root-cause writeup instead of re-diagnosing it by hand each time
  - Before proposing a new hook to prevent a CI failure from recurring
allowed-tools: Read Write Bash AskUserQuestion
disallowed-tools: Edit Agent
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:ci-triage — CI Failure Triage

## Iron Law

**Never re-judge a failure that's already in the store.** Every failing run is reduced to a stable signature (workflow + failing job/step, or the run-level diagnostic for a zero-job run) before it is shown to the model. A signature already present in `.dev-kit/ci-triage-log.json` gets its occurrence list bumped and is skipped; only genuinely new signatures reach the judging step.

## What it does

> **One-shot convenience:** `python3 lib/ci_triage.py summary [--count N | --commits SHA... | --no-scan]` runs a fresh scan (bumping already-known signatures only) and prints the rendered report in a single call. Use this for the "just tell me what's in the store now" flow. No judging step happens here — judgment is still a separate `record --from-json` call (the script never invents judgment content).

1. **Ask scope** (does not default to a fixed count). Use `AskUserQuestion` or read explicit CLI-style args if the user already gave them: either "how many recent commits on the current branch" (a count) or an explicit list of commits/SHAs. Never hardcode a number — the right scope depends on what the user is chasing (a fresh regression vs. a historical sweep).
2. **Scan**: `python3 lib/ci_triage.py scan --count N` (or `--commits <sha...>`). This resolves each commit to its full 40-char SHA (`gh run list --commit` silently returns nothing on a short SHA — see `runs_for_commit` in `lib/ci_triage.py`), finds its linked workflow runs, and for every non-passing run fetches failure detail: the failing job/step's log via `gh run view --log-failed`, or — when GitHub scheduled zero jobs at all (e.g. a stale trigger registration) — the one-line `gh run view` diagnostic instead.
3. Each failure is hashed to a signature and checked against the store. The scan output separates:
   - `unjudged` — new signatures, each carrying the raw failure detail for the model to read.
   - `already_known` — signatures already judged; their occurrence count was bumped, nothing else happens.
   - `commits[].note` — commits with no direct run (most commonly a bot-authored commit pushed with `GITHUB_TOKEN`, which GitHub does not re-trigger; see the parent commit's run instead).
4. **Judge** each `unjudged` entry (this is the model's job, not the script's). The store schema is reproduction- and regression-prevention-shaped, not analysis-shaped — a write-up that can't be re-run or re-checked later doesn't count. Produce:
   - `primary_cause` / `secondary_cause` — classified against `lib/ci_triage.py:CAUSE_TAXONOMY` (validated, not free text). Three primary buckets:
     | Primary | When it applies |
     |---|---|
     | `model` | The agent had the right info/tools and still judged wrong (reasoning error, ignored instruction, hallucinated tool args, over-automated, mis-triggered search, failed to say "I don't know") |
     | `context` | The agent acted reasonably on wrong/missing/stale/conflicting information (retrieval miss, stale doc, bad memory injection, context-window loss, ambiguous tool output, conflicting sources) |
     | `harness` | The control system around the agent broke, not the agent itself (ambiguous tool schema, excessive permission, retry repeating the same mistake, evaluator misjudging, eval-env drift, stale/contaminated persisted state, incomplete trace, mis-tuned guardrail, timeout/rate limit) |
     A pure CI/Actions failure with no agent in the loop (like the cost-flag.yml example below) is `harness` by construction — there's no model reasoning step and no retrieved context to blame.
   - `evidence` — the specific log line / API field that proves the cause. A citation, not a narrative; if the evidence is inconclusive, say so instead of guessing.
   - `repro` — a concrete, re-runnable recipe (exact commands/inputs) that reproduces the failure right now. Someone must be able to run it later to confirm the fix actually closed the case.
   - `regression_test` — **required**. `path::test_name` of an executable test that fails before the fix and passes after. This is the point of the whole skill: a failure that isn't captured as a test doesn't stop recurring, it just stops getting noticed. If a runtime test is genuinely infeasible for this case, use the explicit escape hatch `"N/A: <reason>"` — never leave it empty.
   - `proposal` — the concrete fix.
   - `hook_proposal` (optional) — when the fix is something a hook could enforce so the failure structurally can't recur, describe it here. Do not create the hook file itself; propose it only (see `## Rules`). `regression_test` and `hook_proposal` are complementary, not substitutes: a test catches it in CI, a hook prevents it from happening at all.
5. **Record**: write the judgment to a temp JSON file `{id, primary_cause, secondary_cause, evidence, repro, regression_test, proposal, hook_proposal}` and run `python3 lib/ci_triage.py record --from-json <path>`. `record_judgment` rejects an unknown cause pair and an empty `repro`/`regression_test` before it flips the case to `status: open`.
6. **Report**: `python3 lib/ci_triage.py report` prints every case grouped by status. Present this to the user as the final output — new cases in full, already-known cases as a one-line "N new occurrences, see case <id>".
7. **Process (auto-resolve)**: `python3 lib/ci_triage.py --store .dev-kit/ci-triage-log.json process --auto-fix --verify-window 10`. Closes the loop from "judged" to "processed" without a human having to remember the flip. The engine:
   - Walks every `open` case.
   - When `--auto-fix` is set AND the case's `proposal` matches a known auto-fixable pattern (currently: `actions/workflows/<id>/disable` + `enable` — the canonical stale-trigger-registration fix), the engine runs the exact `gh api` commands and records them, plus the pre/post `{state, updated_at}` pair, into the case's `resolution` block.
   - When the proposal doesn't match a known pattern OR no commit landed on the workflow file since `first_seen.date`, the case still receives a `resolution` block (`method: manual` / `method: code-fix`) for forensic completeness — the auto-fix path is opt-in, the audit trail is not.
   - Re-scans the workflow's recent runs and counts failures newer than `resolution.fix_applied_at` (the cutoff prevents a long history of pre-fix failures from keeping a freshly-fixed case at `open`).
   - `fresh_failures == 0` → flips `status: open` → `status: processed` with `processed_at`, the full `resolution` record (commands_run, verify_pre/post, commit.sha/subject, pr.number/url when applicable), and a `post_fix_scan` summary. `fresh_failures > 0` → leaves the case at `open` with a `last_process_attempt` note so the next run picks it up. Already-processed cases are skipped (idempotent — the existing `processed_at` is preserved, no commands are re-run).

   This is the loop the user asked for: the same store that records the failure also records its resolution, with the commands that fixed it and the related PR/commit when the fix was a code change. No separate "mark as fixed" step, no chance of a case being judged but never closed.

## Example (grounded in this repo, 2026-07-30)

Scanning the 10 latest `main` commits found 5 occurrences of one signature: `cost-flag.yml` workflow runs triggered by `push` events failed with 0 scheduled jobs ("This run likely failed because of a workflow file issue"). Cross-checking `gh api repos/:owner/:repo/actions/workflows` showed the registration's `updated_at` (2026-07-14T18:11) predates the file's last commit (2026-07-16T15:32) — the file's `on:` block was edited to `pull_request`-only after GitHub's trigger-registration cache was last refreshed, so GitHub keeps queuing (and immediately failing) phantom `push` runs against a trigger the file no longer declares.

Judged as:

```json
{
  "id": "<signature>",
  "primary_cause": "harness",
  "secondary_cause": "state-contamination",
  "evidence": "gh api repos/:owner/:repo/actions/workflows: cost-flag.yml updated_at=2026-07-14T18:11:40+09:00, file last commit=2026-07-16T15:32:23+09:00 (registration older than the trigger edit)",
  "repro": "gh api repos/:owner/:repo/actions/workflows --jq '.workflows[] | select(.path|contains(\"cost-flag\"))'; compare .updated_at against `git log -1 --format=%cI -- .github/workflows/cost-flag.yml`",
  "regression_test": "N/A: this asserts GitHub's server-side trigger-registration cache, not repo state — no in-repo test can observe it. ci-doctor's `workflow triggers` diagnostic is the closest structural guard; extending it to flag a stale registration is the hook_proposal below.",
  "proposal": "gh api -X PUT repos/:owner/:repo/actions/workflows/312869658/disable then enable to force the trigger cache to re-read the current file",
  "hook_proposal": "extend /dev-kit:ci-doctor's workflow diagnostics to compare each workflow's `updated_at` against `git log -1 --format=%cI -- <file>` and WARN when the registration predates the last trigger-relevant edit"
}
```

That's one case, 5 occurrences, not 5 cases — this is exactly the dedup this skill exists to enforce.

After `process --auto-fix` runs, the same case ends up as:

```json
{
  "id": "1b71f09a5926",
  "status": "processed",
  "primary_cause": "harness",
  "secondary_cause": "state-contamination",
  "processed_at": "2026-07-31T11:16:38Z",
  "resolution": {
    "method": "api-toggle",
    "commands_run": [
      "gh api -X PUT repos/:owner/:repo/actions/workflows/312869658/disable",
      "gh api -X PUT repos/:owner/:repo/actions/workflows/312869658/enable"
    ],
    "verify_pre":  {"state": "active", "updated_at": "2026-07-31T20:15:12.000+09:00"},
    "verify_post": {"state": "active", "updated_at": "2026-07-31T20:16:35.000+09:00"},
    "fix_applied_at": "2026-07-31T11:16:35Z",
    "notes": "auto-applied stale workflow registration toggle"
  },
  "post_fix_scan": {"result": "clean", "fresh_failures": 0, "last_run_url": null}
}
```

The forensic trail stays in `.dev-kit/ci-triage-log.json`: which commands ran, the pre/post `updated_at` (proves the toggle actually refreshed the cache), and — for code-fix cases — the related commit SHA + PR URL so a later reader can jump from "what failed" to "what fixed it" without leaving the store.

## Rules

- **Read-only except its own store.** The store lives at `.dev-kit/ci-triage-log.json` (gitignored, like `.dev-kit/ci-config.json`). This skill never edits workflow files, never applies the `hook_proposal`, never pushes. `Edit` is disallowed in frontmatter for this reason — the store is written via `Write`/the CLI's own JSON write path, not by editing repo files. `process --auto-fix` is the one exception: it executes `gh api` calls (disable/enable) to apply a known-pattern fix, but only when the case's own `proposal` field names those exact commands — the engine never invents a fix that wasn't already judged by the model.
- **Full SHA only.** Any code path that calls `gh run list --commit` must resolve to the full 40-char SHA first. A short SHA does not error — it silently returns an empty run list, which looks identical to "no CI ran for this commit."
- **Dedup by signature, not by commit.** The store's unit of record is a failure signature; commits/runs are occurrences under it. Never write a fresh case for a signature that's already `open` or `processed`.
- **Reproduction-shaped, not analysis-shaped.** A case is not "done" because it has a good write-up. `repro` must be something a reader can re-run later to confirm the failure is (or isn't) still present; `regression_test` must name an actual executable test, or explicitly justify why none applies. `record_judgment` enforces both as required fields plus a `primary_cause`/`secondary_cause` pair validated against `CAUSE_TAXONOMY` — free-text classification is rejected.
- **No fabricated root cause.** `evidence` must cite the specific log line, API field, or timestamp comparison that supports the classification. If the evidence is inconclusive, say so — don't guess a cause to fill the field.
- **`process` is idempotent.** Already-processed cases are skipped without re-running their fix commands — the existing `processed_at` and `resolution` block are preserved verbatim. Running `process` repeatedly is safe and cheap.

## Files installed

| Path | Purpose |
|---|---|
| `skills/ci-triage/SKILL.md` | This file |
| `lib/ci_triage.py` | Deterministic engine: commit resolution, full-SHA run matching, failure-detail fetch, signature dedup, `CAUSE_TAXONOMY` validation, store I/O, and the `process` auto-resolve loop. Never generates the judgment content itself — `record_judgment` only validates and persists what the model supplies; `process` only applies fixes that match a known pattern named in the case's own `proposal` field. |
| `tests/test_ci_triage.py` | Signature stability, store round-trip, unjudged→open lifecycle, taxonomy/repro/regression-test validation, short-SHA guard, mocked end-to-end scan (two commits → one case), multi-job signal disambiguation, `##[error]`-preferred log extraction, the `process` lifecycle (auto-fix + clean verify → processed; fresh failure → still open; auto-fix disabled → manual method; already-processed → skipped), historical-failures-before-fix-don't-block-processed regression, and the processed-state report rendering. 35 tests, all passing. |

## Next step

For cases with a `hook_proposal`, hand off to the user for confirmation before creating the hook — this skill only proposes it. Once approved, the hook lives under `hooks/` per this repo's existing hook conventions (see `active-hooks.json` / `CLAUDE.md` §4). For already-processed cases, no next step is needed — the resolution record in `.dev-kit/ci-triage-log.json` is the closure.
