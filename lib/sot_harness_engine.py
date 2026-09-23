"""lib/sot_harness_engine.py — Interview-based SOT harness document writer.

Drives the 5-dimension interview and synthesizes a SOT harness
document. Each dimension surfaces 2-3 evidence-based recommendations
from the agent-harness-playbook research; the user accepts/rejects/
customizes. Output is a complete SOT doc with traceability.

Public surface:
  ROUNDS: list[dict] — the 5 interview rounds, in order
  synthesize_sot: pure function that builds the SOT doc from a decision set
  write_sot_handout: writes the SOT doc to .dev-kit/hand-off/
  write_decision_log: writes the per-round Q+A log

CLI: not provided. The skill (skills/sot-harness-writer/SKILL.md) drives
the conversation; this module is the deterministic synthesizer.
"""

from __future__ import annotations

import re
from pathlib import Path

# Hand-off frontmatter — single source of truth for the discriminator
# the plan-skill consume gate uses (issue #898).
SOT_HANDOFF_KIND = "sot"
SOT_HANDOFF_STATUS_LOCKED = "locked"
SOT_HANDOFF_GENERATED_BY = "sot-harness-writer"


# --------------------------------------------------------------------------- #
# Constructor helpers (replaces the old dataclasses).
# --------------------------------------------------------------------------- #

_RECOMMENDATION_DEFAULTS = {
    "id": "",
    "thesis": "",
    "source_url": "",
    "source_label": "",
    "tradeoff": "",
}

_ROUND_DEFAULTS = {
    "key": "",
    "question": "",
    "recommendations": (),  # tuple of dicts; populated with a list at construction
}

_ROUND_DECISION_DEFAULTS = {
    "round_key": "",
    "recommendation_id": "",
    "decision": "accept",
    "customize_text": "",
    "note": "",
}

_SOT_DECISION_SET_DEFAULTS = {
    "project_name": "",
    "idea_one_liner": "",
    "decisions": {},  # dict[str, dict] (RoundDecision)
    "open_questions": [],
    "session_id": "default",
}

_ROUND_LOG_ENTRY_DEFAULTS = {
    "round_key": "",
    "question": "",
    "user_choice": "",
    "note": "",
}


def new_recommendation(**overrides) -> dict:
    out = dict(_RECOMMENDATION_DEFAULTS)
    out.update(overrides)
    return out


def new_round(**overrides) -> dict:
    out = dict(_ROUND_DEFAULTS)
    out.update(overrides)
    return out


def new_round_decision(**overrides) -> dict:
    out = dict(_ROUND_DECISION_DEFAULTS)
    out.update(overrides)
    return out


def new_sot_decision_set(**overrides) -> dict:
    # Inline fresh mutable containers — shallow copy of DEFAULTS would share
    # the dict/list between calls and leak state across instances.
    out = {
        "project_name": "",
        "idea_one_liner": "",
        "decisions": {},
        "open_questions": [],
        "session_id": "default",
    }
    out.update(overrides)
    return out


def new_round_log_entry(**overrides) -> dict:
    out = dict(_ROUND_LOG_ENTRY_DEFAULTS)
    out.update(overrides)
    return out


# Method replacements — free functions over dicts.

def round_pick(round_obj: dict, rec_id: str):
    """Return the recommendation dict with the given id, or None."""
    for r in round_obj["recommendations"]:
        if r["id"] == rec_id:
            return r
    return None


def round_decision_is_valid(dec: dict) -> bool:
    return (
        dec["decision"] in ("accept", "reject", "customize")
        and bool(dec["recommendation_id"])
    )


def sot_decision_set_is_complete(decisions: dict) -> bool:
    return set(r["key"] for r in ROUNDS).issubset(decisions["decisions"].keys())


