---
description: End-to-end autonomous loop with 4 user gates + unattended build/babysit/ship.
allowed-tools: Read Write Glob Bash Skill AskUserQuestion
argument-hint: "<idea>"
model: opus
---

# /dev-kit:ralph — end-to-end autonomous loop

Forward to `skills/ralph/SKILL.md`. Implementation lives in:
- `skills/ralph/lib/ralph_state.py` (state machine + `attended_lock`
  invariant)
- `skills/ralph/lib/ralph_chain.py` (unattended chain executor: BUILD →
  BABYSIT → SHIP → terminal)
- `hooks/ralph-attended-lock.sh` (mechanical AskUserQuestion refusal
  once `attended_lock` is set — wired in `hooks/hooks.json` +
  `.codex-plugin/hooks/hooks.json`)
- `skills/ralph/scripts/ralph_drive.sh` (bash glue for the state machine)

Arguments:
- `<idea>` — 1-line idea to take through RESEARCH_GATE → PROPOSAL_GATE →
  PLAN_GATE → SHIP_CONFIRM_GATE → ATTENDED_RUN.

After SHIP_CONFIRM_GATE exits Approve, `attended_lock` is set and
AskUserQuestion is invariant-forbidden during ATTENDED_RUN. Each gate
supports Approve / Edit-then-approve (rewinds to that gate) / Abort.

The unattended chain invokes
`babysit-pr --operator-is-only-human --rationale "..."` and `ship`
without ever asking the user. See `skills/ralph/SKILL.md`
§"ATTENDED_RUN chain contract" for the full exit-code mapping.

Linear is OUT OF SCOPE — see `skills/ralph/SKILL.md` §"Linear is OUT OF
SCOPE" for the rationale.

See `docs/proposals/review/ralph-autonomy/main.html` for the design
record + `docs/skills/ralph.md` for the human-facing overview.