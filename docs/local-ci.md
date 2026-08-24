# Local CI mode

Run the same review pipeline that `.github/workflows/review.yml` +
`maintenance.yml` + `auto-fix-pr.yml` run in GH-Actions — **locally**,
without consuming GH-Actions minutes. Two deliverables, both additive
(the existing workflows are unchanged):

1. `bin/review-local.sh` — local equivalent of the
   review + maintenance workflow orchestration.
2. `/dev-kit:babysit-pr --local-verify [--local-test-cmd "..."]` —
   optional local-test gate inside the babysit iteration loop.

## When to use

- The repo has hit its GH-Actions minute cap on private plans.
- The operator wants to iterate on a review verdict locally (faster
  feedback loop than waiting for CI to spin up).
- The operator is testing a provider switch (`bin/set-provider.sh
  <provider>`) or a new `--local-test-cmd` ahead of pushing.

## When NOT to use

- The branch's PR requires a reviewer bot or org-level MCP that's only
  available via `anthropics/claude-code-action`. Local `claude -p` does
  not have access to the same MCP servers; inline comments go through
  `gh pr comment` instead of `mcp__github_inline_comment__create_inline_comment`.
- The PR requires `gh pr merge` — merging is always a human action.
  `bin/review-local.sh`'s `--auto-approve` casts `gh pr review --approve`
  only; the operator merges the PR themselves.

---

## Local review: `bin/review-local.sh`

A direct shell port of the orchestration half of
`.github/workflows/review.yml` + `maintenance.yml`. The LLM-judge
skills (`/dev-kit:review`, `/dev-kit:security`, `/dev-kit:maintenance`)
are reused verbatim via the local `claude -p` invocation.

### Usage

```bash
# Dry-run (no LLM call, no PR mutation): preview env + planned commands.
bin/review-local.sh --pr 123 --dry-run

# Full review + auto-approve on clean verdict.
bin/review-local.sh --pr 123 --auto-approve

# Force a specific provider (overrides .env:CI_REVIEW_PROVIDER).
bin/review-local.sh --pr 123 --provider anthropic --auto-approve

# Run only /dev-kit:review (skip security + maintenance).
bin/review-local.sh --pr 123 --review-only

# Force-anthropic, only security, no auto-approve (dry-run).
bin/review-local.sh --pr 123 --security-only --provider anthropic --dry-run
```

### Slash command

```bash
/dev-kit:review-local --pr 123 --auto-approve
```

The slash command is a thin wrapper over `bin/review-local.sh`. Both
paths apply the same provider switching + gate logic.

### Provider setup

The script reads `CI_REVIEW_PROVIDER` from the process env, then
`.env` (matches `bin/set-provider.sh` resolution). Switch via:

```bash
bin/set-provider.sh anthropic
bin/set-provider.sh deepseek
bin/set-provider.sh minimax
```

The matching `*_API_KEY` must be in `.env` (or the process env):

```bash
# .env (gitignored)
ANTHROPIC_API_KEY=sk-ant-...
MINIMAX_API_KEY=sk-cp-...
DEEPSEEK_API_KEY=sk-...
```

`bin/review-local.sh` reads the key via `lib/ci_setup.read_env_key()`
and passes the same `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` /
`ANTHROPIC_MODEL*` block as `review.yml:120-131` to the `claude -p`
invocation via the `env KEY=... claude -p ...` prefix. The key never
enters the parent shell's persistent environment, so subsequent `gh` /
shell calls cannot leak it via `/proc/<pid>/environ` or core dumps.

#### No provider configured? It still works.

`bin/set-provider.sh` + a `*_API_KEY` are for **CI runners** (GH-Actions
has no interactive login, so it needs an explicit key injected). A
local interactive session almost always already has an authenticated
`claude` CLI (a `claude login` session or a keychain-stored key) — the
script does **not** require `.env`, `CI_REVIEW_PROVIDER`, or any
`*_API_KEY` to be set at all.

If neither `--provider`, `CI_REVIEW_PROVIDER` (env or a real `.env`),
nor a matching `*_API_KEY` is found, the script logs:

```
no provider explicitly configured and no API key found; falling back to local claude CLI auth (no key/base-url injection)
```

