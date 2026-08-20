---
name: babysit-pr
category: ship
description: 0-arg PR babysitter. Polls `gh pr checks`, fetches failing run logs, applies minimal fixes, commits + pushes, and re-iterates until review verdict = Approve and all required checks pass. Hard cap on iterations to prevent infinite loops.
alpha: state
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
> [← Skills index](../../README.md)

# /dev-kit:babysit-pr — PR babysitter loop

## What it does

Monitors the PR associated with the **current branch** and iteratively resolves
every blocker — failing CI checks and review change requests — until the PR
reaches `review verdict = Approve` and `all required checks = success`. Each
iteration is **evidence-driven** (MUST-L3): the skill quotes exit codes, log
snippets, and review verdicts before claiming a step is done.

This is the single user-facing repair entrypoint. GitHub's `auto-fix-pr` workflow
is only an event adapter; it must use the same repair state and must never create
a competing repair loop.

`CONVERSATION_PR` is set only when the user states a literal PR number or the
immediately preceding assistant tool result returned a PR number from PR
creation/listing. Phrases such as "babysit the latest PR" or "babysit the one I
just made" without a number do not establish a handoff; the operator must use
`--pr N` or restate the number.

By default, monitors the PR associated with the current working branch. When
the session is in the main checkout and the conversation explicitly identifies
one PR (for example, the PR just created in the preceding turn), that PR is a
conversation handoff candidate and may be used as the target. The skill must
re-verify that candidate with fresh `gh pr view` data before acting. Pass
`--pr N` to target an explicit open PR; this remains the strongest override.

---

## Inputs (resolved at runtime, NOT user args)

| Variable         | Source                                                                 |
|------------------|------------------------------------------------------------------------|
| `PR_NUMBER`      | `gh pr view --json number -q .number` for the current branch          |
| `CONVERSATION_PR`| The explicit PR number established by the current conversation, if any; treated like `--pr N` after fresh validation |
| `PR_STATE`       | `gh pr view --json state -q .state` (`OPEN` required to proceed)       |
| `REVIEW_VERDICT` | `gh pr view --json reviewDecision -q .reviewDecision` (`''`/`APPROVED`/`CHANGES_REQUESTED`/`REVIEW_REQUIRED`) — re-issue immediately before acting (see MUST rule above) |
| `CHECKS`         | `gh pr checks --json name,state,conclusion` — re-issue immediately before acting (see MUST rule above) |
| `BRANCH`         | `git rev-parse --abbrev-ref HEAD`                                      |
| `MAX_ITERS`      | `1000` watchdog (configurable via `BABYSIT_MAX_ITERS`; not an approval timeout or normal completion condition; durable state is saved before fallback) |
| `OPERATOR_HANDLE`| `gh api /user -q .login` (the human running the babysitter)           |
| `CODEOWNERS_PATH`| `$REPO_ROOT/.github/CODEOWNERS` (parsed by `lib/babysit_pr_cli.py`)    |
| `COLLABORATORS`  | `gh api /repos/{owner}/{repo}/collaborators?per_page=100 -q '.[].login'` |

### CLI flags (issues #324, #527)

```
/dev-kit:babysit-pr [--pr N]
                    [--operator-is-only-human] [--rationale "<text>"]
                    [--local-verify [--local-test-cmd "<cmd>"]]
```

