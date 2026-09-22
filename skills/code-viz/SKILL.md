---
name: code-viz
category: audit
description: 0-arg generic plugin-architecture visualizer. Walks any target repo, emits self-contained HTML with multi-level views (architecture / code / skill / hook / tools-lib / external) + domain pillar map (DB · Cloud · API · MCP · Skill · Hook · Network · Security · Build · Test · Storage · LLM) + per-skill workflows (multi-strategy extraction incl. ## Categories/Dimensions for security/inspect) + GH Actions gate workflow + optional per-diagram PNG export.
alpha: state
when_to_use:
  - User types /dev-kit:code-viz and wants a generic plugin-architecture overview, not repo-specific
  - User wants multi-level views (architecture → code → skill → hook → tools → external) + per-skill workflows
  - User wants diagrams classified by domain pillar (DB / Cloud / API / MCP / Skill / Hook / Network / Security / Build / Test / Storage / LLM)
  - User wants GH Actions gate workflow (review/security/gate verdict) sequence visualized
  - User wants per-skill workflow extraction + drop-in PNG screenshots for a README
allowed-tools: Read Bash Glob Write
disallowed-tools: WebFetch Edit NotebookEdit
model: sonnet
user-invocable: true
disable-model-invocation: false
---

# /dev-kit:code-viz — generic plugin-architecture visualizer

## What it does

`tools/code_viz.py` walks **any target repo** (Claude Code plugin, MCP server,
microservice, monorepo, framework) and emits `/tmp/code-viz.html` containing
**multi-level views** + **a domain pillar map**:

### 6 abstraction levels + 1 cross-cutting view

| # | Level | What it shows |
|---|---|---|
| 0 | **L0 Architecture overview** | Layered topology: external → user surface → events → libs → scripts → external CI |
| 1 | **L1 Code level** | Directory tree, extension breakdown, key files |
| 2 | **L2 Skill level** | Skills + commands inventory; **per-skill workflow diagrams** for top N user-invocable skills via **multi-strategy extraction** (incl. `## Categories`/`## Dimensions` domain content for security/inspect). Skills with no detectable workflow are listed as text, not visualized. |
| 3 | **L3 Hook event** | Claude event × matcher × script matrix |
| 4 | **L4 Tools and Library layer** | `bin/` + `tools/` + `lib/` module inventory |
| 5 | **L5 External tools** | GitHub Actions triggers-to-jobs + MCP servers + third-party CLI invocations + **GH Actions gate workflow sequence** (PR → review/security fan-out → combined verdict) |
| - | **Cross-cutting — Domain pillar map** | Which files fall under DB / Cloud / API / MCP / Skill / Hook / Network / Security / Build / Test / Storage / LLM |

Each diagram is bounded to `72vh`; click any card to expand; ESC / backdrop / close-button dismisses. CSS variables drive both light + dark themes; Mermaid uses `theme: 'base'` + explicit `themeVariables` for high-contrast node text. `@media print` rules hide nav/modal so ⌘P produces a clean README-ready PDF.

## Edges mean something (fan-out vs sequential)

Every edge in every diagram represents a real relationship in the target repo — never a layout artifact:

- **Sequential (chained arrows)** — used ONLY where a genuine before/after relationship exists: per-skill workflow phases (step 2 really does run after step 1), and hooks within one Claude event (they execute in the array order declared in `hooks.json`). Root connects to the first item; each subsequent item chains from the previous.
- **Fan-out (no sibling edges)** — used for every pure inventory: `lib/`/`bin`/`tools/` modules, directory listing, extension breakdown, GitHub Actions workflows (each workflow's own `on:` trigger → its own `jobs:` is real; different workflow files have no relationship to each other), MCP servers, third-party CLI invocations, and the domain pillar map. Root fans out directly to every item — no fabricated ordering between siblings that don't actually depend on each other.

**Row grouping is a layout aid, not a container.** When an inventory exceeds 5 items, rows are still grouped (5 per row) so the diagram doesn't render as one long horizontal line — but the grouping renders as invisible (`fill:none,stroke:none`, blank title): no visible box, no "row N/M" label. Consecutive rows are linked with Mermaid's invisible-link operator (`~~~`) purely to force vertical stacking, never implying an execution order between an unordered inventory's rows.

## Loop-back detection (real retry/iteration engineering, not decoration)

Skills whose SKILL.md documents an actual loop get a dotted, labeled back-edge on their per-skill workflow diagram — not just a straight top-to-bottom chain:

1. **Explicit** — a step's own untruncated text contains `goto N` (e.g. babysit-pr's step 13 says "otherwise `goto 1`" verbatim) → the back-edge points to the exact referenced step, labeled `retry -> step N`.
2. **Implicit fallback** — no explicit `goto`, but the skill body uses recognized loop language (`3-cycle self-fix`, `ambiguity loop`, `retry loop`, `repeat until`, `safety_valve` cap, etc.) → the last step loops back to the first step, since that is what "the process repeats" means absent a more specific target.

`` ```python `` fenced code blocks are stripped before the implicit-keyword scan
(bare/pseudocode ` ``` ` fences are not) so source literals do not look like
workflow prose.

## Iron Law (no exceptions)

**0-arg default OK. Hidden flags:**
- `--target DIR` (default `$PWD`)
- `--out PATH` (default `/tmp/code-viz.html`)
- `--screenshots DIR` (optional; export each `pre.mermaid` as a PNG into DIR)
- `--top-skills N` (default 20, clamped to [1, 40] — how many user-invocable skills get a per-skill workflow diagram; IMPORTANT skills always included)