def validate_sot_decision_set(decisions: dict) -> list[str]:
    """Return a list of validation errors; empty list = pass."""
    errors: list[str] = []
    if not sot_decision_set_is_complete(decisions):
        missing = [r["key"] for r in ROUNDS if r["key"] not in decisions["decisions"]]
        errors.append(f"missing decisions for: {', '.join(missing)}")
    rounds_by_key = {r["key"]: r for r in ROUNDS}
    for key, dec in decisions["decisions"].items():
        if not round_decision_is_valid(dec):
            errors.append(f"decision for {key} is invalid")
            continue
        round_obj = rounds_by_key.get(key)
        if round_obj is None:
            errors.append(f"unknown round key: {key}")
            continue
        if round_pick(round_obj, dec["recommendation_id"]) is None:
            errors.append(
                f"recommendation_id '{dec['recommendation_id']}' does not "
                f"belong to round '{key}'"
            )
        if dec["decision"] == "customize" and not dec["customize_text"].strip():
            errors.append(
                f"customize chosen for {key} but no customize_text provided"
            )
        if dec["decision"] == "reject" and not dec["note"].strip():
            errors.append(
                f"reject chosen for {key} but no reason (note) provided"
            )
    return errors


# The 5 dimensions of an agent harness, derived from the canonical
# 5-subsystem decomposition (walkinglabs, Fowler/Böckeler) and
# Anthropic's effective-harnesses article.

