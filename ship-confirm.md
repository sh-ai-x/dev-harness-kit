# Ship confirmation — approved unattended implementation

- Scope: bounded RALPH continuity, completion receipt, offline replay contract.
- Safety: emergency hooks remain fail-closed; normal RALPH Stop is non-blocking.
- Project fit: local files, existing state/trace/eval/worktree primitives only.
- Human boundary: no auto-merge; PR lands at `USER_MERGE_REQUIRED`.
- Verification: targeted tests, full suite, diff check, and PR checks.
- Rollback: revert candidate pointer/commit; no destructive cleanup required.
- Exclusions: daemon, database, queue, universal Tool wrapper, cross-machine scheduler.
- Operator authorization: explicit user request to continue unattended and prepare a PR.