| Flag                       | Effect |
|----------------------------|--------|
| *(no flag)*                | Default behavior: print `REVIEW_REQUIRED -> human-gate` and exit 0. The flag-absent path is the audit-safe default — operators never accidentally bypass review. |
| `--pr N` | Babysit explicit PR `N`, overriding current-branch PR discovery. The target must be open; use this when the current branch's PR is closed or merged. |
| `--operator-is-only-human` | Opt-out for single-operator repos. Refuses with exit 1 if `CODEOWNERS_PATH` OR `COLLABORATORS` list any handle other than `OPERATOR_HANDLE`. Requires `--rationale`. Posts the audit comment `/ownership-confirmed by operator=<handle> at <ISO-8601>; rationale=<text>` and hands off — it never merges the PR. Auto-merge into `main` is disabled by policy; the human operator runs `gh pr merge` themselves. |
| `--rationale "<text>"`     | Required when `--operator-is-only-human` is set; quoted verbatim into the audit comment. The flag pair is the *only* canonical way to bypass the human-review gate. |
| `--local-verify`           | **Optional additive flag** (default behavior unchanged when absent). After §Algorithm step 7 (APPLY FIX) and **before** step 9 (COMMIT + PUSH), run `--local-test-cmd` (default `pytest -q`) inside the worktree. If the test command exits non-zero, abort the iteration **before** `git add` / `git commit` / `git push` — no commit, no push, no GH-Actions run consumed. The §Algorithm step 8 (VERIFY LOCAL — re-run the specific failing check) is preserved alongside; this flag adds a *broader* pre-commit check, not a replacement. Use when GH-Actions minutes are tight and the operator wants to gate iteration on local test passage without burning CI on a known-failing commit. |
| `--local-test-cmd "<cmd>"` | Shell command for `--local-verify` to run inside the worktree. Defaults to `pytest -q`. The command's stdout+stderr MUST include a pytest-style tail line (`<N> passed in <Ns>s` or `<N> failed in <Ns>s`) per MUST-L3; if the quoted line is missing, the iteration refuses to flip to "ready to push". |

Target precedence is: `--pr N` → an explicitly identified `CONVERSATION_PR` →
the current branch's PR → main-checkout candidate resolution. A conversation
handoff is valid only when the number was explicitly stated or returned by the
immediately preceding PR-creation step; never infer it from the newest PR,
branch timestamps, or an unrelated issue number. Re-issue `gh pr view` and
verify the PR is open before entering the loop. If no target is established,
print a one-line message and exit 1 explaining that an explicit `--pr N`,
conversation PR, or current-branch PR is required.
If the resolved `PR_STATE != OPEN`, print a one-line message and exit 1; do
not silently report success. Never create a PR implicitly.

---

## Worktree-aware execution

The babysitter MUST run inside the worktree that owns the PR's branch —
running edits from the main checkout is denied by `worktree-guard`. The
parent detects the session cwd and either runs in place or spawns a
sub-agent in the resolved worktree.

### Detect cwd

Source `hooks/lib/worktree-detect.sh` (single source of truth — do NOT
re-implement the `--git-dir == --git-common-dir` discriminator):

```bash
source hooks/lib/worktree-detect.sh
worktree_detect          # sets $WORKTREE_DETECT ∈ {"worktree","main","outside"}
```

If `$WORKTREE_DETECT == "outside"` → print `not in a git repo; nothing to babysit`
and exit 0.

### Scenario A — cwd is a worktree

The skill proceeds with the Algorithm section below unchanged: the
session cwd already is the PR's owning worktree.

### Scenario B — cwd is the main checkout

The parent runs these steps in order, BEFORE spawning the sub-agent.

1. `git fetch origin` — refresh remote refs.
2. Validate a conversation handoff before candidate enumeration:
   ```bash
   if [[ -n "${CONVERSATION_PR:-}" ]]; then
     CONVERSATION_SNAPSHOT=$(gh pr view "$CONVERSATION_PR" \
       --json number,state,headRefName,headRefOid -q .)
     CONVERSATION_STATE=$(printf '%s' "$CONVERSATION_SNAPSHOT" \
       | jq -r '.state // empty')
     if [[ "$CONVERSATION_STATE" != "OPEN" ]]; then
       echo "CONVERSATION_PR=#$CONVERSATION_PR is not open; refusing to babysit" >&2
       exit 1
     fi
   fi
   ```
   A validated `CONVERSATION_PR` is authoritative and goes directly to
   worktree resolution; do not replace it with a target selected by count,
   recency, branch name, worktree mtime, or PR number.
3. If no conversation handoff, list candidate PRs off main:
   ```bash
   gh pr list --state open --json number,headRefName,headRefOid,title \
     --jq '.[] | select(.headRefName != "main")'
   ```
4. Zero candidates → print `no open PR off main; nothing to babysit` and exit 0
   (preserves the existing "no PR → exit 0" contract).
5. Multiple candidates without a conversation handoff → print a numbered list
   `number | headRefName | headRefOid | title` and exit 0. Never auto-pick.