ROUNDS: list[dict] = [
    new_round(
        key="project_context",
        question="What is your project's primary agent-harness category?",
        recommendations=[
            new_recommendation(
                id="long_running",
                thesis=(
                    "Long-running autonomous agents that span hours/days "
                    "across many context windows. Uses an initializer agent "
                    "to scaffold a feature list (~200 JSON entries, all "
                    "initially failing) and a coding agent that orients via "
                    "git history + progress notes."
                ),
                source_url="https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents",
                source_label="Anthropic: Effective harnesses for long-running agents",
                tradeoff=(
                    "Highest reliability ceiling; biggest upfront harness "
                    "investment (~1 day to scaffold)."
                ),
            ),
            new_recommendation(
                id="multi_agent_research",
                thesis=(
                    "Orchestrator-worker multi-agent system for research or "
                    "exploratory tasks. The lead agent decomposes, spawns "
                    "specialized subagents that search in parallel via "
                    "separate context windows, then synthesizes findings."
                ),
                source_url="https://www.anthropic.com/engineering/multi-agent-research-system",
                source_label="Anthropic: How we built our multi-agent research system",
                tradeoff=(
                    "Best for search-heavy or open-ended tasks; less suited "
                    "to local codebase changes."
                ),
            ),
            new_recommendation(
                id="single_agent_coding",
                thesis=(
                    "Single coding-only agent (SWE-agent / SWE-ReX style) "
                    "with a tight tool set and a single-pass evaluation "
                    "loop. Best when the task fits in a single context window."
                ),
                source_url="https://github.com/SWE-agent/SWE-agent",
                source_label="SWE-agent",
                tradeoff=(
                    "Lowest harness cost; bounded to short tasks with clear "
                    "issue/PR inputs."
                ),
            ),
        ],
    ),
    new_round(
        key="verification",
        question="How will you verify the agent's work?",
        recommendations=[
            new_recommendation(
                id="self_verification_browser",
                thesis=(
                    "Self-verification prompts + browser automation to "
                    "validate the running app end-to-end. Strongest defense "
                    "against premature task completion."
                ),
                source_url="https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents",
                source_label="Anthropic: Effective harnesses for long-running agents",
                tradeoff=(
                    "Requires a runnable app; not applicable to library/"
                    "infrastructure work."
                ),
            ),
            new_recommendation(
                id="generator_evaluator_split",
                thesis=(
                    "Generator/evaluator split (GAN-inspired): a planner "
                    "expands briefs into full specs, an incremental "
                    "generator, and a Playwright-driven evaluator. The "
                    "evaluator gives concrete feedback beyond self-critique."
                ),
                source_url="https://www.anthropic.com/engineering/harness-design-long-running-apps",
                source_label="Anthropic: Harness design for long-running application development",
                tradeoff=(
                    "Two agents to coordinate; doubles token cost per task "
                    "but halves the verify-fix-loop cost."
                ),
            ),
            new_recommendation(
                id="deterministic_only",
                thesis=(
                    "Deterministic checks only: lint, type check, unit "
                    "tests, contract tests. The agent must satisfy "
                    "machine-checked gates before declaring done."
                ),
                source_url="https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html",
                source_label="Fowler/Böckeler: Harness Engineering",
                tradeoff=(
                    "Cheapest; misses semantic regressions no test catches."
                ),
            ),
        ],
    ),
    new_round(
        key="context",
        question="How will you manage the context window?",
        recommendations=[
            new_recommendation(
                id="frequent_intentional_compaction",
                thesis=(
                    "Frequent intentional compaction: keep context at 40-60% "
                    "utilization, periodic compaction of transcripts to "
                    "durable files, subagents with fresh contexts for "
                    "search/summary."
                ),
                source_url="https://www.humanlayer.dev/blog/advanced-context-engineering",
                source_label="HumanLayer: Advanced Context Engineering",
                tradeoff=(
                    "Requires discipline on the agent side; 15-20% longer "
                    "wall-clock per task."
                ),
            ),
            new_recommendation(
                id="filesystem_memory",
                thesis=(
                    "Filesystem as restorable external memory: durable "
                    "artifacts (research.md, plan.md, progress.log) live on "
                    "disk; the next session reads them instead of "
                    "re-discovering the world."
                ),
                source_url="https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents",
                source_label="Anthropic: Effective harnesses for long-running agents",
                tradeoff=(
                    "Best for multi-session work; adds structure that "
                    "single-session agents don't need."
                ),
            ),
            new_recommendation(
                id="subagent_firewall",
                thesis=(
                    "Subagent context isolation: complex sub-tasks run in a "
                    "subagent's fresh context, returning only a summary. "
                    "The orchestrator never sees the subagent's intermediate "
                    "reasoning."
                ),
                source_url="https://blog.langchain.com/improving-deep-agents-with-harness-engineering/",
                source_label="LangChain: Improving Deep Agents with harness engineering",
                tradeoff=(
                    "Loses traceability of subagent reasoning unless you "
                    "log transcripts."
                ),
            ),
        ],
    ),
    new_round(
        key="safety",
        question="What safety perimeter?",
        recommendations=[
            new_recommendation(
                id="os_sandbox",
                thesis=(
                    "OS-level sandboxing (Linux bubblewrap, macOS "
                    "seatbelt) for filesystem + network isolation, with a "
                    "domain-routed proxy that vets outbound requests."
                ),
                source_url="https://www.anthropic.com/engineering/claude-code-sandboxing",
                source_label="Anthropic: Beyond permission prompts (sandboxing)",
                tradeoff=(
                    "Cuts permission prompts by ~84%; requires a custom "
                    "proxy and platform-specific config."
                ),
            ),
            new_recommendation(
                id="worktree_isolation",
                thesis=(
                    "Git worktree isolation: every change-set lives in its "
                    "own branch and worktree; the main checkout is "
                    "read-only via PreToolUse guards. Lowest infrastructure "
                    "cost."
                ),
                source_url="https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html",
                source_label="Fowler/Böckeler: Harness Engineering",
                tradeoff=(
                    "Doesn't constrain filesystem or network; relies on "
                    "commit-level review."
                ),
            ),
            new_recommendation(
                id="contract_of_intent",
                thesis=(
                    "Contracts-of-intent (AIL/HEAAL pattern): the "
                    "programming language's grammar enforces declared "
                    "purpose, decidable success criteria, and forbidden "
                    "capabilities for every program the agent writes."
                ),
                source_url="https://github.com/hyun06000/AIL",
                source_label="HEAAL: AI Intent Language",
                tradeoff=(
                    "Research-grade; no production adoption as of 2026-08. "
                    "Best for high-risk domains where the spec is worth the "
                    "tooling investment."
                ),
            ),
        ],
    ),
    new_round(
        key="lifecycle",
        question="What session lifecycle?",
        recommendations=[
            new_recommendation(
                id="initializer_progress",
                thesis=(
                    "Initializer + progress log: a one-shot initializer "
                    "creates a feature list + init.sh + progress log + "
                    "initial commit. Each coding session orients from those "
                    "durable artifacts."
                ),
                source_url="https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents",
                source_label="Anthropic: Effective harnesses for long-running agents",
                tradeoff=(
                    "Best for tasks that span >1 session; adds ~30-60 min "
                    "setup cost amortized across the task lifetime."
                ),
            ),
            new_recommendation(
                id="ralph_loop",
                thesis=(
                    "Ralph-style single-task bash loop: one task per "
                    "iteration, deterministic context allocation, subagent "
                    "for expensive work, single validation subagent. The "
                    "agent runs until budget exhausted or success."
                ),
                source_url="https://ghuntley.com/ralph/",
                source_label="Huntley: Ralph Wiggum as a Software Engineer",
                tradeoff=(
                    "Best for greenfield or 'settle the harness' work; "
                    "less suited to long-running exploration."
                ),
            ),
            new_recommendation(
                id="eval_iteration",
                thesis=(
                    "Eval-driven iteration (LangChain 52.8% → 66.5% on "
                    "Terminal Bench 2.0): build a PreCompletionChecklist "
                    "middleware that forces the agent to verify against a "
                    "checklist before declaring done. The eval is the "
                    "self-verification gate."
                ),
                source_url="https://blog.langchain.com/improving-deep-agents-with-harness-engineering/",
                source_label="LangChain: Improving Deep Agents with harness engineering",
                tradeoff=(
                    "Requires the eval to be reliable; bad evals give false "
                    "confidence."
                ),
            ),
        ],
    ),
]


