# PR Status Surface — One-line gate state for the operator

**Language:** English

> Companion to `skills/babysit-pr-local/SKILL.md` and `docs/local-ci.md`.
> Closes the "operator has to tail the babysitter log to know what's
> happening" gap. Describes a single read-only script that emits one
> ANSI line summarizing the active PR's gate state, plus the three
> render points that consume it.

## 1. Problem statement

`/dev-kit:babysit-pr` and its local-mode sibling
`/dev-kit:babysit-pr-local` iterate a PR until every gate passes.
Today the only feedback surface is the babysitter's own stdout: a
per-iteration evidence block (`audit:` / `local:` / `push:` /
`combine:` / `remaining:`). The operator cannot see gate progress
without keeping the babysitter's terminal in view, which breaks down
when:

- the babysitter runs in worktree A while the operator codes in
  worktree B
- the operator wants per-gate verdict (`review=` / `security=` /
  `maintenance=` + per-check conclusion) glanceable without scrolling
- a Codex session (different machine, different terminal) needs the
  same glance

The desired outcome: a single one-line, color-coded summary of the
active PR's gate state, available everywhere a shell stdout can be
rendered, from a single source of truth that does not drift between
consumers.

## 2. The SSOT contract

`bin/babysit-pr-local-status.py` (≈370 lines, read-only) is the
single source of truth. It emits one line, ≤ 200 chars, ANSI-colored,
ending with a newline. Exit code is **always 0**. A broken status line
is worse than no status line: every `gh` call is wrapped in a 3.5s
`timeout` and `|| true`, so a slow network call degrades to `?`
glyphs (dim, never a blank line).

Glyph vocabulary:

| Glyph | Color | Meaning |
|---|---|---|
| `✓` | green | per-gate `Approve`, CI bucket `pass` or `skipping` |
| `✗` | red / yellow | `Blocked` (red), `Changes Requested` (yellow), CI `fail` |
| `·` | yellow | pending verdict / live-pending CI bucket |
| `?` | dim | `gh` call failed, parse error, or no audit comment yet |

Example output:

```
PR#605 feat/foo │ review=✓ sec=✓ maint=✗ │ CI 3✓ 1✗ │ babysit iter=4
```

The script reads no env from the parent shell except `NO_COLOR` (per
no-color.org) and `BABYSIT_STATUS_NO_COLOR` (plugin escape hatch for
log files and CI logs). It honors `sys.stdout.isatty()` so redirected
output never leaks ANSI codes.

## 3. The three render points

The same line is consumed by three independent render surfaces, each
with its own lifecycle, none of which the plugin auto-wires into
user-level config:

| Render point | Lifecycle | How to enable |
|---|---|---|
| **Claude Code status bar** | Re-renders on each prompt submission, on each tool result, and on `refreshIntervalMs` ticks (~300-1500 ms timeout, stderr invisible). | Extend `bin/dev-kit-hooks-status.py`'s `status()` dict with a `pr_gate` key (already shipped in PR #710). The user-level `~/.claude/statusline-command.sh` (already wired at `~/.claude/settings.json:142-145`) picks the key up via `jq -r '.pr_gate // empty'`. |
| **Codex TUI footer** | Codex 0.99.0+ runs the `[tui.status_line]` command at its own cadence (turn boundaries + token deltas). | Paste into `~/.codex/config.toml`:<br>`[tui.status_line]`<br>`type = "command"`<br>`command = "/abs/path/bin/babysit-pr-local-status.py"` |
| **Babysitter tail** | The babysitter prints one line after each iteration's `LOG` step. | The skill body invokes the SSOT directly — no operator setup needed once the plugin is installed. |

The plugin deliberately does **not** mutate user-level config
(`~/.claude/settings.json`, `~/.codex/config.toml`). Both surfaces
are operator-owned; the SSOT script + the per-render opt-in recipes
ship in the plugin, the operator chooses when to wire them in.

## 4. Reading the line

A status line is parsed left-to-right:

1. **`PR#N`** — the PR number for the current branch's head
   (`gh pr view --json number -q .number`). Empty branch or no PR →
   the line degrades to `no PR on <branch>` and exits 0.
2. **`feat/foo`** — the short branch name (only rendered when not
   detached). The branch name lets the operator distinguish between
   worktrees at a glance.
3. **`review=✓ sec=✓ maint=✗`** — three per-skill LLM judge
   glyphs. The source is the most recent
   `<!-- dev-kit-verdict-audit -->` PR comment posted by
   `bin/review-local.sh` (or `bin/review-local.sh`'s GH-Actions
   counterpart). The parser walks known-key positions to handle
   `verdict=Changes Requested` (space-in-value) correctly.
