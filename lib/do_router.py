"""Deterministic implementation for the unified ``/dev-kit:do`` entrypoint.

The slash skill is intentionally declarative.  This module owns the small,
testable part that must be executable: validating the request contract,
resolving the independent mode/team settings, and emitting one immutable
route envelope plus a bounded handoff.  It does not invoke a child skill or
write configuration; static hooks and the child skill remain authoritative.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from lib.context_budget import (
    CONTEXT_SCHEMA_VERSION,
    MAX_ARTIFACT_REFS,
    MAX_EVENT_BYTES,
    MAX_EXCERPT_CHARS,
    MAX_SUMMARY_CHARS,
    build_handoff,
    validate_handoff,
)

SCHEMA_VERSION = 1
ROUTE_VERSION = "1.0.0"
POLICY_VERSION = "static-ssot-v1"
DIRECTIVE_REF = "docs/core/DEV-HARNESS-CORE.md"
DIRECTIVE_VERSION = "1.0.0"

ROUTE_OWNERS: dict[str, str] = {
    "research": "skills/research/SKILL.md",
    "plan": "skills/plan/SKILL.md",
    "build-debug": "skills/build-debug/SKILL.md",
    "build": "skills/build/SKILL.md",
    "review": "skills/review/SKILL.md",
    "security": "skills/security/SKILL.md",
    "babysit-pr": "skills/babysit-pr/SKILL.md",
    "ship": "skills/ship/SKILL.md",
    "ralph-attended": "skills/ralph/SKILL.md",
    "mode-config": "skills/mode/SKILL.md",
    "team-config": "skills/team/SKILL.md",
}

_ROUTE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mode-config", ("dev_kit_mode", "mode-config", "모드 설정", "모드 확인")),
    ("team-config", ("dev_kit_team", "team-config", "팀 설정", "팀 토글")),
    ("ralph-attended", ("ralph", "end-to-end", "end to end", "자율 루프", "전체 루프")),
    ("babysit-pr", ("babysit", "pr monitor", "pr 모니터", "pr 감시", "pr 수리")),
    ("ship", ("release", "릴리스", "tag", "태그", "ship")),
    ("security", ("security", "보안", "owasp", "security review")),
    ("review", ("review", "리뷰", "검토", "code review", "diff review")),
    ("research", ("research", "조사", "근거 조사", "citations", "인용")),
    ("plan", ("plan", "계획", "prd")),
    (
        "build-debug",
        ("debug", "build-debug", "디버그", "diagnose", "진단", "재현", "regression"),
    ),
    ("build", ("build", "구현", "implement", "fix", "수정", "변경", "개발")),
)


def _load_cli_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load resolver: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolver(name: str) -> Any:
    root = Path(__file__).resolve().parent.parent
    return _load_cli_module(name, root / "bin" / f"dev_kit_{name}.py")


def _resolve_settings(root: Path) -> tuple[str, str, str, str]:
    """Resolve mode and team using the existing authoritative resolvers."""
    mode_cli = _resolver("mode")
    team_cli = _resolver("team")
    mode, mode_source = mode_cli._resolve_mode(root)
    team, team_source = team_cli._resolve_team(root)
    if mode not in {"full", "lite", "undev"}:
        raise ValueError(f"invalid resolved mode: {mode!r}")
    if team not in {"on", "off"}:
        raise ValueError(f"invalid resolved team toggle: {team!r}")
    return mode, mode_source, team, team_source


def _project_root(target: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("target must be inside a git worktree")
    return Path(result.stdout.strip()).resolve()


def _stable_id(prefix: str, *parts: str) -> str:
    joined = "\x1f".join(part.strip() for part in parts)
    return f"{prefix}-{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:24]}"


def _hint_spans(intent: str, hint: str) -> tuple[tuple[int, int], ...]:
    """Return whole-token matches, excluding substring-only occurrences."""
    pattern = rf"(?<!\w){re.escape(hint.casefold())}(?!\w)"
    return tuple(match.span() for match in re.finditer(pattern, intent.casefold()))


def _matches(intent: str, route_id: str) -> bool:
    return any(
        _hint_spans(intent, hint)
        for candidate, hints in _ROUTE_HINTS
        if candidate == route_id
        for hint in hints
    )


def _route_candidates(intent: str) -> list[str]:
    """Resolve lexical candidates while preferring explicit compound hints."""
    matches: dict[str, tuple[tuple[int, int, str], ...]] = {}
    for candidate, hints in _ROUTE_HINTS:
        candidate_matches = tuple(
            (start, end, hint)
            for hint in hints
            for start, end in _hint_spans(intent, hint)
        )
        if candidate_matches:
            matches[candidate] = candidate_matches

    compound_matches = tuple(
        (owner, start, end)
        for owner, candidate_matches in matches.items()
        for start, end, hint in candidate_matches
        if len(re.findall(r"\w+", hint, flags=re.UNICODE)) > 1
    )
    candidates = set(matches)
    for candidate, candidate_matches in matches.items():
        if all(
            any(
                owner != candidate and start <= match_start and match_end <= end
                for owner, start, end in compound_matches
            )
            for match_start, match_end, _hint in candidate_matches
        ):
            candidates.discard(candidate)
    return [candidate for candidate, _hints in _ROUTE_HINTS if candidate in candidates]


def resolve_route(intent: str, *, route_id: str | None = None) -> tuple[str | None, list[str]]:
    """Return a route or the candidate routes that make the request unsafe."""
    if route_id is not None:
        if route_id not in ROUTE_OWNERS:
            return None, [f"unknown route_id: {route_id}"]
        return route_id, []
    candidates = _route_candidates(intent)
    # A generic implementation request has one natural owner.  Only an
    # explicit signal can select the cross-skill Ralph owner.
    if len(candidates) == 1:
        return candidates[0], []
    if not candidates:
        return None, ["no owner matched the intent"]
    return None, candidates


def _scope(target: str, acceptance: str) -> dict[str, str]:
    return {"target": target.strip(), "acceptance": acceptance.strip()}


def _context_budget() -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "context_mode": "artifact_ref",
        "max_excerpt_chars": MAX_EXCERPT_CHARS,
        "max_summary_chars": MAX_SUMMARY_CHARS,
        "max_artifact_refs": MAX_ARTIFACT_REFS,
        "max_event_bytes": MAX_EVENT_BYTES,
        "full_transcript_reinjection": False,
    }


def build_envelope(
    intent: str,
    *,
    target: str,
    acceptance: str,
    root: Path | str,
    route_id: str | None = None,
    summary: str = "",
    artifact_refs: tuple[str, ...] = (),
    checkpoint: str = "",
    failure_signature: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    replay_tokens: int | None = None,
    resolver: Callable[[Path], tuple[str, str, str, str]] | None = None,
) -> dict[str, Any]:
    """Emit one route envelope and bounded handoff, or a fail-closed HOLD."""
    intent = (intent or "").strip()
    target = (target or "").strip()
    acceptance = (acceptance or "").strip()
    root_path = _project_root(Path(root).resolve())
    request_id = _stable_id("req", intent, target, acceptance, str(root_path))
    run_id = _stable_id("do", intent, target, acceptance, str(root_path))

    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "intent_ref": _stable_id("intent", intent, target, acceptance),
        "directive_ref": DIRECTIVE_REF,
        "directive_version": DIRECTIVE_VERSION,
        "route_version": ROUTE_VERSION,
        "scope": _scope(target, acceptance),
        "worktree": {"kind": "current", "ref": str(root_path)},
        "policy_version": POLICY_VERSION,
        "context_budget": _context_budget(),
    }

    missing: list[str] = []
    if not intent:
        missing.append("intent")
    if not target:
        missing.append("target")
    if not acceptance:
        missing.append("acceptance")
    if missing:
        return _hold(base, reason="missing_specs", missing_specs=missing)

    try:
        mode, mode_source, team, team_source = (resolver or _resolve_settings)(root_path)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return _hold(base, reason="settings_resolution_failed", missing_specs=[str(exc)])
    base.update({"mode": mode, "mode_source": mode_source, "team_toggle": team, "team_source": team_source})
    route, details = resolve_route(intent, route_id=route_id)
    if route is None:
        if details and all(item in ROUTE_OWNERS for item in details):
            return _hold(base, reason="ambiguous_owner", ambiguity=details)
        return _hold(base, reason="unresolved_owner", missing_specs=details)
    if mode == "undev":
        return _hold(base, reason="mode_disabled", missing_specs=["DEV_KIT_MODE must be full or lite"])

    base.update({
        "status": "DELEGATED",
        "route_id": route,
        "owner": ROUTE_OWNERS[route],
    })
    handoff = build_handoff(
        intent_ref=base["intent_ref"],
        checkpoint=checkpoint,
        failure_signature=failure_signature,
        artifact_refs=artifact_refs,
        summary=summary,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        replay_tokens=replay_tokens,
    )
    validate_handoff(handoff)
    base["handoff"] = handoff
    return base


def _hold(base: dict[str, Any], *, reason: str, **details: list[str]) -> dict[str, Any]:
    base.update({
        "status": "HOLD",
        "reason": reason,
        "route_id": "unresolved",
        "owner": "human",
    })
    base.update(details)
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="resolve a bounded /dev-kit:do route")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--acceptance", required=True)
    parser.add_argument("--route")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--summary", default="")
    args = parser.parse_args(argv)
    try:
        envelope = build_envelope(
            args.intent,
            target=args.target,
            acceptance=args.acceptance,
            root=args.root,
            route_id=args.route,
            summary=args.summary,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
    return 0 if envelope["status"] == "DELEGATED" else 4


if __name__ == "__main__":
    sys.exit(main())