and calls `claude -p ...` **without** any `ANTHROPIC_BASE_URL` /
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` override — `claude -p`
inherits the parent shell's existing auth exactly as if you'd run it
by hand. This is the common case for a local `bin/babysit-pr-local.sh`
run; explicit provider configuration is only needed when you want a
*specific* non-default provider (e.g. testing `minimax` while your
`claude` CLI's own login points at `anthropic`).

An **explicit** ask (`--provider X`, or `CI_REVIEW_PROVIDER` set) with
no matching key still fails loudly — that's a real misconfiguration,
not the "just use my session" case.

### What it does (mirrors `review.yml` step-by-step)

| Step | Source step name | Local equivalent |
|---|---|---|
| Resolve provider | review.yml `Resolve PR + provider` step | `lib/ci_setup.read_provider()` |
| Set ANTHROPIC_* env | review.yml `Run /dev-kit:review via …` env block | `provider_env_for "$PROVIDER"` (sourced from `lib/review_local_lib.sh`) |
| Run `/dev-kit:review` | review.yml `Run /dev-kit:review via …` step | `claude -p "/dev-kit:review --diff ..."` |
| Run `/dev-kit:security` | review.yml `Run /dev-kit:security via …` step | `claude -p "/dev-kit:security --diff ..."` |
| Run `/dev-kit:maintenance` | maintenance.yml `Run /dev-kit:maintenance via …` step | `claude -p "/dev-kit:maintenance --diff ..."` |
| Extract verdict | review.yml `Extract <skill> verdict` step | capture `claude -p` stdout per skill, pipe to `python3 -m lib.maintenance_gate --extract-verdict-from-stdin` |
| Bump-PR skip | review.yml job-level `if:` filter | `is_bump_pr "$PR_TITLE"` (sourced from `lib/review_local_lib.sh`) |
| Combined verdict gate | review.yml `Combined verdict gate` step | `rank()` (from `lib/review_local_lib.sh`) + worst-of wins |
| L3-evidence gate | review.yml `L3 evidence gate (PR body must quote test count)` step | `extract_pytest_tail "$PR_BODY"` (from `lib/review_local_lib.sh`) |
| Auto-approve | review.yml `Auto-approve on clean verdict` step | `gh pr review --approve --body "..."` (only with `--auto-approve`) |
| Audit comment | review.yml `Extract <skill> verdict` audit line | `gh pr comment --body "<!-- dev-kit-verdict-audit --> ..."` |

### Caveats

- **No MCP inline-comment server**: the workflow has access to
  `mcp__github_inline_comment__create_inline_comment` via
  `claude-code-action`. Local `claude -p` does not. The skill body
  falls back to `gh pr comment` (per `skills/review/SKILL.md`).
- **Local API key exposure**: the script scopes the key to the
  `claude -p` invocation only (via `env KEY=... claude -p ...`). It
  does NOT enter the parent shell's persistent env. Do NOT run it on
  a shared host regardless -- the agent still processes PR content
  with operator credentials.
- **Cannot `gh pr merge`**: the script never merges. The operator
  runs `gh pr merge` manually after `--auto-approve` lands.
- **No provider fallback**: `--provider` is strict; an unknown
  provider exits 1. The script does not auto-switch to `minimax`.
  This matches `bin/set-provider.sh` behavior.

---

## Local babysit: `--local-verify`

`/dev-kit:babysit-pr` already runs locally (the skill body lives in
the current shell). What `--local-verify` adds is a **pre-commit
local test gate** so iterations abort *before* `git push` when the
local test suite fails — saving the GH-Actions run that would
otherwise be consumed by a known-failing commit.

### Usage

```bash
# Default: run pytest -q before each iteration's push.
# (Additive flag; default behavior is unchanged when --local-verify is absent.)
/dev-kit:babysit-pr --local-verify

# Project-specific test command. Stdout/stderr MUST include a
# pytest-style tail line ('<N> passed in <Ns>s' or '<N> failed in <Ns>s')
# per MUST-L3.
/dev-kit:babysit-pr --local-verify --local-test-cmd "make test"
```

### What it does

The skill's §Algorithm loop gains a new step 7.5 between
APPLY FIX (step 7) and VERIFY LOCAL (step 8):

```
7.5. LOCAL VERIFY (only when --local-verify set)
     - lib.babysit_pr_cli.run_local_verify(cmd=--local-test-cmd,
                                          cwd=<worktree>)
       executes the command via `bash -c "$cmd"` and returns a
       LocalVerifyResult. The iteration proceeds only when
       passed=True AND tail_line is the quoted pytest tail line.
     - non-zero exit OR missing tail line OR timeout -> abort iteration
       BEFORE git add / commit / push (MUST-L3 enforcement).
