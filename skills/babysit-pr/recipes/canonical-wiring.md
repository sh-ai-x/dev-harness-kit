# Canonical babysit-pr wiring

This is the Python block executed at the top of every `/dev-kit:babysit-pr`
invocation, after the §Lock file protocol. It installs the side-effect shims
that `lib.babysit_pr_cli.run_babysit_once` requires, resolves ownership
metadata (operator handle, repo owner/name, collaborators list, PR number),
calls the helper, and maps the helper's exit codes to the orchestrator's
contract.

If this wiring block is missing from the skill body, the flag reaches the
§Algorithm pseudocode but never `run_babysit_once`, so the bypass silently
no-ops and the PR is left waiting for human review. The
`tests/test_babysit_pr_cli.py` suite pins the helper's behaviour in
isolation; this recipe is the only place the slash-arguments-reach-the-helper
contract lives.

```python
import sys, os, json, time, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

import babysit_pr_cli as bpc   # noqa: E402  (path set up above)

bpc._write_stdout   = lambda s: print(s, flush=True)
bpc._write_stderr   = lambda s: print(s, file=sys.stderr, flush=True)
bpc._post_pr_comment = lambda n, body: subprocess.run(   # noqa:731
    ["gh", "pr", "comment", str(n), "--body", body], check=True)

argv = sys.argv[1:]
operator = subprocess.run(
    ["gh", "api", "/user", "-q", ".login"], check=True,
    capture_output=True, text=True).stdout.strip()
codeowners_path = Path(".github/CODEOWNERS")

# Resolve owner/name from the GitHub Actions env
# (`GITHUB_REPOSITORY` is "owner/name") or fall back to `gh repo view`
# so this block works both inside CI and from a local shell. Hard-fail
# loudly if neither resolves -- the older wiring referenced undefined
# `owner` / `repo` identifiers that NameError'd before the helper ran.
repo_full = os.environ.get("GITHUB_REPOSITORY")
if not repo_full:
    repo_full = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner",
         "-q", ".nameWithOwner"],
        check=True, capture_output=True, text=True).stdout.strip()
repo_owner, repo_name = repo_full.split("/", 1)

# The collaborators API call uses `check=True` so a non-zero exit
# (404, rate limit, permission error) raises. The orchestrator
# treats a confirmed non-zero as `collaborator_lookup_ok=False`
# and refuses the bypass with EXIT_OWNERSHIP_UNKNOWN. Empty
# stdout on a successful call is treated as `lookup_ok=True`
# with an empty collaborators list (and combined with the
# positive-ownership check below, refuses on empty CODEOWNERS).
collaborator_proc = subprocess.run(
    ["gh", "api",
     f"/repos/{repo_owner}/{repo_name}/collaborators?per_page=100",
     "-q", ".[].login"],
    capture_output=True, text=True)
collaborator_lookup_ok = (collaborator_proc.returncode == 0)
collaborators = (collaborator_proc.stdout.splitlines() or [])
pr_arg = bpc.parse_babysit_args(argv).pr
pr_view_target = str(pr_arg) if pr_arg is not None else ""
pr_view = subprocess.run(
    ["gh", "pr", "view", *(([pr_view_target]) if pr_view_target else []),
     "--json", "number,state", "-q", "."],
    capture_output=True, text=True)
if pr_view.returncode != 0:
    print("No open PR resolved. Pass --pr N or run from a PR worktree.", file=sys.stderr)
    sys.exit(1)
pr_snapshot = json.loads(pr_view.stdout)
pr_number = int(pr_snapshot["number"])
if pr_snapshot.get("state") != "OPEN":
    print(f"PR #{pr_number} is {pr_snapshot.get('state')}; pass an open --pr N.", file=sys.stderr)
    sys.exit(1)

# Durable control-plane wiring: snapshot before classification and persist
# the phase before choosing wait/repair/approval actions. The repair path
# calls bpc.persist_loop_outcome(...) after local verification; both helpers
# load the prior state and atomically save the transition.
checks = json.loads(subprocess.run(
    ["gh", "pr", "checks", str(pr_number), "--json",
     "name,state,conclusion,databaseId,startedAt,updatedAt"],
    check=True, capture_output=True, text=True).stdout)
loop_state = bpc.persist_loop_snapshot(
    parent_pr=pr_number,
    current_pr=pr_number,
    head_sha=subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "headRefOid",
         "-q", ".headRefOid"], check=True, capture_output=True,
        text=True).stdout.strip(),
    review_verdict=subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "reviewDecision",
         "-q", ".reviewDecision"], check=True, capture_output=True,
        text=True).stdout.strip() or None,
    checks=checks,
    now_epoch=time.time(),
    now_iso=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    github_tracker_issue=(
        int(os.environ["BABYSIT_GITHUB_TRACKER_ISSUE"])
        if os.environ.get("BABYSIT_GITHUB_TRACKER_ISSUE") else None
    ),
    linear_issue=os.environ.get("BABYSIT_LINEAR_ISSUE", ""),
)
print(f"babysit phase={loop_state.phase} strategy={loop_state.strategy}", flush=True)

# Publish only the transition key, never every polling cycle. The adapter
# checks that marker on both trackers before creating a comment, so a worker
# restart is safe. A tracker outage is non-blocking: the durable local state
# remains authoritative and the next invocation retries the same key.
transition_key = f"{loop_state.parent_pr}:{loop_state.head_sha}:{loop_state.context_epoch}:{loop_state.phase}"
if loop_state.github_tracker_issue:
  subprocess.run([
    "python3", "tools/babysit_tracker_sync.py",
    "--repo", repo_full,
    "--github-issue", str(loop_state.github_tracker_issue),
    "--linear-issue", loop_state.linear_issue,
    "--key", transition_key,
    "--pr", str(pr_number),
    "--phase", loop_state.phase,
    "--strategy", loop_state.strategy,
    "--head-sha", loop_state.head_sha,
    "--context-epoch", str(loop_state.context_epoch),
    "--review", pr_snapshot.get("reviewDecision") or "REVIEW_REQUIRED",
    "--checks", f"{sum(1 for c in checks if c.get('bucket') in {'pass', 'skipping'})}/{len(checks)} green",
  ], check=False)

rc = bpc.run_babysit_once(
    argv=argv,
    operator_handle=operator,
    codeowners_path=codeowners_path,
    collaborator_handles=collaborators,
    collaborator_lookup_ok=collaborator_lookup_ok,
    pr_number=pr_number,
)
# Map the helper's exit codes to the orchestrator's contract:
#   EXIT_OK (0)                 -> 0  (ownership confirmed, audit
#                                    comment posted; PR NOT merged --
#                                    the operator merges manually)
#   EXIT_MULTI_OWNER (1)        -> 0  (alternate owners found;
#                                    human-gate per §Algorithm step 3D)
#   EXIT_RATIONALE_REQUIRED (2) -> 2  (operator must add --rationale)
#   EXIT_OWNERSHIP_UNKNOWN (4)  -> 0  (collaborator/CODEOWNERS
#                                    could not be confirmed; the
#                                    bypass refused itself, the
#                                    helper printed the reason; the
#                                    fallback path is the human-gate)
if rc in (bpc.EXIT_OK, bpc.EXIT_MULTI_OWNER, bpc.EXIT_OWNERSHIP_UNKNOWN):
    sys.exit(0)
sys.exit(rc)
```

