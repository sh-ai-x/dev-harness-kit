"""test_provider_divergence_wiring.py -- ensures the SessionStart dispatcher
is wired into BOTH .claude-plugin/hooks/hooks.json (the canonical Claude
config at hooks/hooks.json) AND .codex-plugin/hooks/hooks.json, and that the
dispatcher retains the provider-divergence child.

The dual-runtime parity rule: every hook registered for Claude Code MUST
also be registered for Codex. P3-bucket-C owns the full parity sweep
(failing tests/test_hooks_status.py::test_codex_manifest_registers_shared_hook_definition
locks it); this test is a focused additional check that catches the
specific case where a single new hook is added to only one runtime.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _hook_set(hooks_json_path: Path) -> set[str]:
    """Return set of hook basenames directly referenced in `SessionStart`."""
    cfg = json.loads(hooks_json_path.read_text())
    names = set()
    for group in cfg["hooks"].get("SessionStart", []):
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            base = cmd.rsplit("/", 1)[-1]
            names.add(base)
    return names


class TestWiring(unittest.TestCase):
    def test_claude_registers_provider_divergence(self) -> None:
        names = _hook_set(REPO_ROOT / "hooks" / "hooks.json")
        self.assertIn("session-start.sh", names,
                      "missing SessionStart dispatcher in Claude manifest")
        source = (REPO_ROOT / "hooks" / "session-start.sh").read_text(encoding="utf-8")
        self.assertIn("provider-divergence-check.sh", source,
                      "provider-divergence-check.sh missing from SessionStart dispatcher")

    def test_codex_registers_provider_divergence(self) -> None:
        names = _hook_set(REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json")
        self.assertIn("session-start.sh", names,
                      "missing SessionStart dispatcher in Codex manifest")
        source = (REPO_ROOT / "hooks" / "session-start.sh").read_text(encoding="utf-8")
        self.assertIn("provider-divergence-check.sh", source,
                      "provider-divergence-check.sh missing from SessionStart dispatcher")

    def test_codex_registers_review_yml_isolation(self) -> None:
        """Regression: P4 Gap A.

        The review.yml isolation rule (hooks/review-yml-isolation.sh) was
            historically only registered in the Claude manifest. A Codex
            babysit-pr run could land review.yml alongside
        unrelated edits and the CI gate verdict became unreadable.
        """
        cfg = json.loads((REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json").read_text())
        bash_cmds = []
        for group in cfg["hooks"].get("PreToolUse", []):
            if group.get("matcher") == "Bash":
                for h in group.get("hooks", []):
                    bash_cmds.append(h.get("command", ""))
        self.assertTrue(
            any("review-yml-isolation.sh" in c for c in bash_cmds),
            f"review-yml-isolation.sh missing from Codex PreToolUse::Bash. Found: {bash_cmds}",
        )

    def test_dual_runtime_session_start_parity(self) -> None:
        claude = _hook_set(REPO_ROOT / "hooks" / "hooks.json")
        codex = _hook_set(REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json")
        self.assertEqual(claude, codex,
                         "Claude and Codex must use the same SessionStart dispatcher")


if __name__ == "__main__":
    unittest.main()
