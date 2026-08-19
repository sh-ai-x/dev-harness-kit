---
name: babysit-pr-local
category: ship
description: 0-arg local-mode PR babysitter. Pre-push pytest gate + local LLM judge verdict loop; replaces `gh pr checks --watch` with `bin/review-local.sh`.
when_to_use: |
  - User types /dev-kit:babysit-pr-local
  - GH-Actions minutes are exhausted on the active PR
  - Operator wants to iterate on review verdicts locally before pushing
allowed-tools: Read Bash Grep
disallowed-tools: WebFetch
model: sonnet
disable-model-invocation: false
user-invocable: true
alpha: state
argument-hint: ""
---
> [← Skills index](../../README.md)

# /dev-kit:babysit-pr-local — local-mode PR babysitter

## What it does

Monitors the current branch's PR and iteratively resolves every
blocker using a **local LLM judge** (`bin/review-local.sh`) instead of
GH-Actions, until the PR reaches local verdict = `Approve` and the
deterministic CI checks (`branch-policy`, `secret-scan`, `validate`)
pass. Each iteration is evidence-driven (MUST-L3): the skill quotes
the local audit comment + pytest tail line + combined-verdict line
before claiming a step is done.

Local-mode sibling of `/dev-kit:babysit-pr` (which still drives
iteration via GH-Actions). Both share `lib/babysit_pr_cli` helpers,
the lock protocol, and the worktree-detect plumbing; the algorithm
differs in five steps (see §Algorithm). Operators do not pass flags:
the local judge replaces CI's `review`/`security`/`maintenance` jobs
and the pre-push pytest gate is always on. `gh pr merge` is always a
human action — auto-merge is never invoked.

---

## Inputs (resolved at runtime, NOT user args)

| Variable         | Source                                                                 |
|------------------|------------------------------------------------------------------------|
| `PR_NUMBER`      | `gh pr view --json number` for the current branch                     |
| `PR_STATE`       | `gh pr view --json state` (`OPEN` required to proceed)                 |
| `REVIEW_VERDICT` | Local audit comment verdict from `bin/review-local.sh` (via exit code + `<!-- dev-kit-verdict-audit -->` line) |
| `CHECKS`         | `gh pr checks --json name,state,conclusion` (deterministic CI checks only) |
| `BRANCH`         | `git rev-parse --abbrev-ref HEAD`                                      |
| `MAX_ITERS`      | `1000` (matches `/dev-kit:babysit-pr`; the 3-consecutive-no-progress guard still triggers earlier) |
| `OPERATOR_HANDLE`| `gh api /user -q .login`                                               |
| `LOCAL_TEST_CMD` | `pytest -q` (the pre-push pytest gate default; MUST-L3 enforces the tail line) |

### Hidden flags (never appear in slash description)

The slash command is **0-arg** — operators run
`/dev-kit:babysit-pr-local` with no arguments. Three flags exist for
tests + the rare power-user override; all are suppressed from
`--help` and `argument-hint` so the operator never sees them.

| Flag | Effect | Why hidden |
|---|---|---|
| `--pr N` | Override current-branch PR discovery with explicit PR number | Tests + the rare case where the PR was opened from another worktree |
| `--local-test-cmd CMD` | Override the pre-push pytest gate default (`pytest -q`) | Non-pytest projects (Make, tox) need a real gate; defaulting to `pytest -q` would let a non-pytest project silently pass on `exit 0` with no pytest-tail line |
| `--local-mode` | Internal routing flag — already implied by the slash, kept so the parser doesn't double-route | Routes argv without double-parsing |

These are read by `lib/babysit_pr_cli.parse_babysit_args` (hidden via
`argparse.SUPPRESS`) and surfaced on the resulting Namespace as
`ns.pr`, `ns.local_test_cmd`, `ns.local_mode`. The pre-scan reader is
`lib/babysit_pr_cli.is_local_mode(argv)` for callers that need to
route before invoking the full parser.