## Sub-agent prompt body (moved from SKILL.md)

When delegating to a sub-agent via the `Agent` tool with
`subagent_type: "general-purpose"`, use this prompt body verbatim. The
sub-agent inherits the parent's cd'd cwd as its working directory:

```
cd <worktree_path>

You are the PR babysitter for branch "<headRefName>" (PR #<number>, URL <pr_url>).
Operate ONLY inside <worktree_path>. Do NOT touch the main checkout.

Algorithm (condensed from the parent skill's Algorithm section):
  1. SNAPSHOT — fetch PR_NUMBER, REVIEW_VERDICT, CHECKS via `gh pr view` /
     `gh pr checks` (re-issue immediately before acting — see parent
     SKILL.md "MUST — re-verify state immediately before acting").
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
  - gh pr merge — always forbidden. Merging into main is a human-only
    action; the babysitter (including the single-operator opt-out,
    `--operator-is-only-human`) only ever confirms ownership and posts
    an audit comment, never merges.
  - secret auto-removal (abort + exit 1 on credential detection).
  - destructive git ops: reset --hard, clean -fd, branch -D.
  - pytest.skip / @unittest.skip / removing tests / commenting assertions.
  - marking required checks optional / continue-on-error.
  - closing the PR to bypass the LLM review gate.
  - || true / || echo skipped on steps that exist to fail loudly.
  - raising exit thresholds to mask flaky steps.
  - "fixed" claims without the quoted `local:  ... (exit <code>)` line.
```
