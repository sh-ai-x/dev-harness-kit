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
| `OPERATOR_HANDLE`| `gh api /user -q .login` (the human running the babysitter)           |
| `CODEOWNERS_PATH`| `$REPO_ROOT/.github/CODEOWNERS` (parsed by `lib/babysit_pr_cli.py`)    |
| `COLLABORATORS`  | `gh api /repos/{owner}/{repo}/collaborators?per_page=100 -q '.[].login'` |

### CLI flags (issue #324)

```
/dev-kit:babysit-pr [--operator-is-only-human] [--rationale "<text>"]
```

| Flag                       | Effect |
|----------------------------|--------|
| *(no flag)*                | Default behavior: print `REVIEW_REQUIRED -> human-gate` and exit 0. The flag-absent path is the audit-safe default — operators never accidentally bypass review. |
| `--operator-is-only-human` | Opt-out for single-operator repos. Refuses with exit 1 if `CODEOWNERS_PATH` OR `COLLABORATORS` list any handle other than `OPERATOR_HANDLE`. Requires `--rationale`. Posts the audit comment `/bot-approve by operator=<handle> at <ISO-8601>; rationale=<text>` and schedules `gh pr merge --auto --squash`. |
| `--rationale "<text>"`     | Required when `--operator-is-only-human` is set; quoted verbatim into the audit comment. The flag pair is the *only* canonical way to bypass the human-review gate. |

If `PR_NUMBER` is empty OR `PR_STATE != OPEN` → print a one-line message and
exit 0. Never create a PR implicitly (MUST: explicit user action).

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
2. List candidate PRs off main:
   ```bash
   gh pr list --state open --json number,headRefName,headRefOid,title \
     --jq '.[] | select(.headRefName != "main")'
   ```
3. Zero candidates → print `no open PR off main; nothing to babysit` and exit 0
   (preserves the existing "no PR → exit 0" contract).
4. Multiple candidates → print a numbered list
   `number | headRefName | headRefOid | title` and exit 0. Never auto-pick
   when ambiguous (explicit user action required).
5. Exactly one candidate → resolve its owning worktree:
   ```bash
   git worktree list --porcelain \
     | awk '/^worktree /{wt=$2; next} /^HEAD [0-9a-f]/{print wt, $2}'
   ```
   Match the line whose second field equals `<pr>.headRefOid`.
6. If no local worktree owns the candidate branch → create one and verify:
   ```bash
   git worktree add -b <headRefName> .worktrees/<headRefName> origin/<headRefName>
   cd .worktrees/<headRefName>
   [[ "$(git rev-parse HEAD)" == "<headRefOid>" ]] \
     || { echo "HEAD mismatch after worktree add"; exit 1; }
   ```
   (the literal `origin/<headRefName>` above is the remote-tracking ref)
7. If a local worktree owns the branch → use its existing path.
8. `cd <worktree_path>` once. The parent's Bash cwd persists for the rest of
   the parent's session, so the resolved worktree is now the parent cwd.

### Sub-agent delegation

Spawn the sub-agent via the `Agent` tool with `subagent_type: "general-purpose"`
and the following prompt body verbatim. The sub-agent inherits the parent's
cd'd cwd as its working directory.

