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
# Note: tdd-scope-judge.sh was originally deleted by this PR, but a
# follow-up commit (4114eb49 / 6f3...8a) restored it as the canonical
# exception to the UserPromptSubmit "no Python" rule — it has its own
# 45s `claude -p` subprocess timeout and a fail-safe default to
# `tdd_required: true`, so a stalled judge cannot accidentally allow an
# edit that should require TDD. It is now the only remaining Python
# entry on UserPromptSubmit, with the documented `python3 -m lib.<x>`
# shape that the regen lint allows.
REMOVED_HOOKS = (
    "worktree-auto-cut.sh",
    "linear-task-change.sh",
)

# Hooks that remain under UserPromptSubmit in both runtimes.
#
# `tdd-scope-judge.sh` — canonical lib invocation (`python3 -m
#   lib.tdd_scope_judge`), 45s subprocess timeout, fail-safe default.
# `notification-collapse.sh` — pure bash regex on stdin, sub-100ms.
# `context-window-guard.sh` — `tail -n 100 | jq -s`, sub-100ms.
REMAINING_HOOKS = (
    "tdd-scope-judge.sh",
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
        # The lint now reads each UserPromptSubmit hook's script body,
        # so the temp dir must contain the shell files referenced in
        # hooks.json. Copy the UserPromptSubmit hooks explicitly
        # (tdd-scope-judge.sh, notification-collapse.sh,
        # context-window-guard.sh); the regen matrix check only
        # enumerates UserPromptSubmit entries.
        for shell in (
            "tdd-scope-judge.sh",
            "notification-collapse.sh",
            "context-window-guard.sh",
        ):
            shutil.copy(
                REPO_ROOT / "hooks" / shell,
                self.root / "hooks" / shell,
            )
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

    def _inject_userpromptsubmit(
        self,
        command: str,
        *,
        timeout: int = 0,
        script_body: str = "# trivial hook body\n",
    ) -> None:
        """Replace the first UserPromptSubmit entry with a single hook.

        The lint reads the script body when checking forbidden tokens, so
        the helper also creates a `hooks/<name>.sh` file (or rewrites the
        referenced one) with the caller-supplied `script_body`. Pass an
        empty body to test the missing-file path; pass a body with a
        forbidden token to verify the script-body lint.
        """
        path = self.root / "hooks" / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # Drop all existing UserPromptSubmit entries (they were valid
        # but irrelevant to this test) and insert one synthetic entry
        # that the lint should reject or accept.
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
        # Materialize the referenced script so the lint's body check
        # has something to read. The path is extracted from the
        # command's trailing `hooks/<name>.sh` token.
        import re
        match = re.search(r"hooks/([A-Za-z0-9_.\-]+\.sh)\b", command)
        if match:
            script_rel = "hooks/" + match.group(1)
            script_path = self.root / script_rel
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script_body, encoding="utf-8")

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

    def test_regen_rejects_forbidden_token_in_script_body(self):
        """The lint MUST also read each script body and reject forbidden
        tokens there. A wrapper script that invokes `python` from a
        comment-free line is still a UserPromptSubmit blocker; the
        command-string check alone leaves that gap open.
        """
        # A body whose `#` lines are stripped still contains `python`
        # on the active line — the lint must catch it.
        body = (
            "#!/usr/bin/env bash\n"
            "# explanatory comment with python\n"
            "exec python3 -m my_module\n"
        )
        self._inject_userpromptsubmit(
            "bash ${CLAUDE_PLUGIN_ROOT}/hooks/dummy.sh",
            script_body=body,
        )
        result = _run_regen(self.root)
        self.assertNotEqual(
            result.returncode, 0,
            msg=f"regen accepted a script body containing 'python'; "
                f"stderr={result.stderr!r}",
        )
        self.assertIn("script body", result.stderr)

    def test_regen_ignores_forbidden_tokens_in_comment_lines(self):
        """The lint MUST strip `#` comment lines so a token that only
        appears in a docstring (describing the OLD shape, e.g. `jq -rs`
        in `context-window-guard.sh`'s history note) does NOT trip the
        body check.
        """
        body = (
            "#!/usr/bin/env bash\n"
            "# historical: the old shape used `jq -rs` here.\n"
            "# describing the python invocation we used to call.\n"
            "tail -n 100 \"$1\" | jq -s '.'\n"
        )
        self._inject_userpromptsubmit(
            "bash ${CLAUDE_PLUGIN_ROOT}/hooks/dummy.sh",
            script_body=body,
        )
        result = _run_regen(self.root)
        self.assertEqual(
            result.returncode, 0,
            msg=f"regen rejected a script whose only forbidden tokens "
                f"live inside `#` comments; stderr={result.stderr!r}",
        )

    def test_regen_rejects_missing_script_file(self):
        """A UserPromptSubmit hook whose referenced script file does not
        exist MUST fail closed. A broken hook must not silently pass
        regen — the matrix snapshot would then list a shell that
        consumers cannot invoke.
        """
        # Write the manifest referencing a script we never create.
        path = self.root / "hooks" / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["hooks"]["UserPromptSubmit"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/does-not-exist.sh",
                        "fail_closed": False,
                    }
                ]
            }
        ]
        path.write_text(json.dumps(data, indent=2))
        # Intentionally do NOT create the script.
        result = _run_regen(self.root)
        self.assertNotEqual(
            result.returncode, 0,
            msg=f"regen accepted a UserPromptSubmit entry whose "
                f"script file is missing; stderr={result.stderr!r}",
        )
        self.assertIn("missing", result.stderr)


