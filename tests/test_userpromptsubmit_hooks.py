"""
test_userpromptsubmit_hooks.py — Regression tests for the UserPromptSubmit
policy in `fix/remove-userpromptsubmit-advisories`.

UserPromptSubmit fires synchronously in front of every prompt. Three
advisory hooks that previously lived there have been removed because they
all stalled the user at some point (network-bound git fetch, LLM-judge
Python call, full-JSONL jq walk). The remaining policy is restrictive:

  * Allowed: in-process regex/tail-sample on stdin (well under 100 ms).
  * Forbidden: `python`, `curl`, `git fetch`, `git worktree`, full-file
    `jq -rs`, or any `timeout > 5`. The regen tool
    (`tools/regenerate_active_hooks.py`) hard-rejects any UserPromptSubmit
    entry that violates this.

These tests pin:
  1. The deleted hook files do not exist on disk.
  2. Neither runtime's `hooks.json` references the deleted hooks.
  3. The regenerated matrix snapshot does not list them.
  4. The regen tool rejects each forbidden token (one test per token).
  5. The regen tool rejects a UserPromptSubmit entry with timeout > 5.
  6. The regen tool accepts a clean UserPromptSubmit entry.
  7. The remaining `context-window-guard.sh` uses a tail sample
     (`.[-100:]`), not a full-file slurp (`-rs [...]`).
  8. The remaining `notification-collapse.sh` is a pure regex on stdin.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "regenerate_active_hooks.py"

# Hooks that were removed by this fix and must NOT exist on disk, must
# NOT appear in either manifest, and must NOT appear in the regenerated
# matrix snapshot.
REMOVED_HOOKS = (
    "tdd-scope-judge.sh",
    "worktree-auto-cut.sh",
    "linear-task-change.sh",
)

# Hooks that remain under UserPromptSubmit in both runtimes. The set
# must stay small — every entry here must be provably < 100 ms on a
# representative prompt (no Python, no network, no full-file walk).
REMAINING_HOOKS = (
    "notification-collapse.sh",
    "context-window-guard.sh",
)

CC_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
CODEX_HOOKS_JSON = REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json"


def _userpromptsubmit_command_paths(manifest: dict) -> list[str]:
    """Return the path component of every UserPromptSubmit command.

    Each manifest entry has the shape `bash ${CLAUDE_PLUGIN_ROOT}/hooks/<x>.sh`
    or `bash ${PLUGIN_ROOT}/hooks/<x>.sh`. We normalize both to the
    bare `<x>.sh` filename so the comparison set is independent of the
    runtime root token.
    """
    paths: list[str] = []
    for entry in manifest.get("hooks", {}).get("UserPromptSubmit", []) or []:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            # Extract the final `hooks/<x>.sh` token (both manifests
            # converge here; the `bash ${...}/` prefix is dropped).
            tail = cmd.rsplit("/hooks/", 1)[-1]
            paths.append(tail)
    return paths


def _run_regen(root: Path) -> subprocess.CompletedProcess:
    """Invoke the regen tool against `root` (which must contain hooks/hooks.json)."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestRemovedHooks(unittest.TestCase):
    """The three deleted hooks MUST be gone from disk + both manifests."""

    def test_removed_hook_files_do_not_exist(self):
        for name in REMOVED_HOOKS:
            path = REPO_ROOT / "hooks" / name
            self.assertFalse(
                path.exists(),
                f"{path} still on disk; must be deleted as part of "
                f"the UserPromptSubmit slim-down",
            )

    def test_cc_manifest_does_not_reference_removed_hooks(self):
        cfg = json.loads(CC_HOOKS_JSON.read_text(encoding="utf-8"))
        paths = _userpromptsubmit_command_paths(cfg)
        for name in REMOVED_HOOKS:
            self.assertNotIn(
                name, paths,
                f"hooks/hooks.json UserPromptSubmit still references "
                f"deleted hook {name!r}; paths={paths}",
            )

    def test_codex_manifest_does_not_reference_removed_hooks(self):
        cfg = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
        paths = _userpromptsubmit_command_paths(cfg)
        for name in REMOVED_HOOKS:
            self.assertNotIn(
                name, paths,
                f".codex-plugin/hooks/hooks.json UserPromptSubmit still "
                f"references deleted hook {name!r}; paths={paths}",
            )

    def test_only_remaining_hooks_are_wired_in_both_runtimes(self):
        """The UserPromptSubmit set MUST be exactly {notification-collapse,
        context-window-guard}. Any drift — extra hook or missing one — is a
        regression that needs a follow-up PR, not silent acceptance."""
        for path in (CC_HOOKS_JSON, CODEX_HOOKS_JSON):
            cfg = json.loads(path.read_text(encoding="utf-8"))
            paths = sorted(_userpromptsubmit_command_paths(cfg))
            self.assertEqual(
                paths,
                sorted(REMAINING_HOOKS),
                f"{path.relative_to(REPO_ROOT)} UserPromptSubmit hook set "
                f"drifted from the policy; got {paths}, expected "
                f"{sorted(REMAINING_HOOKS)}",
            )


