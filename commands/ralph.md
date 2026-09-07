---
description: End-to-end autonomous loop with 4 user gates + unattended build/babysit/ship.
allowed-tools: Read Write Glob Bash Skill AskUserQuestion
argument-hint: "<idea>"
model: opus
---

# /dev-kit:ralph — end-to-end autonomous loop

Forward to `skills/ralph/SKILL.md`. Implementation lives in
`skills/ralph/lib/ralph_state.py` (state machine) +
`skills/ralph/scripts/ralph_drive.sh` (chain glue).

Arguments:
- `<idea>` — 1-line idea to take through RESEARCH_GATE → PROPOSAL_GATE →
  PLAN_GATE → SHIP_CONFIRM_GATE → ATTENDED_RUN.

After SHIP_CONFIRM_GATE exits Approve, `attended_lock` is set and
AskUserQuestion is invariant-forbidden during ATTENDED_RUN. Each gate
supports Approve / Edit-then-approve (rewinds to that gate) / Abort.

Linear is OUT OF SCOPE — see `skills/ralph/SKILL.md` §"Linear is OUT OF
SCOPE" for the rationale.

See `docs/proposals/review/ralph-autonomy/main.html` for the design
record + `docs/skills/ralph.md` for the human-facing overview.