6. Exactly one candidate, or a validated conversation handoff → resolve its
   owning worktree:
   ```bash
   git worktree list --porcelain \
     | awk '/^worktree /{wt=$2; next} /^HEAD [0-9a-f]/{print wt, $2}'
   ```
   Match the line whose second field equals `<pr>.headRefOid`.
7. If no local worktree owns the target branch → create one and verify:
   ```bash
   git worktree add -b <headRefName> .worktrees/<headRefName> origin/<headRefName>
   cd .worktrees/<headRefName>
   [[ "$(git rev-parse HEAD)" == "<headRefOid>" ]] \
     || { echo "HEAD mismatch after worktree add"; exit 1; }
   ```
   (the literal `origin/<headRefName>` above is the remote-tracking ref)
8. If a local worktree owns the branch → use its existing path.
9. `cd <worktree_path>` once. The parent's Bash cwd persists for the rest of
   the parent's session, so the resolved worktree is now the parent cwd.

### Sub-agent delegation

Spawn the sub-agent via the `Agent` tool with `subagent_type: "general-purpose"`.
The sub-agent inherits the parent's cd'd cwd as its working directory and runs
the same §Algorithm as the parent (single source of truth — see below).
The only per-invocation differences are: (a) the agent's working directory
stays pinned to `<worktree_path>` via the parent's `cd`, and (b) the agent
inherits this skill's Iron Laws (L1–L5) and the safety valves from the
parent body.

### Lock file isolation

The lock file for the babysitter loop lives at
`<worktree_path>/.dev-kit/babysit.lock`. The parent's cd to the worktree
ensures the parent lock and the sub-agent lock share the same path; if the
parent did NOT cd first, the sub-agent would write its lock to the main
checkout's `.dev-kit/` and collide with sibling processes there.

### Why this design

The parent resolves the worktree first because the `worktree-guard` hook
denies Edit/Write in the main checkout — a sub-agent spawned before
resolution would either fail closed on its first edit or silently run in
the wrong working tree. Prepping the worktree in the parent keeps the
spawn deterministic (one worktree path, one branch, one lock file) and
matches the `git-workflow` rule that every change-set lives in its own
worktree from creation through merge.

---

## Algorithm

```
LOOP iter = 1 .. MAX_ITERS:  (hard increment at end of body — see L82 fallback)
  0. OPT-OUT CHECK — if --operator-is-only-human is on argv, defer to the
                    bypass helper BEFORE entering the CLASSIFY loop. See
                    §Canonical wiring for the full Python block and the
                    helper's exit-code → orchestrator-exit mapping.
                    If the helper returned EXIT_RATIONALE_REQUIRED
                    (2), print the helper's stderr and EXIT 2 so the
                    operator adds a non-empty --rationale and retries.
                    If no --operator-is-only-human was passed,
                    continue to step 1 (default human-gate path).

                    IMPORTANT: do NOT use `sys.exit(rc)` here. The
                    helper's EXIT_MULTI_OWNER (1) means "bypass
                    refused, fall back to human-gate" -- it is a
                    successful outcome of the bypass decision, NOT a
                    failure of the skill. Mapping it 1:1 to a
                    process exit code would surface a fake failure
                    to CI for a refusal that the algorithm step 3D
                    treats as a normal "REVIEW_REQUIRED -> exit 0"
                    hand-off. The correct mapping is:

                      if rc == bpc.EXIT_OK:
                          sys.exit(0)
                      elif rc == bpc.EXIT_MULTI_OWNER:
                          sys.exit(0)   # human-gate fallback
                      elif rc == bpc.EXIT_OWNERSHIP_UNKNOWN:
                          sys.exit(0)   # human-gate fallback
                      elif rc == bpc.EXIT_RATIONALE_REQUIRED:
                          sys.exit(2)
```

### MUST — re-verify state immediately before acting

Every read of `gh pr view`, `gh pr checks`, `gh run view`, `gh api`,
or any other PR / workflow / status query is **racy**. The PR state,
check rollup, review verdict, and individual check conclusions can
change at any moment because of:

- a maintainer pushing / force-pushing a commit (fires `pull_request`
  → re-runs workflows → updates check conclusions)