If `--pr N` is absent and `PR_NUMBER` is empty, print a one-line
message and exit 1 explaining that an explicit `--pr N` or
current-branch PR is required. If the resolved `PR_STATE != OPEN`,
print a one-line message and exit 1. Never create a PR implicitly.

---

## Worktree-aware execution

The skill MUST run inside the worktree that owns the PR's branch —
edits from the main checkout are denied by `worktree-guard`. The
parent detects the session cwd via `hooks/lib/worktree-detect.sh`
and either runs in place or spawns a sub-agent in the resolved
worktree.

The lock file lives at `<worktree_path>/.dev-kit/babysit.lock`. It is
**shared** with `/dev-kit:babysit-pr` (same path) — if both skills
race on the same worktree, the second arrival sees a fresh lock and
refuses with `already running`. The
`lib/babysit_pr_reliability.is_stale_lock` helper handles SIGKILL /
OOM via the same 30-minute TTL. The lock body appends
`source=babysit-pr-local` so a post-mortem can tell which skill held
the lock; the parent skill writes the same prefix without `source=`.

Sub-agent delegation + parent-side preflight live in
[`recipes/canonical-wiring.md`](recipes/canonical-wiring.md).

---

## Algorithm

```
LOOP iter = 1 .. MAX_ITERS (=1000, BABYSIT_MAX_ITERS env-overridable):

### MUST — re-verify state immediately before acting

Every read of `gh pr view`, `gh pr checks`, `gh run view`, `gh api`,
or any other PR / workflow / status query is **racy**. The PR state,
check rollup, review verdict, and individual check conclusions can
change at any moment because of:

- a maintainer pushing / force-pushing a commit (fires `pull_request`
  → re-runs workflows → updates check conclusions)
- another operator running `gh workflow run ...` to dispatch workflows
- a queued workflow run starting or completing
- the local `bin/review-local.sh` itself finishing and posting a fresh
  audit comment mid-iteration

The local babysitter MUST re-query the relevant state — REVIEW_VERDICT
(read from the latest audit comment), CHECKS, the PR headRefOid, the
operator handle, anything else the next decision depends on — **immediately
before acting on it**. A value read at the start of an iteration or at
the top of a Claude turn MUST NOT be reused as "the current state"
several turns later; the operator or another automation may have changed
it in between.

Failure modes this rule prevents:

- claiming a PR is "approved" because the most recent local verdict was
  "Approve" at the start of the turn, after a re-run posted a fresh
  verdict that's actually "Changes Requested"
- diagnosing the wrong failing-check because a fresh `pull_request` run
  re-rendered the rollup with a different `databaseId` than the cached one
- running `gh run watch $REVIEW_RUN` against a stale run id because the
  run was force-cancelled and replaced while the babysitter was drafting
- missing a fresh linear-pr-sync SUCCESS that replaced a stale FAILURE
  entry in the rollup

The corollary: never act on a `gh pr view` / `gh pr checks` result
that was returned in a previous turn or by a previous tool call without
re-issuing the call. Cached responses from sub-agent handoffs and
parallel tool calls are especially dangerous — they always look fresh.

  1. SNAPSHOT   — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS via
                  `gh pr view` + `gh pr checks`. The local REVIEW_VERDICT
                  is read from the most recent
                  `<!-- dev-kit-verdict-audit -->` comment posted by
                  `bin/review-local.sh` (source=bin_review_local). The
                  local judge replaces the GH-Actions review verdicts;
                  CHECKS still reflects the deterministic CI jobs. Every
                  `gh` call in this step and the steps that follow is
                  itself subject to the MUST rule above; do not trust a
                  cached value.
  2. TERMINATE  — if REVIEW_VERDICT == "Approve" (local audit) AND
                  every check.conclusion ∈ {success, skipped, neutral}
                  → print "✅ PR #<n> approved — done (local)" + iterate
                  count → exit 0.
  3. CLASSIFY   — bucket blockers into:
                    A) CI failing      (deterministic check, e.g. secret-scan)
                    B) CI pending      (sleep 30s, continue)
                    C) Local Changes   (REVIEW_VERDICT == "Changes Requested"
                                         OR exit 1 from step 4L)
                    D) Local Blocked   (REVIEW_VERDICT == "Blocked")
                       → continue (the next step 4L iterates locally;
                         local mode never exits 0 on REVIEW_REQUIRED
                         because the local judge is the reviewer).
                  Note (contrast vs babysit-pr):
                    - REVIEW_REQUIRED (no audit comment yet) → continue,
                      not exit 0; the next step 4L runs the local judge
                      and the audit comment lands.
                    - REVIEW_REQUIRED after step 4L's first run → read
                      the audit comment and route to C/D/A accordingly.
  4. WAIT       — if any check pending and no failures, sleep 30s, continue.
  4L. LOCAL REVIEW — NEW STEP.
        a. Invoke `bin/babysit-pr-local.sh --pr $PR_NUMBER`.
           The wrapper validates args (refuses --auto-approve), then
           execs `bin/review-local.sh --pr $PR_NUMBER` with the
           provider-resolved env block + secret-scoped API key. The
           local judge runs /dev-kit:review + /dev-kit:security +
           /dev-kit:maintenance and emits:
             - exit 0 → Approve (loop terminates next iteration)
             - exit 1 → Changes Requested / Blocked / parse failure
                        (loop iterates; the audit comment is the
                         operator's "what to fix" record)
             - stdout last line: `combined verdict: <Word>`
                                (the MUST-L3 evidence quote)
             - audit comment posted:
                `<!-- dev-kit-verdict-audit -->` ... `verdict=$WORST`
                `review=$REVIEW_V security=$SECURITY_V
                 maintenance=$MAINTENANCE_V provider=$PROVIDER
                 source=bin_review_local`
        b. If exit 0 → goto 1 (the next TERMINATE check exits 0
           because the audit comment already says verdict=Approve,
           and the operator may still need to merge manually).
        c. If exit 1 → continue to step 5 with the verdict tagged in
           the iteration log (see step 11).
        d. Refusal detail: exit 2 means the wrapper rejected --auto-approve
           (operator error) — log + exit 1.
  5. FETCH LOGS — for each FAILING check (deterministic CI only):
                  `gh run view <run-id> --log-failed`; truncate to last
                  200 lines; capture exit code + first error.
                  Use `lib/babysit_pr_reliability.build_check_state` +
                  `diff_check_states` to skip checks whose databaseId
                  AND conclusion are unchanged since the prior iteration
                  (the prior diagnosis already accounts for them).
  6. DIAGNOSE   — per failing check in `changed`:
                    - test failure       → re-read test + source, write the fix
                    - lint/format        → run formatter, commit
                    - type-check         → fix types
                    - secret detected    → abort (NEVER auto-remove secrets; user must decide)
                    - local judge feedback →
                          open the most-recent
                          `<!-- dev-kit-verdict-audit -->` comment;
                          read the LLM judge's inline comments via
                          `gh pr view --comments`; apply the
                          reviewer-requested change.
  7. APPLY FIX  — modify code (Edit/Write). One logical change per iteration.
  7.5. LOCAL VERIFY — ALWAYS ON in local mode (no flag needed).
       DEFAULT cmd = `pytest -q` (overridable via hidden
       `--local-test-cmd CMD`). Calls
       `lib.babysit_pr_cli.run_local_verify(cmd=..., cwd=<worktree>)`,
       which enforces MUST-L3 via the pytest-tail regex
       (`<N> passed in <Ns>s` or `<N> failed in <Ns>s`).
       - passed=True → proceed to step 9.
       - passed=False (non-zero exit, missing tail line, timeout)
         → abort iteration BEFORE `git add` / `git commit` / `git push`.
  8. VERIFY LOCAL — HARD GATE, re-run the same failing command locally;
                    quote exit code + test count.
                    - Pass → proceed to step 9.
                    - Fail → return to step 6 with the new failure output
                      and retry within the SAME iteration (counts toward
                      the 3-consecutive-no-progress guard).
  9. COMMIT     — `git add <specific paths>` (NEVER `git add -p`) + conventional commit.
  10. PUSH     — `git push origin HEAD`. CI still runs the deterministic
                  checks (`branch-policy`, `secret-scan`, `validate`);
                  the local judge is the substitute for the LLM-judge
                  (`review` + `security` + `maintenance`) jobs.
  11. LOG     — append one line to `.dev-kit/babysit.log`:
                  `<ISO-8601> iter=<n> source=babysit-pr-local mode=local
                   review=<verdict> exit=<rc> branch=<headRefName>`
  12. SLEEP    — sleep 20s (give the local audit comment time to settle
                  before the next TERMINATE poll). NO `gh pr checks
                  --watch`; the local judge replaces the GH-Actions wait.
  13. SAVE STATE — overwrite `.dev-kit/babysit-checks.json` with
                  `build_check_state(CHECKS)` so the next iteration
                  diffs against it.
  14. INCREMENT — `iter = iter + 1`; if `iter > MAX_ITERS`, fall through
                  to the cap-fallback below.
```

