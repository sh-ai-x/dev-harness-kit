"""Contract tests for the manifest-level full/lite/undev hook boundary."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "hooks" / "mode-gate.sh"


def _run(mode: str, hook: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DEV_KIT_MODE"] = mode
    return subprocess.run(
        ["bash", str(GATE), str(hook)],
        input="{}\n",
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=10,
    )


def test_full_runs_and_lite_undev_silence_non_lite_hooks() -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", prefix="mode-gate-test-", dir=ROOT / "hooks", delete=False
    ) as fixture:
        hook = Path(fixture.name)
        fixture.write("printf 'hook-ran\\n'\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    try:
        assert _run("full", hook).stdout == "hook-ran\n"
        assert _run("lite", hook).stdout == ""
        assert _run("undev", hook).stdout == ""
    finally:
        hook.unlink(missing_ok=True)


def test_both_manifests_route_real_hooks_through_mode_gate() -> None:
    for relative in ("hooks/hooks.json", ".codex-plugin/hooks/hooks.json"):
        manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        commands = [
            entry["command"]
            for groups in manifest["hooks"].values()
            for group in groups
            for entry in group["hooks"]
        ]
        assert commands
        assert all("mode-gate.sh" in command for command in commands)
