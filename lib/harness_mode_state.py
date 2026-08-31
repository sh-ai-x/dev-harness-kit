#!/usr/bin/env python3
"""
harness_mode_state.py — session-scoped gate resolution for /dev-kit:harness-mode.

State lives at ``.dev-kit/harness-mode.session.json``, reset to ``{"mode": "full"}``
by ``hooks/session-start-harness-mode-reset.sh`` at the start of every session
(new window = strict by default, per design). Correctness gates are hardcoded
to always resolve "on" regardless of file contents — no state file, hand-edited
or not, can disable them.

Terminology (issue #775):
- **Local hook** = the harness-mode-controlled switches below. Fires on the
  developer's machine during a session via PreToolUse/PostToolUse/UserPromptSubmit
  hooks (`hooks/*.sh`) or Python consumers (`lib/tdd_scope_judge.py`,
  `lib/execute.py`). Read by `resolved_gate()` at each invocation.
- **CI workflow gate** (NOT controlled here) = the GH-Actions jobs in
  `.github/workflows/*.yml` (review/security/maintenance/lint/test/validate/
  severity-gate/...). They run on every push regardless of harness-mode and
  are the only thing blocking merge via branch protection.

The `show` subcommand groups local hooks by category
(correctness / quality / style / process) and surfaces a `ci_gates_notice`
pointer so readers know the two surfaces are distinct.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic import atomic_write_json  # noqa: E402

STATE_REL_PATH = Path(".dev-kit") / "harness-mode.session.json"

# Never opt-out-able, regardless of mode or hand-edited state file (Iron Law:
# these are the gates whose failure CI cannot recover from — see the
# workflow-fast-mode-lean proposal §1).
CORRECTNESS_GATES = frozenset({
    "stop_verify",
    "secret_scan",
    "intent_integrity",
    "gh_ci_required",
})

# Optional gates the /dev-kit:harness-mode picker exposes. `fast` sets every
# one of these to its "off" value in one shot; `full` (the default) leaves
# them all at their "on" value.
OPTIONAL_GATE_DEFAULTS = {
    "tdd_scope_judge": {"full": "on", "fast": "off"},
    "slop_detector": {"full": "on", "fast": "off"},
    "pre_commit_review": {"full": "on", "fast": "off"},
    "maintenance": {"full": "on", "fast": "off"},
    "security_owasp": {"full": "full", "fast": "quick"},
    "babysit_pr": {"full": "full", "fast": "manual"},
}

# Per-gate metadata for the new grouped `show` output (issue #775).
# - `type`: always "local_hook" for harness-mode-controlled switches (vs the
#   out-of-scope "ci_workflow_gate" type that would come from `lib/ci_gates`).
# - `category`: bucket for the grouped `show` output. One of GROUPED_LOCAL_HOOKS.
# - `description`: one-line explainer rendered next to each gate in `show`
#   output AND in the picker table rows (see skills/harness-mode/SKILL.md).
GATE_CATEGORIES = {
    # correctness (always on, never offered in picker)
    "stop_verify": {
        "type": "local_hook",
        "category": "correctness",
        "description": "L3 evidence: refuse 'done' without quoted exit code + test count + log",
    },
    "secret_scan": {
        "type": "local_hook",
        "category": "correctness",
        "description": "credential-leak detection on every Write/Edit",
    },
    "intent_integrity": {
        "type": "local_hook",
        "category": "correctness",
        "description": "plan-vs-execution drift detection (high-severity intent gates)",
    },
    "gh_ci_required": {
        "type": "local_hook",
        "category": "correctness",
        "description": "refuse local edits that would break GH-Actions (e.g. stale plugin.json version)",
    },
    # quality (picker-offered)
    "tdd_scope_judge": {
        "type": "local_hook",
        "category": "quality",
        "description": "TDD scope judge before each build step (skipping defers to CI test gate)",
    },
    "security_owasp": {
        "type": "local_hook",
        "category": "quality",
        "description": "local security scan depth this session (full 10-dim / quick / off)",
    },
    # style (picker-offered)
    "slop_detector": {
        "type": "local_hook",
        "category": "style",
        "description": "TODO/stub/placeholder markers on every Write/Edit (skipping defers to l4-todo-scan.sh + CI)",
    },
    "maintenance": {
        "type": "local_hook",
        "category": "style",
        "description": "code-sanity (CC/OE/VM) gate, locally (CI `/dev-kit:maintenance` always re-runs on PRs)",
    },
    # process (picker-offered)
    "pre_commit_review": {
        "type": "local_hook",
        "category": "process",
        "description": "codex:review before each commit (local fast-feedback layer)",
    },
    "babysit_pr": {
        "type": "local_hook",
        "category": "process",
        "description": "/dev-kit:babysit-pr behavior (full auto-fix loop / manual one `gh pr checks` dump)",
    },
}

# Ordered list of category buckets for the grouped `show` output. The order
# is stable so `jq '.local_hooks | keys' | sort` consumers see the same
# sequence on every run.
GROUPED_LOCAL_HOOKS = ("correctness", "quality", "style", "process")

# Reverse index: category → list of gate keys. Built once at import time so
# `show` doesn't rebuild it per call.
GATE_CATEGORIES_BY_CATEGORY: dict[str, list[str]] = {cat: [] for cat in GROUPED_LOCAL_HOOKS}
for _gate, _entry in GATE_CATEGORIES.items():
    GATE_CATEGORIES_BY_CATEGORY[_entry["category"]].append(_gate)

# String surfaced in `show` output to remind readers that CI workflow gates
# are NOT toggled by harness-mode. Kept here as a constant so the wording
# is stable across runs (and tests can assert it).
CI_GATES_NOTICE = (
    "CI workflow gates (.github/workflows/*.yml) are NOT toggled by "
    "harness-mode. They run on every push regardless of session mode and "
    "are the only thing blocking merge via branch protection."
)


def _state_path(root: Optional[Path] = None) -> Path:
    return (root or Path(".")) / STATE_REL_PATH


def read_state(root: Optional[Path] = None) -> dict:
    """Read the session state file. Missing or corrupt -> safe default (full)."""
    path = _state_path(root)
    if not path.exists():
        return {"mode": "full", "gates": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "full", "gates": {}}
    if not isinstance(data, dict) or data.get("mode") not in ("full", "fast", "custom"):
        return {"mode": "full", "gates": {}}
    data.setdefault("gates", {})
    return data


def write_state(mode: str, gates: Optional[dict] = None, root: Optional[Path] = None) -> None:
    """Atomic write of the session state file.

    ``mode`` is one of "full", "fast", "custom". ``gates`` is a per-gate
    override map (used by "custom"); ignored keys inside CORRECTNESS_GATES
    are dropped before writing so a bad caller cannot even persist an
    attempt to disable a correctness gate.
    """
    if mode not in ("full", "fast", "custom"):
        raise ValueError(f"invalid mode: {mode!r}")
    clean_gates = {
        k: v for k, v in (gates or {}).items() if k not in CORRECTNESS_GATES
    }
    atomic_write_json(_state_path(root), {"mode": mode, "gates": clean_gates})


def resolved_gate(name: str, root: Optional[Path] = None) -> str:
    """Return the effective value for one gate: "on" / "off" / "full" / "quick" / "manual".

    Correctness gates always resolve "on" — this is the single enforcement
    point for the harness-mode correctness guarantee.
    """
    if name in CORRECTNESS_GATES:
        return "on"
    state = read_state(root)
    if name in state.get("gates", {}):
        return state["gates"][name]
    mode = state.get("mode", "full")
    defaults = OPTIONAL_GATE_DEFAULTS.get(name, {"full": "on", "fast": "off"})
    return defaults.get(mode, defaults["full"])


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    `argv` is parsed for testability — defaults to `sys.argv[1:]` when
    invoked as `python -m lib.harness_mode_state ...`. Tests pass an
    explicit list so they don't need to mutate `sys.argv`.
    """
    parser = argparse.ArgumentParser(description="harness-mode session state CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get", help="print the resolved value for one gate")
    get_p.add_argument("gate")

    write_p = sub.add_parser("write", help="write the session mode")
    write_p.add_argument("mode", choices=["full", "fast", "custom"])
    write_p.add_argument("--gates", help="JSON object of per-gate overrides (custom mode)")

    show_p = sub.add_parser(
        "show",
        help="print local hooks grouped by category + ci_gates_notice",
    )
    show_p.add_argument(
        "--json",
        action="store_true",
        help="emit compact single-line JSON (default: pretty-printed)",
    )
    show_p.add_argument(
        "--root",
        default=None,
        help="project root for .dev-kit/harness-mode.session.json (default: cwd)",
    )

    args = parser.parse_args(argv)
    if args.command == "get":
        print(resolved_gate(args.gate))
        return 0
    if args.command == "write":
        gates = json.loads(args.gates) if args.gates else {}
        write_state(args.mode, gates)
        return 0
    if args.command == "show":
        root = Path(getattr(args, "root")) if getattr(args, "root", None) else None
        state = read_state(root)
        out = _build_show_output(state, root=root)
        if getattr(args, "json", False):
            print(json.dumps(out, sort_keys=True))
        else:
            print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    return 1


