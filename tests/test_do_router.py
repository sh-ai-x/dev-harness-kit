from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.context_budget import validate_handoff
from lib.do_router import build_envelope, resolve_route

ROOT = Path(__file__).parent.parent


def _resolved(_root: Path) -> tuple[str, str, str, str]:
    return "full", "test", "on", "test"


def test_route_requires_all_three_acceptance_fields(tmp_path: Path) -> None:
    envelope = build_envelope(
        "implement a change", target="", acceptance="", root=ROOT, resolver=_resolved
    )
    assert envelope["status"] == "HOLD"
    assert envelope["reason"] == "missing_specs"
    assert set(envelope["missing_specs"]) == {"target", "acceptance"}
    assert envelope["owner"] == "human"


def test_build_route_emits_independent_mode_and_team_and_bounded_handoff() -> None:
    envelope = build_envelope(
        "implement the parser fix",
        target="lib/parser.py",
        acceptance="pytest tests/test_parser.py -q passes",
        root=ROOT,
        resolver=_resolved,
        summary="use the existing parser contract",
        artifact_refs=("docs/contract.md",),
    )
    assert envelope["status"] == "DELEGATED"
    assert envelope["route_id"] == "build"
    assert envelope["mode"] == "full"
    assert envelope["team_toggle"] == "on"
    assert "transcript" not in envelope["handoff"]
    validate_handoff(envelope["handoff"])


def test_undev_is_a_fail_closed_hold() -> None:
    envelope = build_envelope(
        "implement the parser fix",
        target="lib/parser.py",
        acceptance="pytest passes",
        root=ROOT,
        resolver=lambda _root: ("undev", "test", "on", "test"),
    )
    assert envelope["status"] == "HOLD"
    assert envelope["reason"] == "mode_disabled"
    assert envelope["route_id"] == "unresolved"


def test_ambiguous_intent_is_not_guessed() -> None:
    envelope = build_envelope(
        "review and implement the security fix",
        target="the PR diff",
        acceptance="review report and tests pass",
        root=ROOT,
        resolver=_resolved,
    )
    assert envelope["status"] == "HOLD"
    assert envelope["reason"] == "ambiguous_owner"
    assert set(envelope["ambiguity"]) == {"review", "security", "build"}


def test_explicit_route_can_be_used_when_intent_is_generic() -> None:
    route, details = resolve_route("do the requested work", route_id="build")
    assert route == "build"
    assert details == []


def test_cli_emits_json_and_nonzero_hold_exit(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lib.do_router",
            "--intent",
            "implement a fix",
            "--target",
            "lib/a.py",
            "--acceptance",
            "tests pass",
            "--root",
            str(ROOT),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "DELEGATED"


@pytest.mark.parametrize("route", ["not-a-route", ""])
def test_invalid_explicit_route_holds(route: str, tmp_path: Path) -> None:
    envelope = build_envelope(
        "implement a fix",
        target="lib/a.py",
        acceptance="tests pass",
        root=ROOT,
        route_id=route or None,
        resolver=_resolved,
    )
    if route:
        assert envelope["status"] == "HOLD"
