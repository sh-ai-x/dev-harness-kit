---
name: babysit-pr
category: ship
description: 0-arg PR babysitter. Polls `gh pr checks`, fetches failing run logs, applies minimal fixes, commits + pushes, and re-iterates until review verdict = Approve and all required checks pass. On MERGED, removes the local worktree + local branch + upstream branch + remote-tracking ref so the next session starts clean. Hard cap on iterations to prevent infinite loops.
when_to_use: |
  - User types /dev-kit:babysit-pr
  - PR is open on current branch and CI is red / review requested changes
  - User wants unattended iteration on a single PR
allowed-tools: Read Bash Grep
disallowed-tools: WebFetch
model: sonnet
disable-model-invocation: false
user-invocable: true
---

# /dev-kit:babysit-pr — PR babysitter loop

## What it does

Monitors the PR associated with the **current branch** and iteratively resolves
every blocker — failing CI checks and review change requests — until the PR
reaches `review verdict = Approve` and `all required checks = success`. Each
iteration is **evidence-driven** (MUST-L3): the skill quotes exit codes, log
snippets, and review verdicts before claiming a step is done.

Operates **only** on the PR of the current working branch. If no PR exists for
the branch, stop and tell the user to push + open one.

---

## Inputs (resolved at runtime, NOT user args)

| Variable         | Source                                                                 |
|------------------|------------------------------------------------------------------------|
| `PR_NUMBER`      | `gh pr view --json number -q .number` for the current branch          |
| `PR_STATE`       | `gh pr view --json state -q .state` (`OPEN` required to proceed)       |
| `REVIEW_VERDICT` | `gh pr view --json reviewDecision -q .reviewDecision` (`''`/`APPROVED`/`CHANGES_REQUESTED`/`REVIEW_REQUIRED`) |
| `CHECKS`         | `gh pr checks --json name,state,conclusion`                            |
| `BRANCH`         | `git rev-parse --abbrev-ref HEAD`                                      |
| `MAX_ITERS`      | `1000` (high cap; configurable via `BABYSIT_MAX_ITERS` env var; the 3-consecutive-no-progress stuck-loop guard still triggers earlier) |

If `PR_NUMBER` is empty OR `PR_STATE != OPEN` → print a one-line message and
exit 0. Never create a PR implicitly (MUST: explicit user action).

---

## Algorithm

```
LOOP iter = 1 .. MAX_ITERS:  (hard increment at end of body — see L82 fallback)
  1. SNAPSHOT   — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS (single gh call)
  2. TERMINATE  — if REVIEW_VERDICT == "APPROVED"
                    AND every check.conclusion ∈ {success, skipped, neutral}
                    → CONFIRM_MERGE — `gh pr view $PR_NUMBER --json state,mergedAt -q`
                        if `state != MERGED`: print "approve + checks green but PR not
                        merged (still draft? admin-only?). exiting 0 — human merges."
                        → exit 0
                    → CLEANUP — best-effort, idempotent, non-fatal:
                        1. local worktree → `git worktree remove` the dir this session
                           lives in (skip silently if `ExitWorktree` would error: another
                           live session owns it, or it was already removed).
                        2. local branch → `git branch -D $BRANCH` (force: squash-merge
                           leaves no ancestry, so `-d` refuses — see issue loop during
                           PR #187 babysit; `-D` is safe after `git diff origin/main HEAD`
                           is empty). Refuse silently if cwd is detached.
                        3. remote branch → `git push origin --delete $BRANCH`. Refuse
                           silently on 403 (branch protection / fork / offline).
                        4. remote-tracking ref → `git branch -dr origin/$BRANCH`.
                    → print "✅ PR #<n> approved, merged, and cleaned up — done"
                      + iterate count + 4-line summary of cleanup actions taken
                      (or "no-op" with reason for any skipped step)
                    → exit 0
  3. CLASSIFY   — bucket blockers into:
                    A) CI failing      (check.conclusion == "failure")
                    B) CI pending      (check.conclusion == null/pending) → wait
                    C) Review changes  (REVIEW_VERDICT == "CHANGES_REQUESTED")
                    D) Review required (REVIEW_VERDICT == "REVIEW_REQUIRED" or "")
                       → print "waiting for human review" + exit 0 (cannot self-approve)
  4. WAIT       — if any check is pending and no failures, sleep 30s, continue
  5. FETCH LOGS — for each failing check:
                    `gh run view <run-id> --log-failed`  (via checks databaseId)
                    truncate to last 200 lines; capture exit code + first error
  6. DIAGNOSE   — per failing check, identify ONE root cause:
                    - test failure       → re-read test + source, write the fix
                    - lint/format        → run formatter, commit
                    - type-check         → fix types
                    - secret detected    → abort (NEVER auto-remove secrets; user must decide)
                    - review feedback    → read review comments, apply reviewer-requested change
  7. APPLY FIX  — modify code (Edit/Write). One logical change per iteration.
  8. VERIFY LOCAL — re-run the same failing command locally; quote exit code + test count
  9. COMMIT     — `git add <specific paths>` of the file(s) just modified (NEVER `git add -p` — interactive, hangs without TTY; the skill runs unattended) + conventional commit
  10. PUSH     — `git push origin HEAD`
  11. LOG     — append one line to `.dev-kit/babysit.log`:
                  `<ISO-8601> iter=<n> check=<name> fix=<one-line> exit=<code>`
  12. SLEEP    — `gh pr checks --watch` or sleep 20s for CI to pick up
  13. INCREMENT — `iter = iter + 1`; if `iter > MAX_ITERS`, fall through to
                  the cap-fallback below; otherwise `goto 1`.
```

