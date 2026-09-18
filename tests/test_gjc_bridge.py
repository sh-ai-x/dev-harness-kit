from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GJC_DIR = PROJECT_ROOT / ".gjc"


class TestGjcBridgeLayout(unittest.TestCase):
    def test_claude_and_codex_plugin_surfaces_stay_intact(self) -> None:
        claude_manifest = yaml.safe_load(
            (PROJECT_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        codex_manifest = yaml.safe_load(
            (PROJECT_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        hooks_manifest = yaml.safe_load(
            (PROJECT_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )

        self.assertEqual(claude_manifest["skills"], "./skills/")
        self.assertEqual(codex_manifest["skills"], "./skills/")
        self.assertEqual(codex_manifest["hooks"], "./.codex-plugin/hooks/hooks.json")
        self.assertIn("PreToolUse", hooks_manifest["hooks"])
        self.assertIn("PostToolUse", hooks_manifest["hooks"])
        self.assertIn("Stop", hooks_manifest["hooks"])

    def test_project_gjc_skills_symlinks_to_skill_ssot(self) -> None:
        skills = GJC_DIR / "skills"
        self.assertTrue(skills.is_symlink(), ".gjc/skills must be a symlink")
        self.assertEqual(skills.resolve(), (PROJECT_ROOT / "skills").resolve())
        self.assertTrue((skills / "bootstrap" / "SKILL.md").is_file())

    def test_project_gjc_rules_symlinks_to_rule_ssot(self) -> None:
        rules = GJC_DIR / "rules"
        self.assertTrue(rules.is_symlink(), ".gjc/rules must be a symlink")
        self.assertEqual(rules.resolve(), (PROJECT_ROOT / "rules").resolve())
        self.assertTrue((rules / "git-workflow.md").is_file())

    def test_project_gjc_hook_adapter_exists_without_replacing_foreign_hooks(self) -> None:
        adapter = GJC_DIR / "extensions" / "dev-kit-hooks" / "index.js"
        self.assertTrue(adapter.is_file(), "GJC hook adapter extension missing")
        text = adapter.read_text(encoding="utf-8")
        self.assertIn("hooks/hooks.json", text)
        self.assertIn("PreToolUse", text)
        self.assertIn("PostToolUse", text)
        self.assertIn("SessionStart", text)
        self.assertTrue((PROJECT_ROOT / "hooks" / "hooks.json").is_file())

    def test_gitignore_keeps_runtime_gjc_state_out_but_allows_bridge(self) -> None:
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".gjc/*", gitignore)
        self.assertIn("!.gjc/skills", gitignore)
        self.assertIn("!.gjc/rules", gitignore)
        self.assertIn("!.gjc/extensions", gitignore)
        self.assertIn("!.gjc/extensions/**", gitignore)


class TestGjcRuleMetadata(unittest.TestCase):
    def test_rule_files_have_gjc_rulebook_metadata(self) -> None:
        missing: list[str] = []
        for path in sorted((PROJECT_ROOT / "rules").glob("*.md")):
            if path.name == "index.md":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), f"{path} missing frontmatter")
            end = text.find("\n---", 4)
            self.assertNotEqual(end, -1, f"{path} frontmatter not closed")
            frontmatter = yaml.safe_load(text[4:end]) or {}
            if not frontmatter.get("description"):
                missing.append(f"{path.relative_to(PROJECT_ROOT)}: description")
            globs = frontmatter.get("globs")
            if not isinstance(globs, list) or not globs:
                missing.append(f"{path.relative_to(PROJECT_ROOT)}: globs")
        self.assertEqual(missing, [])


class TestGjcHookAdapter(unittest.TestCase):
    def test_adapter_registers_gjc_hooks_and_reads_existing_manifest(self) -> None:
        script = """
import adapter, { devKitGjcHookAdapter } from './.gjc/extensions/dev-kit-hooks/index.js';
const calls = [];
const api = {
  registerFunctionHook(event, handler, options) {
    calls.push({ kind: 'function', event, target: options?.target, capabilities: options?.capabilities ?? [] });
  },
  on(event, handler) {
    calls.push({ kind: 'event', event });
  },
};
adapter(api);
console.log(JSON.stringify({
  calls,
  bashPre: devKitGjcHookAdapter.commandsFor('PreToolUse', 'Bash').length,
  writePost: devKitGjcHookAdapter.commandsFor('PostToolUse', 'Write').length,
  postFailure: devKitGjcHookAdapter.failureResult('PostToolUse', 'boom', 'tool_result'),
  mapped: devKitGjcHookAdapter.asClaudeToolName('bash'),
}));
"""
        result = subprocess.run(
            ["bun", "-e", script],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        data = yaml.safe_load(result.stdout)
        self.assertIn(
            {"kind": "function", "event": "tool_call", "target": "*", "capabilities": ["tool"]},
            data["calls"],
        )
        self.assertIn(
            {"kind": "function", "event": "tool_result", "target": "*", "capabilities": ["tool"]},
            data["calls"],
        )
        self.assertIn({"kind": "event", "event": "session_start"}, data["calls"])
        self.assertIn({"kind": "event", "event": "agent_end"}, data["calls"])
        self.assertGreater(data["bashPre"], 0)
        self.assertGreater(data["writePost"], 0)
        self.assertEqual(data["mapped"], "Bash")
        self.assertTrue(data["postFailure"]["isError"])
        self.assertEqual(data["postFailure"]["content"][0]["type"], "text")
        self.assertIn("PostToolUse", data["postFailure"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
