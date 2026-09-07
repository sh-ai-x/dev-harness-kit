# Visualization — how workflows become diagrams

`/dev-kit:code-viz` walks the plugin and emits one self-contained HTML with
multi-level views (architecture → code → skill → hook → tools → external) plus
a per-skill workflow extraction. The patterns it uses are reusable — same
approach to render GH Actions pipelines, multi-phase repair loops, or any
other long-running process with discrete phases.

This document is the full reference for the diagrams emitted by `code-viz` and
the rules the extractor follows. Use it when you want to author a new
per-skill workflow, audit how a diagram was assembled, or extend `code-viz`
to a new plugin.

## Diagrams

The four Mermaid blocks below are the compact, source-backed examples. The
generated HTML remains the canonical interactive view for the full
architecture → code → skill → hook → tools → external levels and per-skill
workflow extraction.

### `/dev-kit:plan` — 5 gates with ambiguity loop

The plan skill walks an idea through five gates before emitting a PRD.
The dotted back-edge from `emit` to `frame` is the ambiguity loop: when the
emitted PRD fails its own acceptance criteria, the workflow re-enters at
`frame` for one more pass.

```mermaid
flowchart TD
  plan([plan])
  frame["frame<br/>goal + target user + 1-line situation"]
  validate["validate<br/>evidence (>=3 sources) + value_score + ambiguity score"]
  non_goals["non-goals<br/>3+ non-goals with rationale + breach response"]
  decompose["decompose<br/>phases/name/index.json + stepN.md (per-step)"]
  emit["emit<br/>PRD.md 6-section DoD pass + hand-off"]
  plan --> frame
  frame --> validate
  validate --> non_goals
  non_goals --> decompose
  decompose --> emit
  emit -. ambiguity loop .-> frame
```

### `/dev-kit:security` — OWASP Top-10 (A01–A10)

Eleven parallel subagents, one per OWASP 2025 category, return
evidence-backed findings; a verification pass confirms or rejects each
before a per-category breakdown table + verdict.

```mermaid
flowchart TD
  sec([security]) --> A01
  A01["A01 · Broken Access Control<br/>IDOR, path traversal, missing authz"] --> A02
  A02["A02 · Security Misconfiguration<br/>default creds, debug mode on, verbose errors"] --> A03
  A03["A03 · Software Supply Chain Failures<br/>vulnerable deps, untrusted CI artifacts"] --> A04
  A04["A04 · Cryptographic Failures<br/>weak hashes (MD5/SHA1), no TLS, hardcoded keys"] --> A05
  A05["A05 · Injection<br/>SQL, command, template, XSS, LDAP"] --> A06
  A06["A06 · Insecure Design<br/>no rate limit, client-side trust, missing threat model"] --> A07
  A07["A07 · Authentication Failures<br/>weak passwords, missing MFA, credential stuffing"] --> A08
  A08["A08 · Software/Data Integrity Failures<br/>unsigned updates, unsafe deserialization, CI/CD pipeline attack"] --> A09
  A09["A09 · Security Logging and Alerting Failures<br/>no audit trail, missing alerts, log injection"] --> A10
  A10["A10 · Mishandling Exceptional Conditions<br/>bare except, fail-open defaults, panic-driven errors"]
```

### `/dev-kit:babysit-pr` — bounded repair loop with retry back-edge

The 14-step repair state machine with its pre-loop opt-out check and an
outcome checkpoint. The dotted back-edge from `INCREMENT` to `OPT-OUT CHECK`
is the bounded-iteration loop that re-polls CI until verdicts flip green.

```mermaid
flowchart TD
  bp([babysit-pr]) --> s0
  s0["step 0 · OPT-OUT CHECK<br/>if --operator-is-only-human, defer to bypass"] --> s1
  s1["step 1 · SNAPSHOT<br/>fetch PR_NUMBER, REVIEW_VERDICT, CHECKS"] --> s2
  s2["step 2 · TERMINATE<br/>if APPROVED + every check green, exit 0"] --> s3
  s3["step 3 · CLASSIFY<br/>bucket blockers: CI failing / pending / review"] --> s4
  s4["step 4 · WAIT<br/>if any check pending and no failures, sleep 30s"] --> s5
  s5["step 5 · FETCH LOGS<br/>gh run view --log-failed for each failing check in changed"] --> s6
  s6["step 6 · DIAGNOSE<br/>identify ONE root cause per failing check"] --> s7
  s7["step 7 · APPLY FIX<br/>modify code; one logical change per iteration"] --> s8
  s8["step 8 · VERIFY LOCAL<br/>HARD GATE, re-run the same failing command"] --> s8o
  s8o["step 8.5 · OUTCOME<br/>persist progress and recovery state"] --> s9
  s9["step 9 · COMMIT<br/>git add specific paths + conventional commit"] --> s10
  s10["step 10 · PUSH<br/>git push origin HEAD"] --> s11
  s11["step 11 · LOG<br/>append one line to .dev-kit/babysit.log"] --> s12
  s12["step 12 · SLEEP<br/>gh pr checks --watch or sleep 20s"] --> s13
  s13["step 13 · SAVE STATE<br/>overwrite .dev-kit/babysit-checks.json"] --> s14
  s14["step 14 · INCREMENT<br/>iter = iter + 1; cap at MAX_ITERS"]
  s14 -. retry -> step 0 .-> s0
```

### Repair state machine — observation → patch → verify → push

