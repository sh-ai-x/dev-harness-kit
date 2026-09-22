# PRD — Bounded Self-Improving RALPH Meta-Harness

## Goal

Apply the accepted Best Trade-off proposal to the existing dev-kit workflow
without turning the harness into a service platform or constraining normal
model exploration.

## Acceptance criteria

1. Normal Stop in an attended RALPH run records a checkpoint and exits 0.
2. A worker/session boundary never synthesizes workflow `COMPLETED`.
3. A fresh worker can read the latest checkpoint and continue the next action.
4. Completion evidence contains artifact hashes, acceptance checks, verifier
   provenance, and candidate id; promotion remains idempotent and fail-closed.
5. Existing non-RALPH Stop/SessionEnd behavior remains compatible.
6. Replay/holdout entry points are offline and do not block active RALPH work.
7. Targeted regression tests pass.

## Explicit exclusions

No daemon, database, queue, universal tool wrapper, raw result mirror,
cross-machine scheduling, or automatic security/kernel/product-code changes.
