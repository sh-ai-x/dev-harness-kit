---
name: review-code
description: Parallel multi-dimension code review with a false-positive filter. Fans out to specialized subagents (MVP dimensions: correctness, security, architecture) that run at the same time and return structured, evidence-backed findings; a verification pass then confirms or rejects each candidate before rendering per-line inline comments plus one PR-style summary with a verdict. Use when the user types /review-code, or asks to review code, review a diff, review a PR, or "check this before merge".
when_to_use: |
  - User types /review-code [paths] [--diff] [--diff --staged] [--fast]
  - User asks to "review this code", "review the diff", "review the PR", "check this before merge"
  - User wants a structured, severity-ranked, low-noise review rather than an ad-hoc read-through
allowed-tools: Read Grep Glob Bash Agent
disable-model-invocation: false
model: opus
---

## Provider (defaults to MiniMax, Anthropic-compatible)

This skill follows whatever provider the GitHub Action configures via env vars.
Default is **MiniMax-M3[1m]** at `ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`.
Opt-in to real Claude Code via `REVIEW_PROVIDER=anthropic`.

# review-code — parallel multi-dimension code review

Review target code by fanning out to **specialized dimension experts that run in parallel**,
one subagent per dimension, then run a **verification pass** that filters out false positives.

**MVP dimensions (3):** `correctness`, `security`, `architecture`.

**Guiding principle — precision over recall.** A review that cries wolf gets ignored.
**Every rendered finding must be real and demonstrable.**

---

## Step 1 — Resolve scope

1. No paths (common) → whole project directory.
2. `--diff` → diff vs default branch.
3. `--diff --staged` → working-tree changes only.
4. `--fast` → skip verifier (deterministic filter only).

Filter to source files. Empty list → tell user, stop. >~40 files → narrow subset.

On diff run, also capture changed hunks (`git diff -U0`). Experts must only flag issues
**introduced by the changed lines**, not pre-existing code.

---

## Step 2 — Fan out to the experts (THE PARALLEL STEP)

> **Issue all 3 `Agent` calls inside ONE assistant message** so they run concurrently.
> Separate messages run sequentially and defeat the purpose.

Each call: `subagent_type: "general-purpose"`, `model: "sonnet"`, `run_in_background: false`.

### Shared contract (prepend to every expert prompt)

```
You are a code-review expert for ONE dimension: <DIMENSION>. Read each file and report
ONLY real, demonstrable issues in your dimension. Precision matters more than
completeness — a false positive is worse than a missed nit.

Files to review (read each one):
<file list, absolute paths>
[diff run only] Only report issues in these changed hunks; ignore pre-existing code:
<hunk list>

MANDATORY per finding:
- failure_scenario: a CONCRETE trigger — specific inputs/state that lead to wrong
  output, crash, or exploit. If you cannot write one, the issue is speculative → DROP.
- confidence: high | medium | low — your certainty the issue is real AND reachable.

DO NOT report:
- Style, naming, formatting preferences with no functional impact.
- Hypothetical issues with no reachable trigger.
- "Missing" validation when a visible guard, type, or caller already covers it.
- Defensive-programming suggestions that aren't a real defect.
- Anything outside your dimension, or (on a diff run) outside the changed hunks.
- A weaker restatement of a more fundamental issue you're also reporting.

Severity: critical (breaks behavior / exploitable / data loss → blocks merge) ·
major (real defect) · minor (non-blocking improvement) · nit (trivial).

Return ONLY a fenced ```json array:
[{
  "file": "<absolute path>",
  "line": <1-indexed anchor int>,
  "dim": "<DIMENSION>",
  "severity": "critical|major|minor|nit",
  "confidence": "high|medium|low",
  "title": "<short imperative title>",
  "tldr": "<one line: what's wrong and why it matters>",
  "failure_scenario": "<concrete inputs/state → wrong output/crash/exploit>",
  "good": "<what is done well near this code, or null>",
  "fix": "```<lang>\n<corrected code snippet>\n```"
}]
Return [] if you find nothing real. Prefer 2 solid findings over 8 speculative ones.
```

### Dimension charters

- **correctness** — logic errors, edge-case/boundary/null handling, off-by-one, state
  transitions, error-handling gaps, race conditions, API/contract misuse, wrong return values.
- **security** — `eval` / `new Function` / `os.system` / `child_process.exec` /
  `pickle` / `yaml.load` / `dangerouslySetInnerHTML` / SSRF / IDOR / hardcoded
  credentials / weak crypto. READ surrounding code; only report if reachable.
- **architecture** — module boundaries, coupling, layering, leaky abstractions,
  duplication, God objects, poor extensibility. Report only structural problems
  with concrete maintenance/scaling impact.

---

## Step 3 — Verify (false-positive filter)

Parse each expert's JSON. Then:

### 3a. Deterministic filter
Drop if:
- Missing/empty `failure_scenario`.
- `confidence: low` AND severity is `minor`/`nit`.
- (Diff run only) anchor outside changed hunks.

### 3b. Dedupe
Same `file+line+theme` → keep higher severity. Cross-dimension root cause → collapse.

### 3c. Verifier pass (default; skipped with `--fast`)
Spawn one verifier subagent (`general-purpose`, `model: "sonnet"`). Give it surviving
candidates + file list with this prompt:

```
You are a strict verifier. RE-READ the cited code and decide if each candidate is REAL.
Try hard to REFUTE. Return only:
[{ "id": <index>, "verdict": "CONFIRMED|PLAUSIBLE|REJECTED",
   "reason": "<one line>" }]
- CONFIRMED: you executed failure_scenario against the code and it holds.
- PLAUSIBLE: likely real but can't fully confirm from given scope.
- REJECTED: code already handles it, or scenario doesn't trigger.
```

Drop every REJECTED. Keep CONFIRMED + PLAUSIBLE.

### 3d. Sort
By severity (critical→nit), CONFIRMED before PLAUSIBLE, then file, then line.

---

## Step 4 — Render

### Layer 2 — PR summary (exactly one, at top)

```
## Review summary

**Verdict:** <Blocked | Changes Requested | Approve>
**Severity:** 🔴 <n>  🟠 <n>  🟡 <n>  ⚪ <n>
**Precision:** <M> findings shown · <K> filtered as false positives/low-signal

**Walkthrough:** <2-3 lines>

**Strengths:**
- <notable good points>

**Blocking findings (critical + major only):**
- [🔴 critical · CONFIRMED] <title> — path:line

**Next actions:**
- [ ] <short checklist>
```

Verdict: Blocked (≥1 critical) → Changes Requested (≥1 major) → Approve.

### Layer 1 — inline comments (one per finding)

```
[🔴 critical · CONFIRMED] <title>        @ path/to/file.py:42  (dim: security)
TL;DR: <one line>
✓ Good: <redeeming aspect, or "—">
Fix:
```<lang>
<code>
```
```

---

## Regression testing

After any prompt change, run:
- `bash .claude/skills/review-code/fixtures/check.sh real-bugs` → MUST catch all
- `bash .claude/skills/review-code/fixtures/check.sh traps` → MUST NOT flag
- `bash .claude/skills/review-code/fixtures/check.sh clean` → MUST return Approve
- Compare against `expected.md` (source of truth).

## Scaling 3 → 10 dimensions

Add a charter line + one more `Agent` call **in the same fan-out message**. The
Step 3 verifier handles whatever candidates arrive.