def _build_show_output(state: dict, root: Optional[Path] = None) -> dict:
    """Build the structured `show` output (issue #775).

    Shape:
      {
        "mode": <mode string>,
        "local_hooks": {
          <category>: {
            <gate>: {"value": ..., "type": "local_hook", "description": ...},
            ...
          },
          ...
        },
        "gates": {<gate>: <value>, ...},   # legacy flat alias
        "ci_gates_notice": <string>,
      }

    The flat `gates:` alias is kept so legacy consumers (e.g. a downstream
    script that does `python -m lib.harness_mode_state show | jq '.gates'`)
    do not break in this release. New consumers should prefer
    `.local_hooks.<category>.<gate>.value`.

    `root` is forwarded to `resolved_gate()` so the per-gate values come
    from the SAME state file the caller passed to `read_state()`; without
    this, callers that point at a non-cwd state file (the `-root` CLI flag,
    the test harness) would see `state["mode"]` from one file and per-gate
    values from another.
    """
    mode = state.get("mode", "full")
    all_gates = CORRECTNESS_GATES | set(OPTIONAL_GATE_DEFAULTS)
    local_hooks: dict[str, dict[str, dict]] = {cat: {} for cat in GROUPED_LOCAL_HOOKS}
    flat: dict[str, str] = {}
    for gate in sorted(all_gates):
        entry = GATE_CATEGORIES[gate]
        value = resolved_gate(gate, root=root)
        record = {
            "value": value,
            "type": entry["type"],
            "description": entry["description"],
        }
        local_hooks[entry["category"]][gate] = record
        flat[gate] = value
    return {
        "mode": mode,
        "local_hooks": local_hooks,
        "gates": flat,  # legacy alias; new code should use .local_hooks
        "ci_gates_notice": CI_GATES_NOTICE,
    }


if __name__ == "__main__":
    sys.exit(main())