- another operator running `gh workflow run ...` to dispatch workflows
- a queued workflow run starting or completing
- a bot reviewer (claude[bot] / github-actions[bot]) posting a new review

The babysitter MUST re-query the relevant state — `REVIEW_VERDICT`,
`CHECKS`, the failing-check's `run-id`, the PR `headRefOid`, the
author-association, anything else the next decision depends on —
**immediately before acting on it**. A value read at the start of an
iteration or at the top of a Claude turn MUST NOT be reused as
"the current state" several turns later; the operator or another
automation may have changed it in between.

Failure modes this rule prevents:

- claiming a PR is "approved" because REVIEW_VERDICT was APPROVED when
  the babysitter started, after a new reviewer pushed CHANGES_REQUESTED
- diagnosing the wrong failing-check run because a fresh `pull_request`
  run re-rendered the rollup with a different `databaseId` than the
  one cached from a previous iteration
- posting a status against a stale SHA because the branch was force-pushed
  while the babysitter was drafting its comment
- missing a fresh linear-pr-sync SUCCESS that replaced a stale FAILURE
  entry in the rollup

The corollary: never act on a `gh pr view` / `gh pr checks` result
that was returned in a previous turn or by a previous tool call without
re-issuing the call. Cached responses from sub-agent handoffs and
parallel tool calls are especially dangerous — they always look fresh.

```
  1. SNAPSHOT   — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS (single gh call).
                    Load the prior iteration's check-state cache from
                    `.dev-kit/babysit-checks.json` (absent on iter 1 —
                    treat as `{}`). Diff it against the fresh CHECKS via
                    `lib/babysit_pr_reliability.py::diff_check_states(prev, curr)`
                    (see §Check-state caching below). The `changed` /
                    `unchanged` split feeds step 5. Every `gh` call in
                    this step and the steps that follow is itself subject
                    to the MUST rule above; do not trust a cached value.
  2. TERMINATE  — if REVIEW_VERDICT == "APPROVED"
                    AND every check.conclusion ∈ {success, skipped, neutral}
                    → print "✅ PR #<n> approved — done" + iterate count
                    → exit 0
  3. CLASSIFY   — bucket blockers into:
                    A) CI failing      (check.conclusion == "failure")
                    B) CI pending      (check.conclusion == null/pending) → wait
                    C) Review changes  (REVIEW_VERDICT == "CHANGES_REQUESTED")
                    D) Review required (REVIEW_VERDICT == "REVIEW_REQUIRED" or "")
                       → persist WAIT_FOR_APPROVAL and wait/resume (cannot self-approve)
  4. WAIT       — if any check is pending and no failures, persist WAIT_FOR_CHECKS,
                  sleep/reconcile, and continue; if checks are green but review
                  is required, persist WAIT_FOR_APPROVAL and continue until a
                  fresh review event or approval arrives.
  5. FETCH LOGS — for each failing check IN `changed` (per step 1's diff):
                    `gh run view <run-id> --log-failed`  (via checks databaseId)
                    truncate to last 200 lines; capture exit code + first error.
                    For a failing check in `unchanged`, its log is byte-
                    identical to what a prior iteration already diagnosed
                    and fixed-for — re-fetching wastes a network round-trip
                    for no new information. Reuse the cached diagnosis from
                    `.dev-kit/babysit.log` instead (see §Check-state caching).
  6. DIAGNOSE   — per failing check IN `changed`, identify ONE root cause:
                    - test failure       → re-read test + source, write the fix
                    - lint/format        → run formatter, commit
                    - type-check         → fix types
                    - secret detected    → abort (NEVER auto-remove secrets; user must decide)
                    - review feedback    → read review comments, apply reviewer-requested change
  7. APPLY FIX  — modify code (Edit/Write). One logical change per iteration.
  7.5. LOCAL VERIFY (only when --local-verify set; opt-in flag) —
       run `--local-test-cmd` (default `pytest -q`) inside the worktree
       via `lib.babysit_pr_cli.run_local_verify(cmd=..., cwd=<worktree>)`.
       MUST-L3: the iteration records the command's quoted pytest tail
       line (`<N> passed in <Ns>s` or `<N> failed in <Ns>s`) returned in
       `LocalVerifyResult.tail_line`. If the helper returns
       `passed=False` — non-zero exit, timeout, or exit 0 without a tail
       line — refuse to advance to step 9. Abort the iteration
       **before** `git add` / `git commit` / `git push` — no commit, no
       push, no GH-Actions run consumed. `lib.babysit_pr_cli.lint_local_test_cmd`
       returns shell-meta warnings (informational only; the operator
       owns the boundary). This is the broad pre-commit gate (full
       pytest run); step 8 (below) is the narrow post-fix re-check of
       the specific failing check. Default-absent path is unchanged:
       skip this step entirely.
  8. VERIFY LOCAL — HARD GATE, re-run the same failing command locally;
                    quote exit code + test count.
                    - Local verify PASSES → proceed to step 9 (COMMIT).
                    - Local verify FAILS  → do NOT commit or push. A push
                      that still fails locally will fail the same way in
                      CI ~1-10 min later (pytest/lint run, or the ~3-5 min
                      LLM review+security pair) — pushing it anyway just
                      burns a full CI cycle to relearn what step 8 already
                      knows. Instead: go back to step 6 (DIAGNOSE) with the
                      new local failure output and retry within the SAME
                      iteration. This retry-in-place does NOT increment
                      `iter` on its own; it counts toward the 3-consecutive-
                      no-progress guard the same way a pushed-and-still-
                      failing CI run would.
  9. COMMIT     — `git add <specific paths>` of the file(s) just modified (NEVER `git add -p` — interactive, hangs without TTY; the skill runs unattended) + conventional commit
  10. PUSH     — `git push origin HEAD`
  11. LOG     — append one line to `.dev-kit/babysit.log`:
                  `<ISO-8601> iter=<n> check=<name> fix=<one-line> exit=<code>`
  12. SLEEP    — `gh pr checks --watch` or sleep 20s for CI to pick up
  13. SAVE STATE — overwrite `.dev-kit/babysit-checks.json` with
                  `build_check_state(CHECKS)` from the fresh snapshot just
                  polled, so the next iteration's step 1 diffs against it.
  14. INCREMENT — `iter = iter + 1`; if `iter > MAX_ITERS`, fall through to
                  the cap-fallback below; otherwise `goto 1`.
```

