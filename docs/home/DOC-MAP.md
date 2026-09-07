# Doc map (categorized)

Full categorized inventory of every topic doc shipped in this repo.

Most topic docs ship in **both** `.md` and `.html` formats — HTML for browsing
(sticky topnav, dark/light theme auto-switch, copy-able code blocks), MD for
grepping and native GitHub rendering. The landing page and the skill index are
Markdown-only. Where a Korean sibling exists, it is linked in the same row.

| Topic | HTML | MD | 한국어 | What you get |
|---|---|---|---|---|
| Why + value + quickstart | — | [`docs/home/00-index.md`](00-index.md) | [`00-index.ko.md`](00-index.ko.md) | Beginner landing — read first |
| STAGES (what each loop step owns) | [`docs/stages/STAGES.html`](../stages/STAGES.html) | [`docs/stages/STAGES.md`](../stages/STAGES.md) | [`STAGES.ko.md`](../stages/STAGES.ko.md) | bootstrap → plan → valuate → build → review → security → ship |
| CI install (run dev-kit CI elsewhere) | [`docs/quality/ci-setup.html`](../quality/ci-setup.html) | [`docs/quality/ci-setup.md`](../quality/ci-setup.md) | [`ci-setup.ko.md`](../quality/ci-setup.ko.md) | `branch-policy` + validate + test + auto-fix workflows |
| CI template drift refresh (selective) | — | [`docs/quality/ci-update.md`](../quality/ci-update.md) | — | Detect + selectively apply dev-kit ⇄ consumer template drift; 4-state per-file classification with backup-before-overwrite (no `--force` blast) |
| Maintenance gate (PR-only quality) | [`docs/quality/maintenance-gate.html`](../quality/maintenance-gate.html) | [`docs/quality/maintenance-gate.md`](../quality/maintenance-gate.md) | — | 20-checkbox rubric enforced in `.github/workflows/maintenance.yml` |
| Runtime portability (Claude Code ↔ Codex) | [`docs/architecture/RUNTIME-PORTABILITY.html`](../architecture/RUNTIME-PORTABILITY.html) | [`docs/architecture/RUNTIME-PORTABILITY.md`](../architecture/RUNTIME-PORTABILITY.md) | [`RUNTIME-PORTABILITY.ko.md`](../architecture/RUNTIME-PORTABILITY.ko.md) | The contract both runtimes honor so plugin.json means the same thing |
| Visualization (code-viz output + mechanics) | — | [`docs/architecture/visualization.md`](../architecture/visualization.md) | — | The four per-skill Mermaid diagrams + GH Actions gate workflow + per-skill extraction / loop-back / edge-semantics rules that `code-viz` follows |
| Naming convention (SSOT) | [`docs/naming/NAMING.html`](../naming/NAMING.html) | [`docs/naming/NAMING.md`](../naming/NAMING.md) · [ADR-0010](../adr/ADR-0010-naming-convention.md) | [`NAMING.ko.md`](../naming/NAMING.ko.md) | Why a hook is `bash-guard.sh`, not `bashHook.sh` |
| Pre-implementation gate | [`docs/planning/PRE-IMPL-CHECK.html`](../planning/PRE-IMPL-CHECK.html) | [`docs/planning/PRE-IMPL-CHECK.md`](../planning/PRE-IMPL-CHECK.md) | — | 9 questions before code |
| Cost & risk | [`docs/quality/COST-ANALYSIS.html`](../quality/COST-ANALYSIS.html) | [`docs/quality/COST-ANALYSIS.md`](../quality/COST-ANALYSIS.md) | — | Token ceilings, cost-gate trailer format |
| Team adoption | [`docs/adoption/team-adoption.html`](../adoption/team-adoption.html) | [`docs/adoption/team-adoption.md`](../adoption/team-adoption.md) | — | Why a single maintainer and a 20-person team adopt the harness differently |
| Hook coverage gaps (P4 Bucket B audit) | [`docs/hooks/hook-coverage-gaps.html`](../hooks/hook-coverage-gaps.html) | [`docs/hooks/hook-coverage-gaps.md`](../hooks/hook-coverage-gaps.md) | — | Which hook events are wired vs. which aren't, per runtime |
| Hook reference (enforcement + inventory) | — | [`docs/hooks/HOOK-REFERENCE.md`](../hooks/HOOK-REFERENCE.md) | — | Two hook tables: by what they guard, and by event |
| Linear PR sync (PR-event → Linear state) | — | [`docs/tools/LINEAR-PR-SYNC.md`](../tools/LINEAR-PR-SYNC.md) | — | `.github/workflows/linear-pr-sync.yml` + `tools/linear_pr_sync.py`; non-blocking, drafts skipped |
| ACP dispatch (M-tier architecture) | [`docs/architecture/ACP-DISPATCH.html`](../architecture/ACP-DISPATCH.html) | [`docs/architecture/ACP-DISPATCH.md`](../architecture/ACP-DISPATCH.md) | [`ACP-DISPATCH.ko.md`](../architecture/ACP-DISPATCH.ko.md) | How Model-tier agents find and dispatch to Capability-tier skills |
| ACP (Agent Coordination Protocol) | [`docs/architecture/acp-harness.html`](../architecture/acp-harness.html) | [`docs/architecture/acp-harness.md`](../architecture/acp-harness.md) | [`acp-harness.ko.md`](../architecture/acp-harness.ko.md) | The wire-format ACP uses to talk between agents |
| Skill reference | — | [`docs/skills/README.md`](../skills/README.md) | [`README.ko.md`](../skills/README.ko.md) | All skills with category + α classification |
| Workflow scenarios (interrupt / skip) | — | [`docs/workflow/WORKFLOW-SCENARIOS.md`](../workflow/WORKFLOW-SCENARIOS.md) | — | What to do when plan→build stops, plan drifts mid-build, etc. |
| Token efficiency + research | — | [`docs/observability/token-efficiency.md`](../observability/token-efficiency.md) | — | The "every claim cites its source / cost" bundle |
| Metric & gate skills | — | [`docs/observability/metrics.md`](../observability/metrics.md) | — | The five skill families that emit a number you can act on — `maintenance` (gate), `ci-doctor` (pre-flight), `security-metrics` (static triage), `evaluate` (post-hoc judge), `harness-effectiveness` (sub-second reducer) |
| Session monitor | — | [`docs/observability/session-monitor.md`](../observability/session-monitor.md) | — | Cross-terminal session picker + resume |
| Decision records | — | [`docs/adr/`](../adr/) | — | Locked ADRs (historical; English only) |
| Repo map | [`docs/repo/REPOSITORY-MAP.html`](../repo/REPOSITORY-MAP.html) | [`docs/repo/REPOSITORY-MAP.md`](../repo/REPOSITORY-MAP.md) | — | Where each component lives in the tree |

## Where to start

- **Five-minute reader:** [`docs/home/00-index.md`](00-index.md) — sections 1–3 (why, quickstart, value).
- **Wiring the CI in a new repo:** [`docs/quality/ci-setup.md`](../quality/ci-setup.md).
- **Resuming a flow that broke:** [`docs/workflow/WORKFLOW-SCENARIOS.md`](../workflow/WORKFLOW-SCENARIOS.md).
- **Auditing cost or finding evidence for a claim:** [`docs/observability/token-efficiency.md`](../observability/token-efficiency.md).
- **Picking the right metric or gate for a PR:** [`docs/observability/metrics.md`](../observability/metrics.md) — when to run `maintenance` vs `ci-doctor` vs `security-metrics` vs `evaluate` vs `harness-effectiveness`.
- **Picking up a Claude Code / Codex session from a new shell:** [`docs/observability/session-monitor.md`](../observability/session-monitor.md).