The skill does **not modify** the target — read-only walk + new HTML in `/tmp` + optional PNGs. Validation failure is always a hard error (non-zero exit) — there is no lenient mode.

## Generic by design (not repo-specific)

- **All classification is filename/path heuristic**. No hardcoded skill names, pipeline stages, or module roles. Works on any plugin/repo.
- **Surfaces are optional**. Missing `skills/`, `hooks/`, `.github/`, `lib/`, etc. → section gracefully omitted, not crashed.
- **IMPORTANT_SKILLS priority list** — `plan`, `build`, `review`, `security`, `eval`, `inspect`, `prune`, `refactor`, `ci-setup`, `babysit-pr`, `ship`, `bootstrap`, `code-viz`, `report`, `token-analyzer` always get workflow diagrams before alphabetical selection.
- **Skills without an extractable workflow** are listed as text in a "no workflow detected" section rather than visualized (no empty diagrams).

## Per-skill cycle extraction strategies (5 fallbacks)

1. **Strategy F — `## Categories` / `## Dimensions` / `## Audit areas` / `## Checks`** with bullet-list items (e.g. security's `## Categories` listing A01–A10, inspect's `## Dimensions` listing dead/dup/smell/.../slop). This runs first because it's the most semantically meaningful.
2. **Strategy A — `[N/M] LABEL`** with separators `→ | -> | — | – | - ` (e.g. `plan`'s `[1/5] frame — goal + target user`).
3. **Strategy B — `## Gate N/M — label`** / `## Phase N — label` / `## Sub-stage N — label`.
4. **Strategy C — numbered list under** `## Algorithm` / `## Behavior` / `## Pipeline` / `## Phases` / `## Cycle` (e.g. `babysit-pr`'s 14-step `## Algorithm`, `review`'s `## Scope` numbered list).
5. **Strategy D — `## <SectionName>` headers** as implicit phases (e.g. `eval`'s `## Modes` → `## Rubric registry` → `## Cross-validate` → `## Verdict` → `## Output`).

If all fail, the skill is added to a **"no explicit workflow detected"** text list (not a Mermaid diagram).

## Mermaid pitfalls (already burned into the validator)

- `<br/>` inside flowchart node shape labels — flaky in v10.9.1; use `\n` or `·`.
- `<n>`-style placeholders (e.g. `<name>`) — interpreted as HTML; use `[N]` or just text.
- `:` inside `stateDiagram-v2` transition labels (e.g. `lib/foo.py:130`) — `:` is the separator. **Replace with `line N` form.** Flowchart edge labels handle `:` fine.
- JS post-render sizing — Mermaid's async render loses the race with `setTimeout`/`load`. CSS-only with `!important` is more reliable.
- Raw `on:` in YAML GH-Actions — `yaml.safe_load` parses the bare key `on` as Python boolean `True`; always read via `data.get(True, data.get('on'))`.
- Unthemed Mermaid in dark mode — default theme paints light fills that disappear against a dark page; force `theme: 'base'` + explicit `themeVariables.primaryTextColor`.
- Long body snippets in node labels — keep labels ≤ 60 chars; strip backticks / arrows / quotes before interpolation.

## Verifier (must pass before declaring done — Playwright headless)

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    errs = []
    page.on('pageerror', lambda e: errs.append(str(e)))
    page.goto(f'file://{out}', wait_until='networkidle')
    page.wait_for_timeout(1500)
    body = page.evaluate("() => document.body.innerText")
    syntax_error = 'Syntax error in text' in body
    svgs = page.query_selector_all('pre.mermaid svg')
    page.query_selector('pre.mermaid').click()
    page.wait_for_timeout(300)
    modal_open = page.evaluate('() => document.getElementById("mermaid-modal").classList.contains("open")')
    b.close()
assert not syntax_error, 'mermaid render failed'
assert len(svgs) == expected_count, f'{len(svgs)}/{expected_count} mermaid blocks rendered'
assert not errs, f'pageerror: {errs}'
assert modal_open, 'click-to-expand did not open modal'
```

Hard-fail exit code 1 if any of these fail.

## Output (printed to stdout)

```
[code-viz] target=<abs path>
[code-viz] discovered: <N> skills, <M> commands, <H> hooks, <W> GH workflows, <L> lib, <B> bin, <T> tools, <E> MCP
[code-viz] pillar map: <per-pillar counts>
[code-viz] wrote /tmp/code-viz.html (X bytes, E mermaid diagrams)
[code-viz] exported N PNGs into <screenshots dir>
[code-viz] validation: 0 'Syntax error in text' | E/E svgs rendered | 0 pageerror | modal click OK
open /tmp/code-viz.html
```

## Implementation

The executable SSOT is `tools/code_viz.py`; the skill only supplies the
operator contract. It accepts `--target=DIR`, `--out=PATH`,
`--screenshots=DIR`, and `--top-skills=N` (1..40). It is read-only for
the target and hard-fails when Playwright validation fails.

```bash
python3 tools/code_viz.py --target=. --out=/tmp/code-viz.html
```

Keep classification, extraction, rendering, and validation changes in
`tools/code_viz.py`; keep this file limited to the invocation contract.

## Verification

The tool must print `validation: 0 syntax-error`, render every Mermaid block,
report no page errors, and confirm the modal click. Any validation failure is
non-zero. `tests/test_code_viz_skill.py` compiles and runs the tool against a
synthetic repository.

## Next step

Open the printed HTML path. Pass `--screenshots=DIR` when README-ready PNGs
are required.