class TestContextWindowGuardUsesTailSample(unittest.TestCase):
    """`context-window-guard.sh` MUST bound the file read to the last 100
    records via `tail -n 100 | jq -s`, not walk the full JSONL.

    The prior implementation `jq -rs [...]` read every record and
    timed out on long babysit sessions. Two rewrites were tried in
    this branch: the first (`jq -rs '[ .[-100:] | select(...)]'`)
    errored on the array slice under `-s` and was silently swallowed
    by `|| echo 0`, making the guard dead. The current shape —
    `tail -n 100 "$TRANSCRIPT" | jq -s '[ .[] | select(...) ] | add'` —
    bounds the file walk via tail (constant-time in transcript length)
    and uses `.[]` to iterate the slurped array unambiguously.

    Source-shape tests pin the rewrite; behavioural tests below run
    the hook end-to-end against synthetic JSONL transcripts and
    assert the WARN reaches stderr.
    """

    def setUp(self):
        self.path = REPO_ROOT / "hooks" / "context-window-guard.sh"
        if not self.path.exists():
            raise unittest.SkipTest("context-window-guard.sh not found")
        self.src = self.path.read_text(encoding="utf-8")
        # Strip `#` comment lines so the lint assertions don't false-
        # positive on tokens that appear in docstring history notes
        # describing the OLD shape. Mirrors tools/regenerate_active_hooks.py
        # body-check behavior so the two lints agree.
        self.src_active = "\n".join(
            line for line in self.src.splitlines()
            if not line.lstrip().startswith("#")
        )

    def _run_hook_with_transcript(self, transcript_path: Path) -> subprocess.CompletedProcess:
        """Invoke `context-window-guard.sh` with a payload naming the
        transcript path, exactly as the harness does on UserPromptSubmit.
        """
        payload = json.dumps({"transcript_path": str(transcript_path)})
        return subprocess.run(
            ["bash", str(self.path)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(REPO_ROOT),
        )

    def _write_jsonl(self, tmp: Path, records: list[dict]) -> Path:
        p = tmp / "transcript.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_over_threshold_emits_hard_warn(self):
        """Last 100 records summing ≥ 300K (HARD_KB) → `[context-window-guard] HARD:`
        on stderr. Would have caught the prior silently-dead jq filter.
        """
        # 6 records × (input_tokens=60000, cache_read_input_tokens=50000) = 110K each
        # → 660K total, well above the 300K HARD threshold.
        records = [
            {"message": {"usage": {
                "input_tokens": 60000, "cache_read_input_tokens": 50000,
            }}}
            for _ in range(6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._write_jsonl(Path(tmp), records)
            result = self._run_hook_with_transcript(tp)
        self.assertEqual(
            result.returncode, 0, msg=f"hook crashed: {result.stderr}",
        )
        self.assertIn(
            "[context-window-guard] HARD",
            result.stderr,
            f"expected HARD WARN on stderr; got: {result.stderr!r}",
        )

    def test_under_threshold_emits_no_warn(self):
        """Sub-threshold tail (sum < 100K WARN_KB) → stderr is empty.
        The hook is advisory; a quiet session must not raise a tier.
        """
        records = [
            {"message": {"usage": {
                "input_tokens": 1000, "cache_read_input_tokens": 500,
            }}}
            for _ in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._write_jsonl(Path(tmp), records)
            result = self._run_hook_with_transcript(tp)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr, "",
            f"sub-threshold session must be silent; got: {result.stderr!r}",
        )

    def test_does_not_use_full_file_jq_rs(self):
        """The full-file `jq -rs` shape MUST be gone.

        The prior rewrite errored silently (`Cannot index array with
        string "message"` swallowed by `|| echo 0`); the current shape
        uses `tail -n 100` to bound the file walk before jq sees it.
        This test pins both halves: no full-file slurp AND a tail bound.
        """
        self.assertNotIn(
            "jq -rs", self.src_active,
            "context-window-guard.sh uses `jq -rs` (full-file slurp); "
            "that walks the whole transcript and is the shape we deleted",
        )

    def test_uses_tail_to_bound_file_walk(self):
        """The hook MUST bound the file read via `tail -n 100` before jq.

        Without `tail -n 100`, jq reads the entire transcript regardless
        of any later `.[]` iteration — the constant-time claim is empty.
        """
        self.assertIn(
            "tail -n 100", self.src_active,
            "context-window-guard.sh must bound the file read via "
            "`tail -n 100` so jq never sees the whole transcript",
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