```
cd <worktree_path>

You are the PR babysitter for branch "<headRefName>" (PR #<number>, URL <pr_url>).
Operate ONLY inside <worktree_path>. Do NOT touch the main checkout.

Algorithm (condensed from the parent skill's Algorithm section):
  1. SNAPSHOT — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS via `gh pr view` /
     `gh pr checks`.
  2. TERMINATE — if REVIEW_VERDICT == "APPROVED" AND every check.conclusion
     ∈ {success, skipped, neutral}, print "PR approved" and exit 0.
  3. CLASSIFY — A) CI failing, B) CI pending (wait), C) CHANGES_REQUESTED,
     D) REVIEW_REQUIRED (exit 0 with human-gate message).
  4. WAIT — if any check pending and no failures, sleep 30s and goto 1.
  5. FETCH LOGS — for each failing check, `gh run view <id> --log-failed`;
     truncate to last 200 lines; capture exit code + first error.
  6. DIAGNOSE — one root cause per failing check: test failure, lint/format,
     type-check, secret detected (abort), review feedback.
  7. APPLY FIX — Edit/Write. One logical change per iteration.
  8. VERIFY LOCAL — re-run the failing command; MUST-L3: quote exit code +
     test count in this format:
       local:  <command> → <result> (exit <code>)
  9. COMMIT — `git add <specific paths>` (NEVER `git add -p`).
  10. PUSH — `git push origin HEAD`.
  11. LOG — append to .dev-kit/babysit.log:
         <ISO-8601> iter=<n> check=<name> fix=<one-line> exit=<code>
  12. SLEEP — `gh pr checks --watch` or sleep 20s.
  13. INCREMENT iter; goto 1.

Termination conditions:
  - APPROVED + green → exit 0.
  - REVIEW_REQUIRED → exit 0 with "waiting for human review" message.
  - CHANGES_REQUESTED → apply + iterate.
  - 3 consecutive iterations with no progress → exit 1 with the blocker list.

Lock file: write <worktree_path>/.dev-kit/babysit.lock (NOT the parent's
main-checkout lock). On exit: `trap 'rm -f .dev-kit/babysit.lock' EXIT`.

Iron Laws (apply to every claim of progress):
  - L1: no prod code without verification artifact.
  - L2: no fix without reproducing the bug.
  - L3: no completion claim without quoted exit code / test count / build log.
  - L4: no TODO/FIXME/"we'll extend later".
  - L5: no option list when not asked. One answer.

Safety valves (forbidden):
  - git push --force to main/master.
  - gh pr merge (user merges); the single-operator opt-out
    (`--operator-is-only-human`) is the only sanctioned merge path the
    babysitter can drive.
  - secret auto-removal (abort + exit 1 on credential detection).
  - destructive git ops: reset --hard, clean -fd, branch -D.
  - pytest.skip / @unittest.skip / removing tests / commenting assertions.
  - marking required checks optional / continue-on-error.
  - closing the PR to bypass the LLM review gate.
  - || true / || echo skipped on steps that exist to fail loudly.
  - raising exit thresholds to mask flaky steps.
  - "fixed" claims without the quoted `local:  ... (exit <code>)` line.
```

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
  1. SNAPSHOT   — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS (single gh call)
  2. TERMINATE  — if REVIEW_VERDICT == "APPROVED"
                    AND every check.conclusion ∈ {success, skipped, neutral}
                    → print "✅ PR #<n> approved — done" + iterate count
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
- **No auto-merge** *unless* the single-operator bypass is engaged
  (see `## Single-operator bypass` below). `gh pr merge` is forbidden
  by default. The user merges unless they opted into
  `--operator-is-only-human`, in which case the skill schedules
  `gh pr merge --auto --squash` and waits for CI to finalize the merge.
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
      gh pr comment <PR_NUMBER> --body "/bot-approve by operator=<handle>
      at <ISO-8601>; rationale=<text>", then run
      gh pr merge <PR_NUMBER> --auto --squash, then exit 0.
