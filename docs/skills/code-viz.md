> [← Skills index](README.md) · [Project README](../../README.md)

# `code-viz`

**Category:** `audit` · **Alpha:** `state` · **Invocation:** `/dev-kit:code-viz` (human-invoked)

`code-viz` is a **generic** plugin-architecture visualizer: it walks **any target repo** (Claude Code plugin, MCP server, microservice, monorepo, framework) and emits one self-contained HTML page with **multi-level views**, **a domain pillar map**, and **per-skill workflow diagrams (including loop-back detection)**. All classification is filename/path heuristic — no hardcoded skill names, pipeline stages, or module roles.

**Header**: stat tiles per surface (skills / commands / hooks / GH actions / lib / bin / tools / MCP — counts change as the repo evolves, run `/dev-kit:code-viz` to see the live numbers) **+** stat tiles per domain pillar (`DB · Cloud · API · MCP · Skill · Hook · Network · Security · Build · Test · Storage · LLM` — same caveat, see the rendered HTML for the live values).

**Then** 4 inventory tables: skills (with `pillars` column), commands, hook scripts (event × matcher × script), GitHub Actions (file / triggers / jobs).

**Then** 6 abstraction levels + 1 cross-cutting pillar map (count depends on the target repo and `--top-skills N`; click each to expand):