```

The existing step 8 (VERIFY LOCAL — re-run the specific failing check)
is preserved. `--local-verify` adds a *broader* pre-commit check, not
a replacement.

### Why this matters

Without `--local-verify`, the babysit loop's typical flow is:

```
fix → git add → git commit → git push → wait for GH-Actions CI
```

A known-failing local test consumes one GH-Actions run per iteration.
With `--local-verify`:

```
fix → pytest -q (LOCAL) → fix re-iteration → ... → git add → git commit → git push
```

Failing iterations abort before the push, so no GH-Actions run is
consumed until the iteration actually passes the gate. The user still
verifies locally, but the GH-Actions budget is preserved for genuinely
green PRs.

### Implementation

- Parser: `lib/babysit_pr_cli.py::parse_babysit_args()` gains two
  `--local-verify` + `--local-test-cmd` fields. `run_babysit_once()`
  is unchanged (the helper stays pure).
- Skill body: `skills/babysit-pr/SKILL.md` §Algorithm step 7.5
  documents the new step. The Bash invocation lives in the
  orchestrator script, not in `lib/`.
- Tests: `tests/test_babysit_pr_cli.py::TestParseBabysitArgs` adds
  T22-T24 (default-off, flag-on, override, coexists-with-other-flags).

### Caveats

- **Local test suite must be sane**: `--local-verify` trusts the
  local test result. If the local test suite is itself broken or
  stale (e.g. missing fixture), the gate refuses to push. Operators
  should run `pytest -q` once without `--local-verify` to confirm
  the local baseline before relying on the flag.
- **MUST-L3 is enforced by the skill body, not by the helper**: if
  the test command exits 0 but its stdout lacks a pytest-style tail
  line, the skill refuses to flip to "ready to push". The operator
  must either pick a test command that emits the tail line or paste
  the evidence manually.
- **No fallback to GH-Actions**: refusing to push means the iteration
  aborts. The operator can re-run `/dev-kit:babysit-pr` without
  `--local-verify` to fall back to the default push-and-wait-CI flow.

---

## `/dev-kit:babysit-pr-local` — local-mode babysit (additive sibling)

When GH-Actions minutes are exhausted AND the operator wants the same
iterative repair loop as `/dev-kit:babysit-pr`, but driven entirely by
the local LLM-judge verdict instead of `gh pr checks --watch`, run
this skill:

```bash
# No flags exposed to operators. Slash invocation is always 0-arg.
/dev-kit:babysit-pr-local   # babysit current branch's PR
```

The skill is a 0-arg orchestrator. Behind the scenes, the §Algorithm
step 4L invokes `bin/babysit-pr-local.sh <PR>` (refuses
`--auto-appearing`), which execs `bin/review-local.sh --pr <PR>` to
run `/dev-kit:review` + `/dev-kit:security` + `/dev-kit:maintenance`
locally. The local audit comment (`<!-- dev-kit-verdict-audit
-->` ... `verdict=Approve source=bin_review_local`) is the
iteration's "green" signal; the local judge's stdout line
(`combined verdict: <Word>`) is the MUST-L3 evidence quote.

### Why a separate skill instead of a flag

The user-facing UX contract — "operate only via skill, no options to
remember" — drives the split:

| | `/dev-kit:babysit-pr --local-verify` | `/dev-kit:babysit-pr-local` |
|---|---|---|
| `--local-verify` flag exposed | yes (operator types it) | no (always on) |
| Review verdict source | GH-Actions CI | local `bin/review-local.sh` |
| `gh pr checks --watch` | yes | no |
| `--auto-approve` semantically possible | yes (the operator may pass it through babysit) | forbidden (refused with exit 2) |
| Pre-push pytest gate | off by default | always on |

The two skills are additive siblings — they share the lock-file
protocol + `lib/babysit_pr_cli` helpers + worktree-detect plumbing,
but the SKILL.md §Algorithm differs in five steps (see
`skills/babysit-pr-local/SKILL.md` §"Step diff vs `/dev-kit:babysit-pr`").
Operators who want local mode type exactly one command; operators
who want CI mode keep using `/dev-kit:babysit-pr` unchanged.

### Hidden flags (not in the slash description; power users + tests)

- `--pr N` — babysit explicit PR number instead of current-branch
  PR discovery. Use this when the PR was opened from another
  worktree (e.g. `git worktree add .worktrees/<branch>` then push
  from there).
- `--local-test-cmd CMD` — override the pre-push pytest default
  (`pytest -q`) with a project-specific runner. The command's
  stdout+stderr MUST emit a pytest-style tail line
  (`<N> passed in <Ns>s` or `<N> failed in <Ns>s`) per MUST-L3.
  For Make / tox / nox / Go / JS projects, supply the runner that
  emits that shape; otherwise the iteration refuses to push.
- `--local-mode` — internal routing flag (already implied by the
  slash invocation; kept so the parser doesn't double-parse).

None appear in `--help`, `description`, or `argument-hint`. Operators
always run `/dev-kit:babysit-pr-local` with no arguments.

### Lock file

`<worktree>/.dev-kit/babysit.lock` — **shared** with
`/dev-kit:babysit-pr`. If both skills race on the same worktree, the
second arrival sees a fresh lock and refuses with `already running`.
The lock body appends `source=babysit-pr-local` so a post-mortem can
tell which skill held the lock. The stale-lock TTL is the same
30 minutes (`lib/babysit_pr_reliability.LOCK_TTL_SECONDS`).

### Implementation

- `bin/babysit-pr-local.sh` — single-call wrapper (≈30 lines) that
  validates args (refuses `--auto-appearing`) and execs
  `bin/review-local.sh --pr $PR_NUMBER`.
- `lib/babysit_pr_cli.py::is_local_mode(argv)` + the hidden
  `--local-mode` argparse field — routing helpers (the parser's
  flag is suppressed from `--help` for L5 compliance).
- `skills/babysit-pr-local/SKILL.md` — the algorithm body (≈365
  lines); §Algorithm mirrors `/dev-kit:babysit-pr` with five
  surgical substitutions (steps 3 / 4 / 4L / 7.5 / 11 / 12 in the
  parent).
- `skills/babysit-pr-local/recipes/canonical-wiring.md` — parent
  preflight block + sub-agent prompt body.
- `commands/babysit-pr-local.md` — slash command description
  (mirrors `commands/review-local.md`).

---

## Related

- `bin/review-local.sh` — local equivalent of the GH-Actions review workflow.
- `bin/babysit-pr-local.sh` — local-mode babysit wrapper (executable; refuses `--auto-appearing`).
- `commands/review-local.md` — slash command wrapper (one-shot local review).
- `commands/babysit-pr-local.md` — slash command wrapper (local-mode babysit).
- `skills/babysit-pr/SKILL.md` — babysit-pr skill (additive `--local-verify` flag, GH-Actions-driven).
- `skills/babysit-pr-local/SKILL.md` — local-mode babysit skill (additive sibling; replaces `gh pr checks --watch` with `bin/review-local.sh`).
- `skills/babysit-pr-local/recipes/canonical-wiring.md` — local-mode sub-agent prompt + parent preflight.
- `lib/maintenance_gate.py` — verdict-extraction + combined-gate helper.
- `lib/ci_setup.py` — provider resolution + secret name lookup.
- `lib/babysit_pr_cli.py` — `is_local_mode`, `parse_babysit_args`, `run_local_verify`, `run_babysit_once`.
- `bin/set-provider.sh` — local provider switch (`bin/set-provider.sh anthropic`).
- `.github/workflows/review.yml` — GH-Actions equivalent (unchanged).
- `.github/workflows/maintenance.yml` — GH-Actions equivalent (unchanged).
- `scripts/ci-local.sh` — pre-existing local validator runner (no LLM review).
- `tests/test_review_local_sh.py` — shell-level tests for `bin/review-local.sh`.
- `tests/test_review_local_lib.py` — unit tests for `lib/review_local_lib.sh`.
- `tests/test_babysit_pr_cli.py` — parser + orchestrator tests for babysit-pr.
- `tests/test_babysit_pr_local_cli.py` — parser + `is_local_mode` tests for babysit-pr-local.
- `tests/test_babysit_pr_local_sh.py` — shell-level tests for `bin/babysit-pr-local.sh`.
- `tests/test_commands_install.py` — slash-command install governance.
