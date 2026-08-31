#!/usr/bin/env python3
"""
harness_mode_state.py — session-scoped gate resolution for /dev-kit:harness-mode.

State lives at ``.dev-kit/harness-mode.session.json``, reset to ``{"mode": "full"}``
by ``hooks/session-start-harness-mode-reset.sh`` at the start of every session
(new window = strict by default, per design). Correctness gates are hardcoded
to always resolve "on" regardless of file contents — no state file, hand-edited
or not, can disable them.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="harness-mode session state CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get", help="print the resolved value for one gate")
    get_p.add_argument("gate")

    write_p = sub.add_parser("write", help="write the session mode")
    write_p.add_argument("mode", choices=["full", "fast", "custom"])
    write_p.add_argument("--gates", help="JSON object of per-gate overrides (custom mode)")

    show_p = sub.add_parser("show", help="print the full resolved gate table as JSON")
    del show_p

    args = parser.parse_args()
    if args.command == "get":
        print(resolved_gate(args.gate))
        return 0
    if args.command == "write":
        gates = json.loads(args.gates) if args.gates else {}
        write_state(args.mode, gates)
        return 0
    if args.command == "show":
        state = read_state()
        all_gates = sorted(CORRECTNESS_GATES | set(OPTIONAL_GATE_DEFAULTS))
        table = {g: resolved_gate(g) for g in all_gates}
        print(json.dumps({"mode": state.get("mode", "full"), "gates": table}, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