If `iter == MAX_ITERS` and PR is still blocked → print the unresolved
blocker list, exit 1. Never silently retry past the cap.

### Step diff vs `/dev-kit:babysit-pr`

| Step | babysit-pr | babysit-pr-local |
|------|-----------|------------------|
| 3 (REVIEW_REQUIRED branch) | exit 0 (human gate) | continue (local judge is the reviewer) |
| 4 (WAIT) | sleep 30s for CI | same sleep 30s for deterministic CI |
| 4L (NEW) | n/a | `bin/babysit-pr-local.sh --pr N` → `bin/review-local.sh --pr N` |
| 5 (FETCH LOGS) | for failing CI checks | identical (deterministic CI still runs) |
| 7.5 (LOCAL VERIFY) | `--local-verify` opt-in, default OFF | always ON; default `pytest -q` |
| 11 (LOG) | `source=babysit-pr` | `source=babysit-pr-local mode=local review=<verdict>` |
| 12 (SLEEP) | `gh pr checks --watch` / 20s | 20s only (no `--watch`) |

---

## Safety valves

- **No `git push --force`** to `main`/`master`. Force-push to feature
  branches is allowed when the PR author == current user AND the
  branch is not protected.
- **No auto-merge, ever.** `gh pr merge` is always forbidden. The
  local judge posts the audit comment; the operator runs `gh pr merge`
  themselves. `bin/babysit-pr-local.sh` refuses `--auto-approve` at
  the wrapper layer; `bin/review-local.sh` also refuses the flag at
  the script layer; the audit comment records the run regardless.