4. **`CI 3✓ 1✗`** — the deterministic-CI bucket count from
   `gh pr checks --json name,state,bucket`. The script uses
   `bucket` (pre-categorized) rather than `conclusion` (raw state)
   because the installed `gh` CLI rejects `conclusion` ("Unknown
   JSON field"). Bucket vocabulary: `pass` + `skipping` →
   `pass`; `fail` + `cancel` → `fail`; `pending` → `pending`;
   unknown → `fail` (fail-closed, matches
   `lib/pr_verify.PASS_BUCKETS`).
5. **`babysit iter=4`** — only rendered when `.dev-kit/babysit.lock`
   is present (active babysitter on this worktree). The iter count
   comes from the last line of `.dev-kit/babysit.log`, parsed for
   `iter=<n>`. Stale locks (pid dead, age > 30 min) are still
   rendered — the script does not classify stale; the babysitter
   itself owns the lock-TTL contract (`lib/babysit_pr_reliability.is_stale_lock`).

## 5. Why a single bash-or-Python SSOT, not a daemon

Two alternatives were considered and rejected:

| Alternative | Why not |
|---|---|
| **Background poller + JSON cache** (`bin/pr-gate-writer.sh` writing `~/.cache/dev-kit/pr-gate.json` every 30s) | Adds an always-on process for a feature that is only useful during babysit iterations. The SSOT is fast enough (~1-5s cold, ~50ms warm) that polling is unnecessary; the on-demand read is honest. |
| **Direct call from each render surface** (Claude Code script + Codex script + babysitter all parsing `gh` independently) | Three independent parsers means three independent bug surfaces. A format drift in `lib/maintenance_gate.format_audit()` (the audit-comment line 1) breaks one consumer silently. The SSOT script centralizes the parser so a format change requires exactly one update. |

The chosen path (one SSOT, on-demand read, fail-soft) trades the
cost of a cold-cache `gh pr view --comments --json comments` call
(1.5s typical, occasionally 3s) for the cost-savings of zero new
background processes and zero new state files. The script is
budgeted at 3.5s per `gh` call; the total worst-case latency is
~3 × 3.5s = 10.5s, well inside the Claude Code statusLine budget
(1500ms-300ms typical, ~5s upper) because the three calls are
sequential subprocess.run() not parallel async. The 5s upper case
is acceptable for the `iter=4` line, which is already stale by the
time it renders (a 5s render lag on a 30s iteration is invisible).

## 6. Operator enablement

Three opt-in recipes, none of which are automatic:

**Claude Code** (one line to add to `~/.claude/statusline-command.sh`):

```bash
echo "$(python3 /Users/sanghee/dev/dev-harness-kit/bin/dev-kit-hooks-status.py --json | jq -r '.pr_gate // empty')"
```

**Codex** (paste into `~/.codex/config.toml`):

```toml
[tui.status_line]
type = "command"
command = "/Users/sanghee/dev/dev-harness-kit/bin/babysit-pr-local-status.py"
```

**Babysitter tail** (no setup — the skill body invokes the SSOT
after each per-iteration `LOG` step).

The recipes are also documented inline in
`skills/babysit-pr-local/SKILL.md` and
`skills/babysit-pr/SKILL.md` (the GH-Actions-mode sibling shares the
surface).

## 7. Verification

The SSOT script is hermetically testable: every `gh` and `git` call
is wrapped in a `subprocess.run` so unit tests can mock the helpers
(`_current_branch`, `_pr_number`, `_audit_comment`, `_gh_checks`,
`_lock_body`, `_iter_from_log`) and assert on the rendered line
without touching the network. 13 tests in
`tests/test_babysit_pr_local_status.py` pin the contract:

- T1-T2: PR not found, all-green
- T3-T4: one CI failure, maintenance `Changes Requested`
- T5: babysitter running with iter
- T6-T7: stale lock, `gh` missing (fail-soft)
- T8: `NO_COLOR` / non-tty
- parser: `verdict=Changes Requested` (space-in-value)
- bucket: `gh pr checks` `bucket` vocabulary

Plus 1 test in `tests/test_hooks_status.py` asserting the parent
`status()` dict always emits a `pr_gate` key (string, possibly
empty) so the user-level `jq -r '.pr_gate'` never errors.

## 8. Limitations + future work

- **Cross-machine sync**: not implemented. Each machine renders its
  own line from its own `gh` calls. A cross-machine "operator's
  babysitter is running" notification would require a shared state
  service, which is out of scope for this iteration.
- **Per-check name on failure**: the current line compresses
  failing checks into a count (`1✗`) without naming which check
  failed. A future iteration could surface the failing-check name
  (e.g. `1✗ (branch-policy)`) when the bucket count is small
  enough to fit in the 200-char budget.
- **Real-time refresh in Codex**: Codex's exact
  `[tui.status_line]` invocation cadence is undocumented in the
  public threads. The SSOT is budgeted at 3.5s/call to be safe at
  any cadence; if Codex turns out to call it more often than
  expected, the user-level config can set `refreshIntervalMs` (not
  yet a Codex knob; would require a Codex feature request).

## Related

- [`skills/babysit-pr-local/SKILL.md`](../../skills/babysit-pr-local/SKILL.md) — algorithm body of the local babysitter.
- [`skills/babysit-pr/SKILL.md`](../../skills/babysit-pr/SKILL.md) — GH-Actions-mode sibling.
- [`bin/babysit-pr-local-status.py`](../../bin/babysit-pr-local-status.py) — the SSOT.
- [`bin/dev-kit-hooks-status.py`](../../bin/dev-kit-hooks-status.py) — emits the `pr_gate` key consumed by Claude Code.
- [`bin/review-local.sh`](../../bin/review-local.sh) — posts the `<!-- dev-kit-verdict-audit -->` comment the SSOT parses.
- [`lib/maintenance_gate.py`](../../lib/maintenance_gate.py) — owns the byte-stable audit-comment line 1 format.
- [`lib/babysit_pr_reliability.py`](../../lib/babysit_pr_reliability.py) — `is_stale_lock` (canonical lock reader), `build_check_state` (canonical CI diff cache).
- [`lib/pr_verify.py`](../../lib/pr_verify.py) — `PASS_BUCKETS` (the same `pass`+`skipping` policy the SSOT mirrors).
- [`docs/local-ci.md`](../local-ci.md) — local-CI playbook.
- [`docs/skills/babysit-pr-local.md`](../skills/babysit-pr-local.md) — operator-facing doc for the local babysitter skill.