### Durable approval-seeking loop

The loop persists its control-plane state in `.dev-kit/babysit-state.json`
using `lib/babysit_pr_loop.py`. The durable phases are:

```text
REPAIRING → WAIT_FOR_CHECKS → REPAIRING
     │              │
     ├──────────────┴────→ WAIT_FOR_APPROVAL → DONE
     └─ unchanged/stale → RECOVERY_REQUIRED → resume on fresh evidence
```

`DONE` is reserved for `REVIEW_VERDICT=APPROVED` with every required check in
`{success, skipped, neutral}`. `WAIT_FOR_APPROVAL` is healthy long-running
state, not exit-0 completion. `RECOVERY_REQUIRED` is also resumable: it stops
repeating an unproductive patch while retaining the PR, evidence, context
epoch, repair attempt, and next wake information. A worker restart loads this
state before taking action and must re-snapshot GitHub immediately.

The pure state machine records context-epoch changes when the head SHA moves,
and turns repeated no-information outcomes into `continue`, `evolve_step`,
`change_direction`, `reset_context`, and finally `recover`. These are strategy
changes, not approval decisions; safety valves, bounded repair attempts,
fresh verification, and the human-only merge boundary remain unchanged.

### Check-state caching (efficiency)

Every iteration used to re-poll and re-diagnose every check from scratch,
even checks whose failure was already fixed-for in a prior iteration and
are simply waiting on a slow CI runner. `lib/babysit_pr_reliability.py`
exposes two pure helpers for this:

- `build_check_state(checks)` — reduces a `gh pr checks --json
  name,conclusion,databaseId` listing to `{name: {conclusion,
  databaseId}}`.
- `diff_check_states(prev_state, curr_checks)` — classifies each current
  check as `"changed"` (new, or its conclusion/databaseId moved since the
  cache) or `"unchanged"` (identical to the cache).

