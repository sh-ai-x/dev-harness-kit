<!--
Dev-harness PR template. Required sections by CI gate:
  - "What"             always
  - "Iron Law L3"      when production code is touched
  - "Risk / blast"     always
  - "TDD evidence"     when test files are added/modified
  - "Hand-off"         always
The L3-quoted test count line is REQUIRED for any PR that touches lib/,
tools/, skills/, or hooks/ — see /dev-kit/review for the gate logic.
-->

## What

<!-- 1-3 sentence summary of the change. If the PR touches more than one
     concern, list the headline bullets here and link the detail below. -->

-

## Iron Law L3 (verification)

<!-- REQUIRED when this PR touches production code (lib/, tools/, hooks/,
     skills/, .githooks/, .claude/, .codex/, .github/).
     Per rules/iron-laws.md L3: no completion claim without quoted exit code +
     test count + build log. Paste the actual line, not a paraphrase.
     The format below is what the gate regex matches.

     Format (regex):  `\d+ (passed|failed)(, \d+ (skipped|xfailed|xpassed))? in [0-9.]+s`
     Example (all-green, no skips):  74 passed in 21.93s
     Example (all-green, with skips): 1101 passed, 30 skipped in 195.50s (0:03:15)
-->

```
<paste the exact pytest / pytest+ruff tail line here>
```

- Test command: `<exact command, e.g. python3 -m pytest -q>`
- Exit code: `<0/1 — paste from $? capture, not from CI badge>`
- Lint: `<ruff clean / N findings — paste the `Found N errors.` line>`

## Risk / blast radius

<!-- File-by-file impact + who/what could break. Skip "none" — list the
     surface even if the answer is "self-contained" so a reviewer can
     audit the boundary. -->

| File | Change | Calls into | Risk |
|---|---|---|---|
| | | | |

- **Backout plan:** `<one-line revert hint, e.g. "git revert <sha> — no schema migration">`
- **Feature flag / config touched:** `<yes/no — list if yes>`

## TDD evidence (red → green)

<!-- For production code, paste the failing test that drove the change.
     For docs/infra only, mark "N/A — no production code change". The
     tdd-guard hook enforces this for lib/, tools/, hooks/, skills/. -->

- N/A — no production code change

<!-- otherwise:
- Failing test (red):  `pytest tests/test_X.py::test_y -k "..."` → AssertionError on line N
- Patch (smallest):     one-line / one-block summary
- Passing test (green): `pytest tests/test_X.py::test_y -k "..."` → exit 0
-->

## Hand-off context

- **Skill(s) used:** `<e.g. /dev-kit:plan + /dev-kit:build + /dev-kit:review>`
- **Companion PR:** `<#NNN — required if this PR is part of a multi-PR plan>`
- **Closes / supersedes:** `<#NNN or "new">`
- **Dry-run artifact:** `<path to .dev-kit/hand-off/<artifact>.md if any>`
- **Reviewer focus areas:** `<e.g. "see also: the 18 deletions in lib/eval_runner.py — verify callers moved to dicts">`

---

### Checklist (delete from final commit message)

- [ ] `pytest` exit 0 (paste L3 line above)
- [ ] `ruff check .` clean (or N findings justified)
- [ ] Branch rebased onto `origin/main`; pre-push version bump applied
- [ ] No secrets (`bin/set-provider.sh` keys, `*.env*`, `~/.claude/`)
- [ ] No `[skip ci]` in commit body (`rules/no-skip-ci.md`)
- [ ] Worktree to be removed after merge: `.worktrees/ci-lint-pr-template-2026-07-21/`