```

### Wiring the four side-effect shims

`run_babysit_once` is pure; the skill installs four named shims before
calling it:

| Shim                  | Real-world implementation                                       |
|-----------------------|-----------------------------------------------------------------|
| `_write_stdout(s)`    | `print(s, flush=True)`                                          |
| `_write_stderr(s)`    | `print(s, file=sys.stderr, flush=True)`                         |
| `_post_pr_comment(n, body)` | `subprocess.run(['gh', 'pr', 'comment', str(n), '--body', body], check=True)` |
| `_run_pr_merge(n, argv)` | `subprocess.run(['gh', *argv], check=True)` where argv = `['pr', 'merge', str(n), '--auto', '--squash']` |

The four shims let `tests/test_babysit_pr_cli.py` pin the orchestrator's
I/O contract without mocking `subprocess` or `gh`. Failure to install
the shims is a `RuntimeError`; the skill wires them at the top of the
SLASH entry so a misconfigured run fails loudly rather than silently.

### Wiring the helper into the SKILL execution path

The skill body must actually invoke `lib.babysit_pr_cli.run_babysit_once(...)`
with the slash arguments -- the §Algorithm pseudocode describes *what
should happen*; this section is *how the skill does it*. The operator's
`--operator-is-only-human` flag must reach the helper through a real
Python call, not just narrative compliance.

The canonical wiring (executed at the top of every `/dev-kit:babysit-pr`
invocation, after the §Lock file protocol):

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

import babysit_pr_cli as bpc   # noqa: E402  (path set up above)

bpc._write_stdout   = lambda s: print(s, flush=True)
bpc._write_stderr   = lambda s: print(s, file=sys.stderr, flush=True)
bpc._post_pr_comment = lambda n, body: subprocess.run(   # noqa: E731
    ["gh", "pr", "comment", str(n), "--body", body], check=True)
bpc._run_pr_merge   = lambda n, argv: subprocess.run(     # noqa: E731
    ["gh", *argv], check=True)

argv = sys.argv[1:]
operator = subprocess.run(
    ["gh", "api", "/user", "-q", ".login"], check=True,
    capture_output=True, text=True).stdout.strip()
codeowners_path = Path(".github/CODEOWNERS")
collaborators = subprocess.run(
    ["gh", "api", f"/repos/{owner}/{repo}/collaborators?per_page=100",
     "-q", ".[].login"],
    capture_output=True, text=True).stdout.splitlines() or []
pr_number = int(subprocess.run(
    ["gh", "pr", "view", "--json", "number", "-q", ".number"],
    capture_output=True, text=True).stdout.strip())

rc = bpc.run_babysit_once(
    argv=argv,
    operator_handle=operator,
    codeowners_path=codeowners_path,
    collaborator_handles=collaborators,
    pr_number=pr_number,
)
sys.exit(rc)
```

If this wiring block is missing from the skill body, the flag reaches
the §Algorithm pseudocode but never `run_babysit_once`, so the bypass
silently no-ops and the PR is left waiting for human review. The
`tests/test_babysit_pr_cli.py` suite pins the helper's behaviour in
isolation; this section is the only place the
slash-arguments-reach-the-helper contract lives.

### Fail-closed ownership policy

- `CODEOWNERS_PATH` is the file at `.github/CODEOWNERS`. The parse is
  token-level and tolerates `# comments`, `@user` and `@org/team` forms,
  and skips `user@domain` email handles (not actionable review gates).
  **Fail-closed**: any IO error reading CODEOWNERS (missing file,
  permission denied, is-a-directory) refuses the bypass with
  `EXIT_MULTI_OWNER`. An outage or permission glitch CANNOT be
  interpreted as "no alternate owners" -- the bypass requires
  positive ownership confirmation before authorizing the auto-merge.
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
- `gh pr merge --auto --squash` waits for CI. If a check fails after
  the bypass, GitHub cancels the auto-merge and the operator must
  re-investigate -- the audit comment remains in the PR thread as a
  record of the bypass.
- Rationale is required. Empty rationale is refused at the parser
  level to keep the audit trail non-vacuous.

### Example invocation

```bash
/dev-kit:babysit-pr --operator-is-only-human \
  --rationale "trivial README typo in docstring; no behavior change"
```

For the iron-law audit trail the rationale text appears verbatim in the
audit comment (`tests/test_babysit_pr_cli.py::TestFormatBotApproveComment`
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

If the same failure recurs after **3 consecutive iterations** with no
progress, exit 1 and print the unresolved blocker list with quoted log
snippets — do not silently retry past the cap, do not lower the bar, do
not skip.

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
`docs/hook-coverage-gaps.md`. The helper is pure (no I/O randomness;
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