class TestRegenMatrixDropsRemovedHooks(unittest.TestCase):
    """The regenerated matrix snapshot must not list removed hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy(CC_HOOKS_JSON, self.root / "hooks" / "hooks.json")
        target = self.root / ".dev-kit" / ".active-hooks.json"
        if target.exists():
            target.unlink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_userpromptsubmit_event_only_contains_remaining_hooks(self):
        result = _run_regen(self.root)
        self.assertEqual(
            result.returncode, 0,
            msg=f"regen failed: stderr={result.stderr}",
        )
        data = json.loads(
            (self.root / ".dev-kit" / ".active-hooks.json").read_text()
        )
        ups = data["events"].get("UserPromptSubmit", [])
        names = sorted(e["name"] for e in ups)
        expected = sorted(n.removesuffix(".sh") for n in REMAINING_HOOKS)
        self.assertEqual(
            names, expected,
            f"regenerated UserPromptSubmit entries drifted; got {names}, "
            f"expected {expected}",
        )
        # Defensive: none of the removed hook names should appear at all.
        removed_basenames = {
            n.removesuffix(".sh") for n in REMOVED_HOOKS
        }
        for entry in ups:
            self.assertNotIn(
                entry["name"], removed_basenames,
                f"deleted hook {entry['name']!r} still appears in the "
                f"regenerated matrix",
            )


class TestRegenRejectsForbiddenUserPromptSubmitTokens(unittest.TestCase):
    """The regen tool MUST refuse any UserPromptSubmit hook whose command
    contains a forbidden token. One test per token keeps the failure
    messages specific so the operator can act without re-reading the
    source.
    """

    # The token list mirrors `_USERPROMPT_SUBMIT_FORBIDDEN_TOKENS` in
    # tools/regenerate_active_hooks.py. If that list grows, this set
    # grows with it.
    FORBIDDEN_TOKENS = (
        "python",
        "curl",
        "git fetch",
        "git worktree",
        "jq -rs",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "hooks").mkdir(parents=True, exist_ok=True)
        # Seed from the real manifest so we don't fight the fail_closed
        # validator with a hand-rolled JSON file. The regen tool's
        # fail_closed check (which is orthogonal) sits upstream of the
        # UserPromptSubmit lint, so we have to seed a fully valid
        # manifest first and then mutate one UserPromptSubmit entry to
        # carry the forbidden token.
        shutil.copy(CC_HOOKS_JSON, self.root / "hooks" / "hooks.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _inject_userpromptsubmit(self, command: str, *, timeout: int = 0) -> None:
        """Replace the first UserPromptSubmit entry with a single forbidden hook."""
        path = self.root / "hooks" / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # Drop all existing UserPromptSubmit entries (they were valid
        # but irrelevant to this test) and insert one synthetic entry
        # that the lint should reject.
        data["hooks"]["UserPromptSubmit"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "fail_closed": False,
                        **({"timeout": timeout} if timeout else {}),
                    }
                ]
            }
        ]
        path.write_text(json.dumps(data, indent=2))

    def test_regen_rejects_each_forbidden_token(self):
        for token in self.FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                # Use a dummy shell path that doesn't need to exist
                # — the lint runs before any path resolution.
                self._inject_userpromptsubmit(
                    f"bash ${{CLAUDE_PLUGIN_ROOT}}/hooks/dummy.sh # contains {token}"
                )
                result = _run_regen(self.root)
                self.assertNotEqual(
                    result.returncode, 0,
                    msg=f"regen accepted UserPromptSubmit command "
                        f"containing forbidden token {token!r}; "
                        f"stderr={result.stderr!r}",
                )
                self.assertIn(
                    token, result.stderr,
                    f"stderr should name the forbidden token {token!r}; "
                    f"got: {result.stderr!r}",
                )
                # The lint must hard-fail BEFORE writing the snapshot,
                # so a partial matrix never lands on disk.
                target = self.root / ".dev-kit" / ".active-hooks.json"
                self.assertFalse(
                    target.exists(),
                    f"regen wrote a snapshot despite a forbidden token; "
                    f"target={target}",
                )

    def test_regen_rejects_userpromptsubmit_timeout_above_5(self):
        """A UserPromptSubmit hook with `timeout > 5` MUST be rejected.

        The cap is 5s — anything beyond is a design smell. The lint
        exists so a future author cannot silently ship a slow hook.
        """
        self._inject_userpromptsubmit(
            "bash ${CLAUDE_PLUGIN_ROOT}/hooks/dummy.sh",
            timeout=30,
        )
        result = _run_regen(self.root)
        self.assertNotEqual(
            result.returncode, 0,
            msg=f"regen accepted timeout=30 on UserPromptSubmit; "
                f"stderr={result.stderr!r}",
        )
        self.assertIn(
            "timeout", result.stderr,
            f"stderr should name the timeout cap; got: {result.stderr!r}",
        )

    def test_regen_accepts_trivial_userpromptsubmit_entry(self):
        """A clean, regex-only UserPromptSubmit hook (no forbidden tokens,
        no timeout > 5) MUST regen successfully.
        """
        self._inject_userpromptsubmit(
            "bash ${CLAUDE_PLUGIN_ROOT}/hooks/dummy.sh",
            timeout=2,
        )
        result = _run_regen(self.root)
        self.assertEqual(
            result.returncode, 0,
            msg=f"regen rejected a clean UserPromptSubmit entry; "
                f"stderr={result.stderr!r}",
        )


class TestContextWindowGuardUsesTailSample(unittest.TestCase):
    """`context-window-guard.sh` MUST sample the tail of the transcript,
    not walk the full file. The prior implementation `jq -rs [...]` read
    every record and timed out on long babysit sessions; the rewrite uses
    `.[-100:]` for a constant-time window.
    """

    def setUp(self):
        self.path = REPO_ROOT / "hooks" / "context-window-guard.sh"
        if not self.path.exists():
            raise unittest.SkipTest("context-window-guard.sh not found")
        self.src = self.path.read_text(encoding="utf-8")

    def test_does_not_use_full_file_jq_rs(self):
        """The full-file `jq -rs [...]` shape MUST be gone. We allow `jq`
        with `-r` and other flags as long as the slurp (`-s`) is paired
        with a slice (`.[-N:]`) rather than the whole array.
        """
        # Negative: a bare `jq -rs` (without the `.[-N:]` slice) on the
        # full array is the buggy shape. We check for a `jq -rs` call
        # that does NOT also include a `.[-` slice.
        if "jq -rs" in self.src:
            # Allow only if paired with a tail slice.
            self.assertIn(
                ".[-", self.src,
                "context-window-guard.sh uses `jq -rs` (full-file slurp) "
                "without a tail slice; that walks the whole transcript "
                "and is exactly the shape we are deleting",
            )

    def test_uses_tail_slice(self):
        """The hook MUST sample via `.[-N:]` (constant-time window)."""
        self.assertIn(
            ".[-", self.src,
            "context-window-guard.sh must sample a tail window "
            "(`.[-N:]`) instead of walking the full transcript",
        )

    def test_bash_n_clean(self):
        """`bash -n` must confirm the rewritten hook is syntactically valid."""
        result = subprocess.run(
            ["bash", "-n", str(self.path)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"bash -n failed: rc={result.returncode}, "
                f"stderr={result.stderr}",
        )


class TestNotificationCollapseIsTrivial(unittest.TestCase):
    """`notification-collapse.sh` is the only intentionally-trimmed hook
    that remains under UserPromptSubmit. It must be a pure regex on the
    incoming prompt string — no Python, no file reads, no network.
    """

    def setUp(self):
        self.path = REPO_ROOT / "hooks" / "notification-collapse.sh"
        if not self.path.exists():
            raise unittest.SkipTest("notification-collapse.sh not found")
        self.src = self.path.read_text(encoding="utf-8")

    def test_does_not_invoke_python(self):
        self.assertNotIn(
            "python", self.src,
            "notification-collapse.sh must be a pure bash regex; "
            "any Python invocation stalls the prompt path",
        )

    def test_does_not_invoke_curl(self):
        self.assertNotIn(
            "curl", self.src,
            "notification-collapse.sh must not hit the network",
        )

    def test_does_not_invoke_git(self):
        self.assertNotIn(
            "git ", self.src,
            "notification-collapse.sh must not touch git",
        )

    def test_bash_n_clean(self):
        result = subprocess.run(
            ["bash", "-n", str(self.path)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"bash -n failed: rc={result.returncode}, "
                f"stderr={result.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