- **No secret auto-removal**. If `secret-scan` flags a credential, the
  skill aborts with the file:line and exits 1.
- **No destructive git ops**: `reset --hard`, `clean -fd`, branch
  deletion forbidden.
- **One PR at a time**: refuse to run if a sibling process holds
  `<worktree>/.dev-kit/babysit.lock` (TTL via `is_stale_lock`).
- **Provider pre-resolved**: `bin/review-local.sh` reads
  `CI_REVIEW_PROVIDER` from env → `.env` via `lib/ci_setup.read_provider`
  and passes the matching `*_API_KEY` only to the `claude -p` invocation
  (no persistent shell env). Operators switch via
  `bin/set-provider.sh <p>`.

---

## Forbidden shortcuts

The following are **forbidden** (same contract as
`/dev-kit:babysit-pr`; bypassing any of them violates MUST-NO-SKIP):
skipping a failing test, making a failing required check optional,
closing the PR to dodge the LLM review, `|| true` / `|| echo skipped`
to mask a failure, marking an iteration "fixed" without the quoted
`local:` / `audit:` / `combine:` lines, or passing `--auto-approve`
to `bin/babysit-pr-local.sh` (refused with exit 2).

If the same failure recurs after **3 consecutive iterations** with no
progress, exit 1 with the unresolved blocker list. Do not silently
retry past the cap, lower the bar, or skip.