If `iter == MAX_ITERS` and PR is still blocked → print the unresolved blocker
list, exit 1. **Never** silently retry past the cap. (The earlier
3-consecutive-no-progress guard fires first when the loop is genuinely
stuck and avoids burning the full 1000-iter budget.)

---

## Safety valves

- **No `git push --force`** to `main`/`master`. Force-push to feature branches is
  allowed when the PR is your own (PR author == current user) AND the branch is
  not protected.
- **No auto-merge**. `gh pr merge` is forbidden. The user merges.
- **No secret auto-removal**. If `secret-scan` or any check flags a credential,
  the skill aborts with the file:line and exits 1.
- **No destructive git operations**: no `reset --hard`, no `clean -fd`, no
  branch deletion.
- **One PR at a time**: refuse to run if there is already a babysit process
  (lock file at `.dev-kit/babysit.lock`).
- **No worktree juggling** (per worktree hygiene in project memory).

### NO-SKIP / NO-WORKAROUND IRON LAW (MUST-NO-SKIP)

The following are **forbidden** as a means of getting the PR to green:

- **NEVER skip a failing test** to make CI pass. `pytest.skip`, `@unittest.skip`,
  removing a test from the suite, or commenting out an assertion are forbidden.
  If a test fails, the code under test must be fixed — the test reflects the
  contract and changing the test means the contract is now wrong.
- **NEVER skip a failing required check** by marking it optional, deleting it
  from the workflow file, or making it `continue-on-error: true`. If a check
  exists, it must pass.
- **NEVER skip the LLM review** by:
  - closing the PR (`gh pr close` is forbidden; only the user closes),
  - removing the review workflow trigger,
  - changing `pull_request` / `pull_request_target` to a no-op,
  - force-merging without the gate passing,
  - marking the review check as optional in branch protection.
  The LLM review is the project's review contract; bypassing it is a
  contract violation.
- **NEVER work around a failure with a workaround**. The failure must be
  **fixed at its root cause**, not masked. Examples of forbidden workarounds:
  - `|| true` / `|| echo skipped` on a step that exists to fail loudly
  - raising exit thresholds to make a flaky step pass
  - widening a regex until it accepts bad input
  - adding `|| exit 0` to silence a gate that should hard-fail
  - rewriting a hard-fail gate to default-to-Approve to hide skipped reviews
  - disabling a hook instead of fixing the violation
- **NEVER mark an iteration "fixed" without quoting the local verification**
  (`local:  <command> → <result> (exit <code>)`). A "fixed" claim without the
  evidence line violates MUST-L3 and the no-workaround iron law (MUST-NO-SKIP).

If the same failure recurs after **3 consecutive iterations** with no
progress, exit 1 and print the unresolved blocker list with quoted log
snippets — do not silently retry past the cap, do not lower the bar, do
not skip.

---

## Lock file protocol

On start:
```bash
mkdir -p .dev-kit
[[ -f .dev-kit/babysit.lock ]] && { echo "already running: $(cat .dev-kit/babysit.lock)"; exit 1; }
echo "$(date -Iseconds) pid=$$ branch=$(git rev-parse --abbrev-ref HEAD)" > .dev-kit/babysit.lock
trap 'rm -f .dev-kit/babysit.lock' EXIT
```

---

## Evidence template (per iteration, printed to stdout)

```
[babysit] iter=3/10  check=pytest  verdict=FAIL  branch=feat/x
  log:    ...AssertionError at tests/test_x.py:42...
  fix:    corrected off-by-one in lib/x.py:18
  local:  pytest → 47 passed, 0 failed (exit 0)
  push:   7a3b2c1 → origin/feat/x
  review: APPROVED (awaiting final CI)
  remaining: 1 check pending (deploy-preview)
```

A claim of "fixed" without the `local:` line violates MUST-L3 (evidence-before-done).

---

## Hook alignment

- `stop-verify=ON` — every "done" claim must include a quoted exit code (L3).
- `secret-scan=ON` — hard-aborts on detected credentials (see safety valve).
- `slop-detector=ON` — blocks vacuous commits ("fix ci", "wip") that contain no
  functional change.
- `tdd-guard=OFF` — not applicable (PR babysitting, not authoring new tests).
- `bash-guard=ON` — guards `git push --force` and `gh pr merge` patterns.

---

## Output language

All stdout/stderr messages in **English only**.

---

## Next step

When the loop terminates with `✅ PR approved`, recommend `/dev-kit:ship` to
tag and release (the user still controls the actual merge + tag push). On
abnormal exit, recommend `/dev-kit:repair` if the failure looks like a golden
asset regression.

---

## Minimal test fixture (manual)

```bash
# Mock the gh CLI for offline regression
alias gh='python3 tests/fixtures/gh_mock.py'

# Case 1: clean PR → exit 0 after 1 iter
/dev-kit:babysit-pr

# Case 2: red CI → 2 iters (fail → fix → pass)
# Case 3: review CHANGES_REQUESTED → read comment, fix, re-push
# Case 4: REVIEW_REQUIRED → exit 0 immediately (human gate)
# Case 5: secret detected → exit 1, no commit
```

Automated fixture lives at `tests/fixtures/babysit-pr/` (added in follow-up PR
once the loop is stable — see `tests/test_smoke.py` for the harness contract).