The cache file `.dev-kit/babysit-checks.json` (gitignored, same directory
as `babysit.lock`) holds the previous iteration's `build_check_state()`
output. Step 5 (FETCH LOGS) and step 6 (DIAGNOSE) only do real work for
checks in `changed` — a check whose databaseId AND conclusion are both
unchanged since the last snapshot has nothing new to diagnose; the
earlier iteration's fix attempt (or the WAIT branch) is still the correct
action. This does not skip CI itself — GitHub still re-runs every
workflow on every push, which is unavoidable — it only avoids redundant
local polling, log-fetching, and re-diagnosis work inside the babysit-pr
loop for checks whose state has not moved.

### Bounded repair PR policy

The loop uses one shared coordinator state for local babysitting and GitHub
review events:

```text
attempt 0: repair the original PR once
no progress: create repair PR attempt 1
no progress: create repair PR attempt 2
no progress: emit human_exception evidence and stop creating PRs
```

"No progress" means the failure signature is unchanged, the finding count did
not decrease, and the successful-check count did not increase. A changed
failure with improving verification is progress and continues the loop.

The durable state must include `parent_pr`, `current_pr`, `attempt`,
`failure_signature`, `run_id`, and `commit_sha`. Before creating a repair PR,
deduplicate on `(parent_pr, attempt, failure_signature)`. The user does not
need to choose between `autofix` and `babysit`: `/dev-kit:babysit-pr` owns the
repair lifecycle; the GitHub workflow only wakes the same coordinator.

Step 0 is the **pre-loop opt-out check** the bypass requires. Without
it, the flag reaches the §Algorithm pseudocode but never
`run_babysit_once`, so the bypass silently no-ops and the PR is left
waiting for human review. Step 0 must run *before* step 1's SNAPSHOT
so the bypass decision happens on a fresh state, not on stale
verdicts. The four `bpc._*` shim assignments are mandatory -- the
helper's default shims raise RuntimeError; without the real
`subprocess.run`/`print` wiring, the helper cannot post the audit
comment or schedule the merge.

If `iter == MAX_ITERS` and PR is still blocked → persist the unresolved
blocker list and exit the current worker with a resumable watchdog status.
**Never** silently retry past the cap, but do not turn the PR into success or
discard its state. The next explicit resume or provider event can continue
from `.dev-kit/babysit-state.json`.

---

## Safety valves

- **No `git push --force`** to `main`/`master`. Force-push to feature branches is
  allowed when the PR is your own (PR author == current user) AND the branch is
  not protected.
- **No auto-merge, ever.** `gh pr merge` is always forbidden — merging
  into `main` is a human-only action. The single-operator bypass (see
  `## Single-operator bypass` below) only confirms ownership and posts
  an audit comment; it never runs `gh pr merge`. The human operator
  merges manually.
- **No secret auto-removal**. If `secret-scan` or any check flags a credential,
  the skill aborts with the file:line and exits 1.
- **No destructive git operations**: no `reset --hard`, no `clean -fd`, no
  branch deletion.
- **One PR at a time**: refuse to run if there is already a babysit process
  (lock file at `.dev-kit/babysit.lock`).
- **No worktree juggling** (per worktree hygiene in project memory).

## Single-operator bypass (`--operator-is-only-human`, issue #324)

When the flag is set on the slash command, the skill checks for
alternate reviewers before exiting 0. The flow uses
`lib/babysit_pr_cli.py::run_babysit_once(...)`, which stays pure (no
`subprocess`/`gh` inside the helper) so the bypass contract is
reproducible in CI without network access:

```
1. PARSE argv via parse_babysit_args(argv).
   No flag -> emit "REVIEW_REQUIRED -> human-gate" + exit 0 (legacy).
2. FLAG set + no rationale -> emit parser error + SystemExit 2.
3. FLAG set + rationale present:
   a. Read .github/CODEOWNERS via parse_codeowners(path).
   b. Resolve COLLABORATORS via:
        gh api /repos/{owner}/{repo}/collaborators?per_page=100 -q '.[].login'
      (skip silently if the endpoint errors; fall back to CODEOWNERS-only
      detection -- the call is best-effort, not a hard prerequisite).
   c. has_alternate_owners(operator, codeowners, collaborators) returns
      (has_alternate, alternates).
   d. Has alternates -> print "Refusing --operator-is-only-human:
      alternate owner(s) found: <names>" + "Falling back to the
      human-gate path" + exit 1.
   e. No alternates -> post the audit comment via
      gh pr comment <PR_NUMBER> --body "/ownership-confirmed by
      operator=<handle> at <ISO-8601>; rationale=<text>", then exit
      0. The PR is NEVER merged by this skill -- the operator runs
      `gh pr merge` themselves.
```