---

## Lock file protocol

On start (existing-lock safety net FIRST; then stamp-and-write):

```bash
mkdir -p .dev-kit
if [[ -f .dev-kit/babysit.lock ]]; then
  if python3 -c "
import sys, pathlib
sys.path.insert(0, 'lib')
import babysit_pr_reliability as bpr
sys.exit(0 if bpr.is_stale_lock('.dev-kit/babysit.lock') else 1)
" ; then
    echo "stale babysit.lock detected (TTL exceeded or pid gone); removing and proceeding"
    rm -f .dev-kit/babysit.lock
  else
    echo "already running: $(cat .dev-kit/babysit.lock)"
    exit 1
  fi
fi
echo "$(date -Iseconds) pid=$$ branch=$(git rev-parse --abbrev-ref HEAD) source=babysit-pr-local" \
  > .dev-kit/babysit.lock
trap 'rm -f .dev-kit/babysit.lock' EXIT
```

The lock is **shared** with `/dev-kit:babysit-pr`; either skill's
stale-lock detector accepts the other's marker (the `source=` field is
the disambiguator).

---

## Evidence template (per iteration, printed to stdout)

```
[babysit-pr-local] iter=3/10  review=Changes Requested  branch=feat/x
  audit:  <!-- dev-kit-verdict-audit --> ... verdict=Changes Requested
                review=Approve security=Approve maintenance=Changes Requested
                provider=minimax source=bin_review_local
  local:  pytest -q → 47 passed, 0 failed (exit 0)
  push:   7a3b2c1 → origin/feat/x
  combine: combined verdict: Changes Requested
  remaining: 0 ci pending; 1 ci passing (branch-policy)
```

A claim of "fixed" without the `local:` + `audit:` + `combine:` lines
violates MUST-L3.

---

## Hook alignment

- `stop-verify=ON` — every "done" claim must include a quoted exit code (L3).
- `secret-scan=ON` — hard-aborts on detected credentials.
- `slop-detector=ON` — blocks vacuous commits.
- `tdd-guard=OFF` — not applicable (PR babysitting, not authoring new tests).
- `bash-guard=ON` — guards `git push --force` patterns.
- `git-guard=ON` — hard-blocks `gh pr merge` (any invocation); merging into
  `main` is always a human action, run outside automation.
- `worktree-guard=ON` — denies Edit/Write from the main checkout.

---

## Output language

All stdout/stderr messages in **English only**.

---

## Minimal test fixture (manual)

```bash
# Mock gh + claude for offline regression; configure the verdict.
alias gh='python3 tests/fixtures/gh_mock.py'
export BABYSIT_STUB_EXIT=0  # 0=Approve / 1=Changes|Blocked

# Case 1: clean PR (local Approve) → exit 0 after 1 iter
# Case 2: red deterministic CI → 2 iters (fail → fix → pass)
/dev-kit:babysit-pr-local
```

Automated coverage:
- `tests/test_babysit_pr_local_cli.py` — parser + `is_local_mode` unit tests.
- `tests/test_babysit_pr_local_sh.py` — wrapper shell tests + exit-code propagation.
- `tests/test_skill_governance.py` — L6 `alpha: state` gate (existing).

---

## Next step

When the loop terminates with `✅ PR approved`, recommend
`/dev-kit:ship` to tag and release (the user still controls the
actual merge + tag push). On abnormal exit, recommend
`/dev-kit:evaluate` against the failing case + a manual patch via
`/dev-kit:build` or `/dev-kit:refactor`. For the
**GH-Actions-driven** sibling instead, use `/dev-kit:babysit-pr`
unchanged.

See [`recipes/canonical-wiring.md`](recipes/canonical-wiring.md) for
the parent-side preflight block + sub-agent prompt body.
