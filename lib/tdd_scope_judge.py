#!/usr/bin/env python3
"""Run the local subscription CLI only for paths deferred by policy."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

STATE = ".dev-kit/.tdd-scope.json"
INSTRUCTION = (
    "Classify the request for TDD. Return JSON only with keys "
    "tdd_required (boolean), confidence (number), reason (string). "
    "False means documentation, configuration, one-off script, formatting, "
    "typo, or simple maintenance. True means core behavior, API, business "
    "logic, security, data handling, or meaningful refactoring.\nRequest: "
)
# Per-judge subprocess timeout. Smaller than the hook-level 120s in
# hooks.json so a hung `claude -p` (issue #647: interactive claude is
# alive and holds the singleton) surfaces here first and the caller can
# observe the recorded decision before the hook fires its outer timeout.
JUDGE_TIMEOUT_SECONDS = 45


def _judge_command(prompt: str) -> list[str]:
    """Build the non-interactive command for the selected judge runtime.

    Honors ``DEV_KIT_BUILD_AGENT`` (default ``claude``). When ``codex``,
    uses ``codex exec`` instead of ``claude -p`` — same flag the harness
    step agent honors, so an operator who has ``codex exec`` working can
    use it for the TDD scope judge too (issue #647 Option A). This
    eliminates the asymmetric dependency where the step agent could fall
    back to codex but the judge could not.
    """
    agent = os.environ.get("DEV_KIT_BUILD_AGENT", "claude").strip().lower()
    full_prompt = INSTRUCTION + prompt
    if agent == "codex":
        return [
            "codex", "exec",
            "--output-format", "json",
            full_prompt,
        ]
    if agent == "claude":
        return [
            "claude", "-p",
            "--output-format", "json",
            "--permission-mode", "plan",
            full_prompt,
        ]
    raise ValueError(
        f"unsupported DEV_KIT_BUILD_AGENT={agent!r}; use claude or codex"
    )


def _skip_tdd_requested() -> bool:
    """Issue #647 Option B escape hatch.

    When ``DEV_KIT_SKIP_TDD`` is set to a truthy value (``1``, ``true``,
    ``yes``, case-insensitive), the judge short-circuits and records
    ``tdd_required=False`` so the harness can proceed without TDD.
    Documented as: "may produce lower-quality builds; not for production
    use" — only for unblocking environments where ``claude -p`` is
    unresponsive and ``codex exec`` is unavailable.
    """
    raw = os.environ.get("DEV_KIT_SKIP_TDD", "").strip().lower()
    return raw in ("1", "true", "yes")


def _parse(raw: str) -> dict:
    for text in (raw, json.loads(raw).get("result", "") if raw.startswith("{") else ""):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("tdd_required"), bool):
            return {"tdd_required": value["tdd_required"], "confidence": float(value.get("confidence", 0)), "reason": str(value.get("reason", ""))}
    raise ValueError("invalid TDD judge response")


def _write_state(root: Path, decision: dict) -> None:
    """Atomic state-file write. Keeps the record consistent across all paths."""
    path = root / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n")


def evaluate(prompt: str, root: Path) -> dict:
    """Run the judge for paths deferred by policy. Records the decision.

    Returns the decision dict. The state file ``.dev-kit/.tdd-scope.json``
    is always written — even on SKIP_TDD bypass, even on subprocess
    failure — so downstream consumers (tdd-guard.sh, /dev-kit:build) can
    inspect a stable record instead of inferring intent from a missing
    file.
    """
    if _skip_tdd_requested():
        decision = {
            "tdd_required": False,
            "confidence": 1.0,
            "reason": "DEV_KIT_SKIP_TDD=1 — judge bypassed (issue #647 escape hatch)",
        }
        _write_state(root, decision)
        return decision

    # Hoist _judge_command() out of the try block so an invalid
    # DEV_KIT_BUILD_AGENT raises immediately (issue #647: must fail
    # closed rather than silently swallowing it as a parse failure).
    cmd = _judge_command(prompt)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=JUDGE_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "judge failed")
        decision = _parse(result.stdout)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        decision = {"tdd_required": True, "confidence": 0.0, "reason": f"judge unavailable: {exc}"}
    _write_state(root, decision)
    return decision


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.prompt, args.root.resolve()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
