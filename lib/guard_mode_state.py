#!/usr/bin/env python3
"""
guard_mode_state.py — session-scoped on/off state for the hard-block
Iron Law hooks: `hooks/tdd-guard.sh`, `hooks/worktree-guard.sh`, and
`hooks/git-guard.sh`, plus the
ask-tier push-confirmation bypass used by `hooks/destructive-confirm.sh`
during `/dev-kit:babysit-pr` and `/dev-kit:babysit-pr-local`.

State lives at ``.dev-kit/guard-mode.session.json``. SessionStart applies the
scoped ``DEV_KIT_GUARDS`` policy; the unconfigured default is all three
repository guards off. Unlike harness-mode's optional local hooks,
`tdd_guard`, `worktree_guard`, and `git_guard` are hard PreToolUse blocks
enforcing Iron Law L1 (no prod code without a verification artifact) and the
`rules/git-workflow.md` worktree-isolation/branch rule; `push_confirm` is the
ask-tier bypass for first-push and force-with-lease asks, opt-in only by the
babysit-pr loop lifetime. This module exists
so a session can deliberately and visibly suspend any of them for itself,
never silently and never beyond the current session.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic import atomic_write_json  # noqa: E402

STATE_REL_PATH = Path(".dev-kit") / "guard-mode.session.json"

GUARDS = ("tdd_guard", "worktree_guard", "git_guard", "push_confirm", "fork_pr_confirm")

# The ask-tier fork confirmation is intentionally independent from the
# repository guard policy. Push confirmation stays on by default because it
# is a human confirmation surface, not a repository edit/branch guard.
OPT_IN_GUARDS = frozenset({"fork_pr_confirm"})

POLICY_GUARDS = frozenset({"tdd_guard", "worktree_guard", "git_guard"})

GUARD_DESCRIPTIONS = {
    "tdd_guard": "hooks/tdd-guard.sh — blocks prod code edits without RED evidence (Iron Law L1)",
    "worktree_guard": "hooks/worktree-guard.sh — blocks Edit/Write/MultiEdit in the main checkout (rules/git-workflow.md worktree isolation)",
    "git_guard": "hooks/git-guard.sh — blocks direct main commits, protected pushes, and unsafe branch changes",
    "push_confirm": "hooks/destructive-confirm.sh — ask-tier gate for first-push and force-with-lease; toggled off by /dev-kit:babysit-pr[-local] for the loop lifetime",
    "fork_pr_confirm": "hooks/pr-create-route.sh — ask-tier gate for `gh pr create` when actor_classifier routes to fork_pr_review_environment; default off (silent breadcrumb only)",
}


def _state_path(root: Optional[Path] = None) -> Path:
    return (root or Path(".")) / STATE_REL_PATH


def _default_state(policy: str = "on", source: str = "default",
                   branch_class: str = "unknown") -> dict:
    """Synthesise a fresh state payload for a scoped policy.

    Defaults to Iron Law enforcement ON. The previous `off` default was
    the source of the C7 finding in the LLM-judge verdict for PR #881:
    silent fail-open without audit. The fail-closed default matches
    `rules/git-workflow.md` (Iron Law L1 + worktree-isolation rule) — a
    caller that genuinely wants guards off must set them explicitly via
    `DEV_KIT_GUARDS=off` (shell scope) or the audit trail loses its
    meaning.
    """
    state = {g: ("on" if g in POLICY_GUARDS or g == "push_confirm" else "off") for g in GUARDS}
    for guard in POLICY_GUARDS:
        state[guard] = policy if policy in ("on", "off") else "on"
    state["policy"] = policy if policy in ("on", "off") else "on"
    state["policy_source"] = source or "default"
    state["branch_class"] = branch_class or "unknown"
    return state


def read_state(root: Optional[Path] = None) -> dict:
    """Read state. Missing/corrupt/invalid guard values use the fail-closed default."""
    path = _state_path(root)
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    # Default the resolved policy to "on" so the contract is fail-closed
    # (PR #881 verdict remediation, C7/C8). A legacy state file with no
    # policy field therefore reads as Iron Law enforcement ON, which is
    # the safe direction for the new contract.
    raw_policy = data.get("policy")
    policy = raw_policy if isinstance(raw_policy, str) and raw_policy in ("on", "off") else "on"
    state = _default_state(
        policy,
        data.get("policy_source", "default") if isinstance(data.get("policy_source", "default"), str) else "default",
        data.get("branch_class", "unknown") if isinstance(data.get("branch_class", "unknown"), str) else "unknown",
    )
    for g in GUARDS:
        if data.get(g) in ("on", "off"):
            state[g] = data[g]
    return state


def write_state(overrides: dict, root: Optional[Path] = None) -> dict:
    """Merge `overrides` (guard -> "on"/"off") onto the current state and
    write it atomically. Unknown keys and non "on"/"off" values are dropped
    silently — defense in depth against a bad caller writing garbage that
    would otherwise be interpreted as "off" via a loose truthiness check.
    Returns the resulting state.
    """
    state = read_state(root)
    for g, v in overrides.items():
        if g in GUARDS and v in ("on", "off"):
            state[g] = v
    atomic_write_json(_state_path(root), state)
    return state


def reset_state(root: Optional[Path] = None, *, policy: str = "on",
                policy_source: str = "default",
                branch_class: str = "unknown") -> dict:
    """Apply the scoped policy. Used by the SessionStart hook.

    Default ``policy="on"`` closes the C8 finding (fail-open default
    kwarg). A caller that genuinely wants guards off must pass
    ``policy="off"`` explicitly; otherwise the audit trail records the
    off-state as the deliberate choice rather than a forgotten kwarg.
    """
    state = _default_state(policy, policy_source, branch_class)
    atomic_write_json(_state_path(root), state)
    return state


def resolved_guard(name: str, root: Optional[Path] = None) -> str:
    """Return "on"/"off" for one guard. Unknown guard names resolve "on"
    (fail closed — an unrecognized name must never be read as permission
    to skip enforcement)."""
    if name not in GUARDS:
        return "on"
    return read_state(root).get(name, "on")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="guard-mode session state CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get", help="print the resolved value for one guard")
    get_p.add_argument("guard", choices=GUARDS)

    set_p = sub.add_parser("set", help="set one guard for this session")
    set_p.add_argument("guard", choices=GUARDS)
    set_p.add_argument("value", choices=["on", "off"])

    reset_p = sub.add_parser("reset", help="apply the scoped guard policy")
    reset_p.add_argument("--policy", choices=["on", "off"], default="on")
    reset_p.add_argument("--source", default="default")
    reset_p.add_argument("--branch-class", default="unknown")

    show_p = sub.add_parser("show", help="print the resolved state of every guard")
    show_p.add_argument("--json", action="store_true", help="compact single-line JSON")

    args = parser.parse_args(argv)
    if args.command == "get":
        print(resolved_guard(args.guard))
        return 0
    if args.command == "set":
        write_state({args.guard: args.value})
        return 0
    if args.command == "reset":
        reset_state(policy=args.policy, policy_source=args.source,
                    branch_class=args.branch_class)
        return 0
    if args.command == "show":
        state = read_state()
        out = {
            g: {"value": state[g], "description": GUARD_DESCRIPTIONS[g]}
            for g in GUARDS
        }
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())