The general repair state machine: observe, reproduce, patch, verify, then
either progress or escalate through bounded repair PRs before handing off
to a human merge. GitHub's `auto-fix-pr` is only an event adapter into this
same repair state; it is not a second user-facing workflow.

```mermaid
flowchart TD
  OBSERVE[observe checks + findings] --> REPRODUCE[reproduce failure]
  REPRODUCE --> PATCH[one minimal patch]
  PATCH --> VERIFY[focused + full verification]
  VERIFY --> PROGRESS{measurable progress?}
  PROGRESS -->|yes| OBSERVE
  PROGRESS -->|no, original PR| R1[repair PR 1]
  R1 --> OBSERVE
  PROGRESS -->|no, repair PR 1| R2[repair PR 2]
  R2 --> OBSERVE
  PROGRESS -->|no, repair PR 2| EX[exception evidence bundle]
  VERIFY -->|all required gates pass| MERGE[human merge hand-off]
```

## GH Actions gate workflow

The shipped `review.yml` defines a PR → review/security fan-out → gate verdict
sequence. code-viz emits this as a `sequenceDiagram`; it renders inline
directly from a fenced ```mermaid``` block:

```mermaid
sequenceDiagram
  participant Dev as Developer
  participant PR as Pull Request
  participant GH as GitHub Actions
  participant R as /dev-kit:review
  participant S as /dev-kit:security
  participant G as gate job
  Dev->>PR: open / synchronize / reopen
  PR->>GH: pull_request event
  GH->>R: spawn review job
  GH->>S: spawn security job (parallel)
  R->>R: 3-dim fan-out (correctness + security + architecture)
  S->>S: OWASP A01-A10 fan-out
  R-->>GH: review verdict + per-line findings
  S-->>GH: security verdict + findings
  GH->>G: gate job (needs review + security)
  G->>G: touch-probe + L3 evidence gate
  G->>G: aggregate combined verdict
  G-->>PR: post verdict as PR comment
  alt verdict = Approve
    PR->>Dev: mergeable
  else verdict = Block
    PR->>Dev: changes requested
  end
```

## What the visualizer ships

The diagrams above are the same shapes `code-viz` emits. The HTML
output it writes to `/tmp/code-viz.html` is the multi-level view that
folds all 6 abstraction levels + the GH Actions gate into a single
self-contained page. The screenshot below is the L0 architecture
overview rendered from that HTML — what `/dev-kit:code-viz` looks
like in a browser:

<img src="../screenshots/code-viz/diagram-00.png" alt="L0 Architecture overview rendered by /dev-kit:code-viz — user → skills/commands → hooks → lib/tools/bin → external" width="360" />

> Regenerate the code-viz image by running `/dev-kit:code-viz --screenshots=docs/screenshots/code-viz --top-skills=20` (the generator is the script embedded in `skills/code-viz/SKILL.md`). The code-viz L0 image and the separately authored Archidraw Overall Skill Map are both committed exports; update each from its own source when the corresponding architecture changes. The per-skill workflows render inline as `mermaid` blocks and need no PNG export.

## Per-skill workflow extraction

For each user-invocable skill, code-viz tries five strategies in order — first
match wins — and falls back to the next only if the previous yields fewer than
two items:

1. **Domain-content sections** — `## Categories`, `## Dimensions`, `## Audit
   areas`, `## Checks` with bolded bullets (e.g. security's A01–A10, inspect's
   8 dimensions).
2. **`[N/M] LABEL → desc`** — used by `plan`'s 5-step framing.
3. **`## Gate N/M — label` / `## Phase N — label`** — numbered gates.
4. **Numbered list under `## Algorithm`** — used by `babysit-pr`'s 14-step
   repair loop, plus its explicit pre-loop and outcome checkpoint.
5. **`## <SectionName>` headers** as implicit phases.

Skills without an extractable workflow are listed as text chips in a "no
workflow detected" section, never visualized as empty diagrams.

## Loop-back detection

Workflows that loop get a dotted, labeled back-edge — not just a straight
top-to-bottom chain:

- **Explicit** — a step's own text contains `goto N` (e.g. babysit-pr's step
  13 says "otherwise `goto 1`"). The back-edge points to the referenced step,
  labeled `retry -> step N`.
- **Implicit fallback** — no explicit goto, but the skill body uses recognized
  loop language (`3-cycle self-fix`, `repeat until`, `safety_valve` cap, …).
  The last step loops back to the first step, since "the process repeats" is
  the only sensible default.

A `python` fenced code block is stripped before the implicit-keyword scan — a
skill's own source code (including this one) can match the detector's pattern
strings as if they were prose describing a real loop.

## Edge semantics

Every edge in every diagram represents a real relationship — never a layout
artifact:

- **Sequential (chained arrows)** — used only where a real before/after
  relationship exists: per-skill workflow phases, hooks within one Claude
  event (they execute in declared array order).
- **Fan-out (no sibling edges)** — used for every pure inventory: `lib/`,
  `bin/`, `tools/` modules, directory listing, GitHub Actions workflows, MCP
  servers, third-party CLI invocations, and the domain pillar map. Root fans
  out to every item directly; no fabricated ordering between siblings that
  don't actually depend on each other.

Row grouping beyond 5 items renders as a borderless, fill-less subgraph with a
blank title — a layout aid, never a container. Consecutive inventory rows are
linked with Mermaid's invisible operator (`~~~`) to force vertical stacking
without implying an execution order.

---

For the live contract (frontmatter fields, generator invocation, and how the
visualizer handles new skill types), see
[`docs/skills/code-viz.md`](../skills/code-viz.md).
