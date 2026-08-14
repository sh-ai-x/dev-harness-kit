# Linear PR state sync

`tools/linear_pr_sync.py` is invoked by `.github/workflows/linear-pr-sync.yml` to keep the Linear issue associated with a pull-request branch aligned with the PR lifecycle.

## State mapping

| GitHub pull-request event | Linear state |
|---|---|
| `opened` (draft=false) | In Progress |
| `opened` (draft=true) | no-op (drafts sync on `ready_for_review`) |
| `ready_for_review`, `reopened`, `synchronize`, `edited` | In Review |
| `closed` with `merged=true` | Done |
| `closed` without merge | Canceled |

Issues are correlated by a `<!-- scope:<branch>::` description-marker **prefix** — this matches both the literal `<!-- scope:<branch>::auto-sync -->` marker this script writes when it creates an issue itself, and the `<!-- scope:<branch>::<prompt words> -->` marker the client-side session hook (`tools/linear_sync.py`) writes, so a branch's PR-lifecycle transitions land on whichever issue the local hook already created instead of spawning a duplicate. Queries are scoped to `LINEAR_PROJECT_NAME` (default `dev-harness-kit`) and paginated. Workflow-state IDs are resolved only within the project's team.

The event sync step is non-blocking so an external Linear outage cannot block a PR. The workflow's `smoke` command is strict and returns non-zero when the project or any required workflow state cannot be resolved.