### Wiring the side-effect shims

`run_babysit_once` is pure; the skill installs named shims before
calling it:

| Shim                  | Real-world implementation                                       |
|-----------------------|-----------------------------------------------------------------|
| `_write_stdout(s)`    | `print(s, flush=True)`                                          |
| `_write_stderr(s)`    | `print(s, file=sys.stderr, flush=True)`                         |
| `_post_pr_comment(n, body)` | `subprocess.run(['gh', 'pr', 'comment', str(n), '--body', body], check=True)` |

There is no merge shim -- the orchestrator never calls `gh pr merge`.
The shims let `tests/test_babysit_pr_cli.py` pin the orchestrator's
I/O contract without mocking `subprocess` or `gh`. Failure to install
the shims is a `RuntimeError`; the skill wires them at the top of the
SLASH entry so a misconfigured run fails loudly rather than silently.

### Wiring the helper into the SKILL execution path

The skill body must actually invoke `lib.babysit_pr_cli.run_babysit_once(...)`
with the slash arguments — the §Algorithm pseudocode describes *what should
happen*; this section is *how the skill does it*. The operator's
`--operator-is-only-human` flag must reach the helper through a real Python
call, not just narrative compliance.

Full Python wiring (side-effect shims, ownership resolution, exit-code
mapping): `skills/babysit-pr/recipes/canonical-wiring.md`. Read that
recipe before any edit to this section — if the recipe and this pointer
drift, `tests/test_babysit_pr_cli.py` will fail.

### Fail-closed ownership policy

- `CODEOWNERS_PATH` is the file at `.github/CODEOWNERS`. The parse is
  token-level and tolerates `# comments`, `@user` and `@org/team` forms,
  and skips `user@domain` email handles (not actionable review gates).
  **Fail-closed**: any IO error reading CODEOWNERS (missing file,
  permission denied, is-a-directory) refuses the bypass with
  `EXIT_OWNERSHIP_UNKNOWN`. An outage or permission glitch CANNOT be
  interpreted as "no alternate owners" -- the bypass requires
  positive ownership confirmation before posting the audit comment.
- `COLLABORATORS` endpoint is rate-limited; an empty collaborator
  list does NOT, on its own, grant the bypass. The bypass requires
  CODEOWNERS to be readable AND to list only the operator. The
  collaborators list is supplementary -- it widens the alt-owner set
  when present, but its absence does not narrow it.

### Policy invariants (the bypass is one-human-only)

- Operator handle is `gh api /user -q .login` -- the bot's own identity,
  not the PR author. Author/operator split matters on private repos
  where a bot opens PRs on behalf of a human.
- CODEOWNERS read + collaborator resolution are covered by
  `### Fail-closed ownership policy` above. An outage or empty
  collaborator list CANNOT authorize the bypass.
- The bypass never runs `gh pr merge`. It only posts the
  `/ownership-confirmed` audit comment and hands off -- the operator
  merges manually, on their own schedule.
- Rationale is required. Empty rationale is refused at the parser
  level to keep the audit trail non-vacuous.

### Example invocation

```bash
/dev-kit:babysit-pr --operator-is-only-human \
  --rationale "trivial README typo in docstring; no behavior change"
```

```bash
# Local-only mode: run pytest before each iteration's push so a failing
# local test aborts without burning GH-Actions minutes.
# (Additive flag; default behavior is unchanged when --local-verify is absent.)
/dev-kit:babysit-pr --local-verify
```

```bash
# Local-only mode with a project-specific test command.
# MUST-L3: stdout/stderr MUST include a pytest-style tail line.
/dev-kit:babysit-pr --local-verify --local-test-cmd "make test"
```