# --------------------------------------------------------------------------- #
# Synthesis: pure function — build the SOT markdown from a decision set.
# --------------------------------------------------------------------------- #


# Precomputed index: round_key -> rec_id -> Recommendation. Built once
# at module load so _rec_for does not scan ROUNDS on every call. Tests
# exercise _rec_for directly as the canonical lookup API.
_REC_INDEX: dict = {
    r["key"]: {rec["id"]: rec for rec in r["recommendations"]} for r in ROUNDS
}


def _rec_for(round_key: str, rec_id: str):
    return _REC_INDEX.get(round_key, {}).get(rec_id)


def _rec_table_row(rec: dict) -> str:
    return (
        f"| {rec['id']} | {rec['thesis']} | {rec['source_url']} |"
    )


def synthesize_sot(decisions: dict) -> str:
    """Build the SOT harness document from a complete decision set."""
    errs = validate_sot_decision_set(decisions)
    if errs:
        return _incomplete_doc(decisions, errs)

    lines: list[str] = []
    lines.append(f"# SOT Harness Document — {decisions['project_name']}")
    lines.append("")
    lines.append(f"> {decisions['idea_one_liner']}")
    lines.append("")
    lines.append(
        f"**Session**: `{decisions['session_id']}`  "
        f"**Generated**: by `/dev-kit:sot-harness-writer`"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for round_obj in ROUNDS:
        dec = decisions["decisions"][round_obj["key"]]
        lines.append(f"## {round_obj['key'].replace('_', ' ').title()}")
        lines.append("")
        lines.append(f"**Question**: {round_obj['question']}")
        lines.append("")
        lines.append("### Recommendations surfaced")
        lines.append("")
        lines.append("| ID | Thesis | Source |")
        lines.append("|---|---|---|")
        for rec in round_obj["recommendations"]:
            lines.append(_rec_table_row(rec))
        lines.append("")
        lines.append(f"### Decision: `{dec['decision']}` → `{dec['recommendation_id']}`")
        lines.append("")
        chosen = _rec_for(round_obj["key"], dec["recommendation_id"])
        if chosen:
            lines.append(f"**Chosen pattern**: {chosen['thesis']}")
            lines.append("")
            lines.append(f"**Source**: {chosen['source_url']}")
            lines.append("")
        if dec["decision"] == "customize" and dec["customize_text"]:
            lines.append("**Customization**:")
            lines.append("")
            lines.append(f"> {dec['customize_text']}")
            lines.append("")
        if dec["note"]:
            lines.append(f"**Note**: {dec['note']}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Selected Patterns (summary)")
    lines.append("")
    lines.append("| Dimension | Pattern ID | Decision |")
    lines.append("|---|---|---|")
    for round_obj in ROUNDS:
        dec = decisions["decisions"][round_obj["key"]]
        lines.append(
            f"| {round_obj['key']} | `{dec['recommendation_id']}` | {dec['decision']} |"
        )
    lines.append("")

    lines.append("## Rejected Patterns")
    lines.append("")
    rejected_count = 0
    for round_obj in ROUNDS:
        dec = decisions["decisions"][round_obj["key"]]
        if dec["decision"] == "reject":
            rec = _rec_for(round_obj["key"], dec["recommendation_id"])
            if rec:
                lines.append(f"- **{round_obj['key']}** rejected `{rec['id']}`: {dec['note'] or '(no reason given)'}")
                rejected_count += 1
    if rejected_count == 0:
        lines.append("(none — all surfaced patterns were accepted or customized)")
    lines.append("")

    lines.append("## Open Questions")
    lines.append("")
    if decisions["open_questions"]:
        for q in decisions["open_questions"]:
            lines.append(f"- {q}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Implementation Phases (sequenced by dependency)")
    lines.append("")
    lines.append(
        "Each of the 5 interview dimensions maps to one implementation "
        "phase. The order is conservative: project_context (Phase 1) before "
        "lifecycle (Phase 2) before verification (Phase 3) before context "
        "(Phase 4) before safety (Phase 5)."
    )
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart LR")
    lines.append("  P1[Phase 1: Project Context] --> P2[Phase 2: Lifecycle]")
    lines.append("  P2 --> P3[Phase 3: Verification]")
    lines.append("  P3 --> P4[Phase 4: Context]")
    lines.append("  P4 --> P5[Phase 5: Safety]")
    lines.append("  P1 --> P3")
    lines.append("```")
    lines.append("")

    lines.append("### Phase 1: Project Context")
    lines.append("")
    pc = decisions["decisions"]["project_context"]
    lines.append(f"- Pattern: `{pc['recommendation_id']}` ({pc['decision']})")
    lines.append("- Deliverables: harness category scaffold + long-running init or single-pass loop per chosen pattern")
    lines.append("")
    lines.append("### Phase 2: Lifecycle")
    lines.append("")
    lc = decisions["decisions"]["lifecycle"]
    lines.append(f"- Pattern: `{lc['recommendation_id']}` ({lc['decision']})")
    lines.append("- Deliverables: init.sh / progress log / feature list or Ralph loop per chosen pattern")
    lines.append("")
    lines.append("### Phase 3: Verification")
    lines.append("")
    v = decisions["decisions"]["verification"]
    lines.append(f"- Pattern: `{v['recommendation_id']}` ({v['decision']})")
    lines.append("- Deliverables: eval suite + self-verification prompts or eval middleware")
    lines.append("")
    lines.append("### Phase 4: Context")
    lines.append("")
    c = decisions["decisions"]["context"]
    lines.append(f"- Pattern: `{c['recommendation_id']}` ({c['decision']})")
    lines.append("- Deliverables: compaction strategy + subagent isolation or filesystem memory")
    lines.append("")
    lines.append("### Phase 5: Safety")
    lines.append("")
    s = decisions["decisions"]["safety"]
    lines.append(f"- Pattern: `{s['recommendation_id']}` ({s['decision']})")
    lines.append("- Deliverables: sandboxing / worktree rules / intent grammar as needed")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Acceptance Criteria (gates before `/dev-kit:build`)")
    lines.append("")
    lines.append("- [ ] All 5 dimensions have a locked decision (A1)")
    lines.append("- [ ] Every accepted recommendation cites a source URL (A2)")
    lines.append("- [ ] Rejected recommendations have a reason (A3)")
    lines.append("- [ ] Open questions are explicit (A4)")
    lines.append("- [ ] Implementation phases are sequenced by dependency (A5)")
    lines.append("")
    lines.append("When all 5 are checked, run:")
    lines.append("")
    lines.append("```bash")
    lines.append(
        f"/dev-kit:plan --from-sot .dev-kit/hand-off/sot-harness-{_safe_session_id(decisions['session_id'])}.md"
    )
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Sources")
    lines.append("")
    seen: set[str] = set()
    for round_obj in ROUNDS:
        dec = decisions["decisions"][round_obj["key"]]
        chosen = _rec_for(round_obj["key"], dec["recommendation_id"])
        if chosen and chosen["source_url"] not in seen:
            lines.append(f"- [{chosen['source_label']}]({chosen['source_url']})")
            seen.add(chosen["source_url"])
    lines.append("")
    return "\n".join(lines)


def _incomplete_doc(decisions: dict, errs: list[str]) -> str:
    return (
        f"# SOT Harness Document — INCOMPLETE\n\n"
        f"**Session**: `{decisions['session_id']}`  "
        f"**Status**: `held` (per MUST-19.1)\n\n"
        f"## Validation errors\n\n"
        + "\n".join(f"- {e}" for e in errs)
        + "\n\n"
        + "Complete the missing rounds in `/dev-kit:sot-harness-writer` and re-run.\n"
    )


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #


def _sot_frontmatter(decisions: dict, status: str) -> str:
    """Render the YAML frontmatter that marks this file as a SOT handoff.

    The discriminator (``handoff_kind: sot``) and ``status`` field let
    the plan skill's consume gate route the file through `--from-sot`
    instead of misinterpreting it as an interview handoff (issue #898).
    """
    lines = [
        "---",
        f"handoff_kind: {SOT_HANDOFF_KIND}",
        f"status: {status}",
        f"session_id: {_safe_session_id(decisions['session_id'])}",
        f"generated_by: {SOT_HANDOFF_GENERATED_BY}",
        "---",
        "",
    ]
    return "\n".join(lines)


def write_sot_handout(decisions: dict, root: Path) -> Path:
    """Write the SOT doc to .dev-kit/hand-off/sot-harness-<session>.md.

    Always carries the typed YAML frontmatter
    (``handoff_kind: sot`` + ``status: locked | held``) so the plan
    skill's consume gate can route it correctly. See issue #898.
    """
    errs = validate_sot_decision_set(decisions)
    safe = _safe_session_id(decisions["session_id"])
    target = root / ".dev-kit" / "hand-off" / f"sot-harness-{safe}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    status = "held" if errs else SOT_HANDOFF_STATUS_LOCKED
    body = synthesize_sot(decisions)
    target.write_text(_sot_frontmatter(decisions, status) + body)
    return target


_SESSION_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_session_id(session_id: str) -> str:
    """Return a filesystem-safe version of ``session_id``.

    Defensive sanitization: the skill flow controls ``session_id`` (default
    ``"default"``) and treats it as trusted, but a programmatic caller could
    pass a traversal sequence. Collapse anything outside ``[A-Za-z0-9._-]``
    to ``_``; truncate to 64 chars; fall back to ``"default"`` on empty.
    """
    cleaned = _SESSION_ID_SAFE_RE.sub("_", session_id).strip("_")
    if not cleaned:
        cleaned = "default"
    return cleaned[:64]


def write_decision_log(
    decisions: dict, rounds_log: list, root: Path
) -> Path:
    """Write the per-round Q+A log to .dev-kit/decision-log-sot-harness/<session>.md."""
    safe = _safe_session_id(decisions["session_id"])
    target = (
        root
        / ".dev-kit"
        / "decision-log-sot-harness"
        / f"{safe}.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        f"# Decision log — sot-harness session {decisions['session_id']}",
        "",
    ]
    for entry in rounds_log:
        lines.append(f"## Round: {entry['round_key']}")
        lines.append("")
        lines.append(f"- Question: {entry['question']}")
        lines.append(f"- User: {entry['user_choice']}")
        if entry["note"]:
            lines.append(f"- Note: {entry['note']}")
        lines.append("")
    target.write_text("\n".join(lines))
    return target
