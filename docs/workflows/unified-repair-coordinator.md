# Unified repair coordinator — Code-Viz

This document is the source-level Code-Viz record for the repair workflow.
`/dev-kit:babysit-pr` is the only user-facing repair entrypoint. GitHub Actions
only emits events into the same coordinator contract.

The dedicated Archidraw MCP rendering is documented in the
[babysit-pr architecture](../architecture/2026-08-24/babysit-pr-architecture.md)
reference. It describes the repair loop and its human merge boundary; it does
not represent a fixed commit count.

## Architecture

```mermaid
flowchart TD
  USER([user]) --> BABY[/dev-kit:babysit-pr/]
  GH[GitHub review / CI events] --> ADAPTER[auto-fix-pr adapter]
  BABY --> COORD[repair coordinator]
  ADAPTER --> COORD
  COORD --> STATE[(repair state + lock)]
  COORD --> LOG[(append-only repair events)]
  COORD --> WORKER[diagnose / patch / verify worker]
  WORKER --> PR[original PR]
  WORKER --> R1[repair PR 1]
  WORKER --> R2[repair PR 2]
  PR --> GH
  R1 --> GH
  R2 --> GH
```

## Lifecycle

```mermaid
flowchart TD
  START([PR event or babysit command]) --> OBSERVE[observe checks and typed findings]
  OBSERVE --> REPRODUCE[reproduce failure]
  REPRODUCE --> PATCH[apply one minimal patch]
  PATCH --> VERIFY[focused test + full required verification]
  VERIFY --> REVIEW[independent diff review]
  REVIEW --> GATE{all required gates explicit?}
  GATE -->|yes| MERGE[final recheck and human merge]
  GATE -->|no| PROGRESS{measurable progress?}
  PROGRESS -->|yes| OBSERVE
  PROGRESS -->|no, attempt 0| R1[create repair PR attempt 1]
  R1 --> OBSERVE1[observe repair PR 1]
  OBSERVE1 --> PROGRESS1{measurable progress?}
  PROGRESS1 -->|yes| OBSERVE
  PROGRESS1 -->|no| R2[create repair PR attempt 2]
  R2 --> OBSERVE2[observe repair PR 2]
  OBSERVE2 --> PROGRESS2{measurable progress?}
  PROGRESS2 -->|yes| OBSERVE
  PROGRESS2 -->|no| EXCEPTION[human_exception evidence bundle]
```

## Event and state contract

```mermaid
sequenceDiagram
  participant E as GitHub event
  participant C as Coordinator
  participant W as Repair worker
  participant P as PR
  participant L as Event log
  E->>C: review/CI event + run_id
  C->>C: acquire PR lock and deduplicate key
  C->>L: repair_started(parent_pr, attempt, signature)
  C->>W: compact context + target findings
  W->>W: reproduce, patch, focused verify, full verify
  W->>P: push one bounded repair wave
  P-->>C: checks + typed verdicts
  C->>L: repair_finished(status, commit_sha, tokens, cost)
  alt no progress and attempt < 2
    C->>P: create linked repair PR
  else no progress and attempt = 2
    C->>L: human_exception(evidence bundle)
  end
```

## Before / after

```mermaid
flowchart LR
  subgraph BEFORE[before]
    B1[review submitted] --> B2[auto-fix edits same PR]
    B2 --> B3[push]
    B3 --> B4[review reruns]
    B4 --> B2
    B4 --> B5[5 label cap]
  end
  subgraph AFTER[after]
    A1[review or CI event] --> A2[shared coordinator]
    A2 --> A3[original PR attempt 0]
    A3 --> A4[verify and recheck]
    A4 -->|no progress| A5[repair PR 1]
    A5 --> A4
    A4 -->|no progress| A6[repair PR 2]
    A6 --> A4
    A4 -->|all gates pass| A7[human merge]
    A4 -->|third no-progress| A8[exception evidence]
  end
```

## Invariants

- `attempt` is `0`, `1`, or `2`; no fourth repair PR is created.
- `failure_signature` is deterministic and is part of duplicate suppression.
- Missing or malformed required verdicts are never approvals.
- Only one coordinator worker may modify a PR at a time.
- Every patch has focused verification, full verification, commit SHA, and an event record.
- Transcripts remain available for recovery; compact events drive analysis and token accounting.

## Current execution policy

```mermaid
flowchart LR
  CHANGE[PR changes code] --> REVIEW[review.yml via pull_request]
  CHANGE --> MAINT[maintenance.yml via pull_request]
  WORKFLOW[PR changes .github/workflows] --> VALIDATE[Claude workflow validation guard]
  VALIDATE -->|skip until merged| HUMAN[human merges workflow PR]
  HUMAN --> NEXT[next PR receives normal review]
  REVIEW --> COMMENT[review/security comments + audit comment]
  MAINT --> COMMENT
  COMMENT --> GATE[required checks and human merge]
```

The review and maintenance workflows run on `pull_request` and inspect the PR
in PR-head context, so the checked-out PR content remains untrusted. Fork PRs do
not receive repository secrets; the consumer CI template additionally uses a
same-repository/fork guard, while this repository's root workflows do not. A PR
that changes a workflow file can still be reported as skipped by Claude's
workflow-validation guard because the PR copy does not yet match the default
branch. That is an expected bootstrap boundary: the workflow change is merged
by a human, and the next ordinary PR exercises the updated workflow.

`/dev-kit:babysit-pr` may diagnose, patch, verify, create bounded repair PRs,
and write audit events, but it never merges into `main`. Passing gates produce
a human-merge hand-off, not an automatic merge. Missing or malformed agent
verdicts are recorded as audit evidence and do not constitute a human approval.