For the iron-law audit trail the rationale text appears verbatim in the
audit comment (`tests/test_babysit_pr_cli.py::TestFormatOwnershipConfirmedComment`
pins the comment shape). Operators are encouraged to link the PR URL
or issue number inside the rationale text for cross-reference.

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

If the same failure recurs after **3 consecutive iterations** with no new
information, persist `RECOVERY_REQUIRED` with the unresolved blocker list and
quoted log snippets, then enter durable recovery wait. Do not silently retry
the same patch, lower the bar, or skip. A later fresh check/review event,
explicit resume, or newer model run may continue the same PR. Only
`APPROVED` plus green required checks is a successful terminal state.

---

## Lock file protocol

On start (existing-lock safety net FIRST; then stamp-and-write):

```bash
mkdir -p .dev-kit
if [[ -f .dev-kit/babysit.lock ]]; then
  # Detect stale locks (SIGKILL / OOM / network-partition from a previous
  # run) before refusing. The pure helper at lib/babysit_pr_reliability.py
  # returns True when EITHER:
  #   (a) the lock mtime is older than ttl_seconds (default 1800s == 30 min), OR
  #   (b) the recorded pid= field names a process that no longer exists.
  # TTL is generous for the babysit cycle (each iteration is a few minutes
  # at most); a SIGKILL is the case the helper specifically catches.
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
echo "$(date -Iseconds) pid=$$ branch=$(git rev-parse --abbrev-ref HEAD)" > .dev-kit/babysit.lock
trap 'rm -f .dev-kit/babysit.lock' EXIT
```

The stale-lock detection is the close for Gap #11 in
`docs/hooks/hook-coverage-gaps.md`. The helper is pure (no I/O randomness;
`now_epoch` is parameterizable for tests) so the same logic is used by
`tests/test_babysit_pr_reliability.py::TestIsStaleLock` end-to-end.

### Ghost-workflow classification (Gap #12)

`gh pr checks` may report a check whose underlying workflow file has
been deleted server-side; the check stays `conclusion=null`/`state=pending`
indefinitely. The §Algorithm step 4 wait loop (sleep 30s, goto 1) would
otherwise spin until MAX_ITERS. Replace the sleep with classify-driven
gating:

```bash
# Per failing/pending check, ask classify_check whether it is a ghost.
# A "ghost" means: no databaseId, OR startedAt/updatedAt is older than
# ghost_threshold_seconds (default 300s == 5 min) -- i.e. no live
# workflow will ever resolve it.
GHOSTS=$(gh pr checks --json name,state,conclusion,databaseId,startedAt,updatedAt   | python3 -c "
import json, sys
checks = json.load(sys.stdin)
import pathlib; sys.path.insert(0, 'lib')
import babysit_pr_reliability as bpr
now = $(date +%s)
ghosts = [c['name'] for c in checks if bpr.classify_check(c, now) == 'ghost']
print(','.join(ghosts))
")
if [[ -n "$GHOSTS" ]]; then
  echo "ghost workflow check(s) detected: $GHOSTS -- surfacing as recovery-required"
  # Print the recovery hint and break the loop instead of busy-waiting.
fi
```

The classify helper is `classify_check(check_dict, now_epoch, ghost_threshold_seconds=300)`
in `lib/babysit_pr_reliability.py`. Pin tests live in
`tests/test_babysit_pr_reliability.py::TestClassifyCheck` (13 cases).

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
- `bash-guard=ON` — guards `git push --force` patterns.
- `git-guard=ON` — hard-blocks `gh pr merge` (any invocation); merging into
  `main` is always a human action, run outside automation.

---

## Output language

All stdout/stderr messages in **English only**.

---

## Next step

When the loop terminates with `✅ PR approved`, recommend `/dev-kit:ship` to
tag and release (the user still controls the actual merge + tag push). On
abnormal exit, recommend `/dev-kit:evaluate` against the failing case + a
manual patch via `/dev-kit:build` or `/dev-kit:refactor` (the historical asset-repair
loop documented in `docs/adr/ADR-0021-eval-repair-loop.md` has no live runtime).

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