| # | Level | Diagrams |
|---|---|---|
| 0 | **L0 Architecture overview** | Layered topology: user → skill frontmatter → Claude events → `lib/` + `tools/` + `bin/` → external (GH Actions, MCP, CLI) → back to user |
| 1 | **L1 Code level** | Directory tree + extension breakdown (fan-out, row-chunked) |
| 2 | **L2 Skill level** | Skill relationship graph (real `/dev-kit:` cross-refs only) + **per-skill workflow diagrams** for the top N (default 20, clamped [1,40]) user-invocable skills, each via 5-strategy extraction, with **loop-back arrows** where the skill body documents a real retry/iteration. Skills with no detectable workflow are listed as text chips, not visualized. |
| 3 | **L3 Hook event** | Claude event × matcher × script — hooks within one event are chained (real declared-order execution); different events are never connected to each other |
| 4 | **L4 Tools and Library layer** | `bin/` + `tools/` + `lib/` module inventory, fan-out from each directory root (no fabricated ordering between sibling modules) |
| 5 | **L5 External tools** | GitHub Actions triggers-to-jobs (each workflow's own real `on:`→`jobs:`, no cross-workflow edges) + MCP servers + third-party CLI invocations (fan-out) + a `sequenceDiagram` of the GH Actions gate workflow (PR → review/security fan-out → combined verdict), when a `needs:`-based gate job is detected |
| - | **Cross-cutting — Domain pillar map** | Which files fall under **DB · Cloud · API · MCP · Skill · Hook · Network · Security · Build · Test · Storage · LLM** (fan-out, independent buckets) |

## When to use it

- User types `/dev-kit:code-viz` and wants a **generic** plugin-architecture overview, not repo-specific.
- User wants **multi-level views** (architecture → code → skill → hook → tools → external).
- User wants diagrams **classified by domain pillar** (DB / Cloud / API / MCP / Skill / Hook / Network / Security / Build / Test / Storage / LLM).
- User wants **per-skill workflow extraction**, including which skills actually loop/retry, + drop-in PNG screenshots for a README.

## Iron Law flags

- `--target DIR` (default `$PWD`)
- `--out PATH` (default `/tmp/code-viz.html`)
- `--screenshots DIR` (optional; export each `pre.mermaid` as a PNG into DIR)
- `--top-skills N` (default 20, **clamped to [1, 40] in code** — how many user-invocable skills get a per-skill workflow diagram; the `IMPORTANT_SKILLS` priority list fills first, then alphabetical)

Read-only walk + new HTML in `/tmp` + optional PNGs. Validation failure is always a hard error (non-zero exit) — there is no lenient mode, so no `--strict` flag exists.

## Edges mean something (fan-out vs sequential)

Every edge represents a real relationship — never a layout artifact:

- **Sequential (chained arrows)**: only where a genuine before/after relationship exists — per-skill workflow phases, and hooks within one Claude event (they run in the array order declared in `hooks.json`).
- **Fan-out (no sibling edges)**: every pure inventory — `lib/`/`bin`/`tools/` modules, directory listing, extension breakdown, GitHub Actions workflows (each workflow's own trigger→jobs is real; different workflow *files* have no relationship to each other), MCP servers, third-party CLIs, domain pillar map. Root fans out directly to every item.
- **Row grouping beyond 5 items is a pure layout aid** — rendered as an invisible container (`fill:none,stroke:none`, blank title), never a visible box or "row N/M" label. Rows are stacked with Mermaid's invisible-link operator (`~~~`), never implying an execution order.

## Loop-back detection

Skills that actually loop get a **dotted, labeled back-edge** on their per-skill workflow diagram, not just a straight top-to-bottom chain:

1. **Explicit** — a step's own untruncated text contains `goto N` (e.g. babysit-pr's step 13: "otherwise `goto 1`") → back-edge to the exact referenced step, labeled `retry -> step N`.
2. **Implicit fallback** — no explicit `goto`, but the body uses recognized loop language (`3-cycle self-fix`, `ambiguity loop`, `retry loop`, `repeat until`, `safety_valve` cap) → generic last-step-loops-to-first-step edge (e.g. plan's ambiguity loop, build's self-fix guard).

`` ```python `` fenced code blocks are stripped before the implicit-keyword scan — otherwise a skill's own embedded source (including this skill analyzing its own SKILL.md) can self-match the detector's pattern-string literals. Bare/pseudocode ` ``` ` fences (e.g. babysit-pr's Algorithm block, which IS the loop description) are deliberately left in place.

## Generic by design (not repo-specific)

- **All classification is filename/path heuristic** via the embedded `PILLAR_PATTERNS` dict. No hardcoded skill names, pipeline stages, or module roles. Works on any plugin/repo.
- **Surfaces are optional**. Missing `skills/`, `hooks/`, `.github/`, `lib/`, etc. → section gracefully omitted, not crashed.
- **IMPORTANT_SKILLS priority list** — `plan`, `build`, `review`, `security`, `eval`, `inspect`, `prune`, `refactor`, `ci-setup`, `babysit-pr`, `ship`, `bootstrap`, `code-viz`, `report`, `token-analyzer` — always fill first, before alphabetical selection, up to `--top-skills`. The canonical list lives in `skills/code-viz/SKILL.md` (`IMPORTANT_SKILLS` constant); this doc mirrors it for reading convenience.
- **Domain pillars are keyword-matched** against each discovered path. A file matches DB if its name contains `db|sql|mongo|redis|postgres|sqlite|orm`; matches Cloud if it contains `aws|gcp|azure|k8s|docker|lambda|s3`; etc.
- **5-strategy per-skill cycle extraction** (in priority order):
  1. **F** — `## Categories` / `## Dimensions` / `## Audit areas` / `## Checks` bullet lists (security's OWASP A01–A10, inspect's 8 dims).
  2. **A** — `[N/M] LABEL → description` with arrow/em-dash variants (plan's `[1/5] frame`).
  3. **B** — `## Gate N/M — label` / `## Phase N — label` / `## Sub-stage N — label`.
  4. **C** — numbered list under `## Algorithm` / `## Behavior` / `## Pipeline` / `## Phases` / `## Cycle` (babysit-pr's 14-step `## Algorithm`).
  5. **D** — `## <SectionName>` headers as implicit phases (eval's `## Modes` → `## Rubric registry` → ...).
  - If all fail, the skill is listed as a text chip in "no explicit workflow detected" — no wasted diagram.

## Output

```text
[code-viz] target=<abs path>
[code-viz] discovered: <N> skills, <M> commands, <H> hooks, <W> GH workflows, <L> lib, <B> bin, <T> tools, 0 MCP
[code-viz] workflows visualized: <V> / <top-skills> top skills; <R> linear (listed as text)
[code-viz] pillar map: <per-pillar counts> (see rendered HTML for current values)
[code-viz] wrote /tmp/code-viz.html (X bytes, E mermaid diagrams)
[code-viz] exported N PNGs into <screenshots dir>
[code-viz] validation: 0 'Syntax error in text' | E/E svgs rendered | 0 pageerror | modal click OK
open /tmp/code-viz.html
```

`workflows visualized: A / B` — both numbers read from the SAME post-slice list the loop actually iterates (`visualized_skills = workflow_skills[:top_skills]`), so the printed stat always matches the rendered HTML — passing `--top-skills 1` prints `1 / 1`, not the untruncated `IMPORTANT_SKILLS` pool size.

## How it works

A single `python3 << 'PY' ... PY` heredoc embedded in `SKILL.md` (no `bin/`, `tools/`, or `lib/` companion needed):

1. **Walk** target recursively — collect all files, classify by directory + extension.
2. **Map** every discovered path to domain pillars via `PILLAR_PATTERNS`.
3. **Parse** optional surfaces: `skills/*/SKILL.md` (frontmatter + body), `commands/*.md`, `hooks/hooks.json`, `.github/workflows/*.yml`, `lib/*.py`, `bin/*.py`, `tools/*.py`, `.mcp.json`, `.claude/settings.json`, `.codex/settings.json`.
4. **Infer relationships** by scanning every `SKILL.md` + `commands/*.md` body for `/skill:<name>` / `/dev-kit:<name>` refs — real cross-references only.
5. **Extract cycles + loop-backs** from each `user_invocable: true` skill body (5-strategy extraction + `find_loop_back()`) — the top N (`visualized_skills`) get a `flowchart TD` per-skill workflow diagram, row-chunked and loop-annotated.
6. **Emit** `/tmp/code-viz.html` with stat tiles (surface + pillar), 4 inventory tables, 26+ diagrams (click-to-expand modal at natural viewBox size), CSS-variable light + dark theme, `theme: 'base'` + `themeVariables` for high-contrast Mermaid text, `@media print` for clean ⌘P → PDF.
7. **(Optional)** Export one PNG per diagram via `--screenshots DIR`.
8. **Validate** via Playwright headless — `body_syntax_error=False`, all `<pre class="mermaid">` produced an `<svg>`, no `pageerror`, click-to-expand modal opens. Hard-fail exit 1 on any failure — unconditionally, no lenient mode.

## Mermaid pitfalls (already burned into the validator)

- `<br/>` inside flowchart node shape labels — flaky in v10.9.1; use `\n` or `·`.
- `<n>`-style placeholders (e.g. `<name>`) — interpreted as HTML; use `[N]` or just text.
- `:` inside `stateDiagram-v2` transition labels (e.g. `lib/foo.py:130`) — `:` is the separator. **Replace with `line N` form.** Flowchart edge labels handle `:` fine.
- JS post-render sizing — Mermaid's async render loses the race. CSS-only with `!important` is more reliable.
- Raw `on:` in YAML GH-Actions — `yaml.safe_load` parses the bare key `on` as Python boolean `True`; always read via `data.get(True, data.get('on'))`.
- Unthemed Mermaid in dark mode — default theme paints light fills that disappear against a dark page; force `theme: 'base'` + explicit `themeVariables.primaryTextColor`.
- Long body snippets in node labels — keep labels ≤ 60 chars; strip backticks / arrows / quotes before interpolation.
- Regex escapes inside f-string raw patterns — `\\t`/`\\n` inside an `rf'...'` pattern is a literal backslash-t, NOT a tab; use single `\t`/`\n` or the pattern silently never matches (this broke Strategy F's `## Categories`/`## Dimensions` detection for an entire iteration before being caught).

## Hand-off

After `[code-viz] validation: 0 syntax-error / E/E svgs / modal click OK`, open `file:///tmp/code-viz.html`. Each diagram card has a `cursor: zoom-in` + `click to expand` hint; clicking shows the diagram at its natural `viewBox` size (the modal scrolls vertically if the diagram is taller than the viewport). Press `Escape` or click outside the card to close. Use the sticky top-nav to jump between levels.

For README inclusion: pass `--screenshots docs/diagrams` and the skill writes one PNG per diagram (`diagram-00.png` … `diagram-NN.png`) — reference those with `![](docs/diagrams/diagram-NN.png)`. The pillar tiles in the header show at a glance which domain pillars the target spans.

## Update history

- **v5 (current)** — Loop-back detection (explicit `goto N` + implicit keyword fallback, dotted labeled edges). Fixed fan-out vs sequential edge correctness (pure inventories no longer draw false sibling-to-sibling relationships; a GH Actions rendering bug drew edges from an undeclared phantom node between unrelated workflows). Removed visible row-grouping boxes (subgraphs kept for layout, styled invisible). Fixed `visualized_skills` display-vs-loop-slice mismatch (`--top-skills 1` used to print the untruncated pool size instead of 1). Removed the dead `--strict` flag (validator always hard-fails; the flag never gated anything). Added `top_skills` clamp to [1, 40] in code (was documented but unenforced). Confirmed byte-identical output across 3 consecutive runs (same input, minus timestamp).
- **v4** — Row-chunking: items beyond 5 per row wrap to new rows instead of one long horizontal line.
- **v3** — Strategy F (domain-content extraction): parses `## Categories` / `## Dimensions` / `## Audit areas` / `## Checks` sections with bolded-bullet items. Skills with no extractable workflow listed as text chips.
- **v2** — Generic multi-level + pillar map (6 abstraction levels + 1 cross-cutting).
- **v1** — Directory visualizer (file counts + extension tables).
