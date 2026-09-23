#!/usr/bin/env python3
"""test_worktree_auto_cut_hook.py — Regression tests pinning the worktree-cut
SSOT in hooks/worktree-auto-cut.sh (issue #322).

Background: After PR #313, ``lib.git_worktree.cut_worktree`` is the canonical
``git worktree add`` entry point used by ``lib/execute.py`` and
``lib/acp_dispatch.py``. But ``hooks/worktree-auto-cut.sh`` still inlined
its own ``git worktree add -b ...; git worktree remove --force ...; git
branch -D ...`` block (lines 233-239 pre-refactor). Two callers meant two
contracts that could drift silently.

This test pins the post-refactor shape:

  1. The hook source MUST NOT contain the literal ``git worktree add -b``
     invocation shape — the inline implementation is replaced.
  2. The hook MUST shell into Python via ``python3 -c`` (or a heredoc fed
     to ``python3 -``) and reference both ``lib.git_worktree`` and
     ``cut_worktree`` so the cut is routed through the canonical helper.
  3. The safe-mode contract is preserved: ``reset_branch`` is NOT set to
     True (the historical ``-b`` flag is the default for
     ``cut_worktree``).
  4. End-to-end: with ``python3`` replaced by a recording shim, the hook
     actually invokes ``cut_worktree`` when given a task prompt in a
     clean main checkout. This proves the source-level assertions are
     connected to runtime behavior, not decorative.
  5. The cleanup semantics survive the refactor: when ``cut_worktree``
     returns non-zero (shim exits 1), the hook still emits the
     "git worktree add failed for branch ..." fallback envelope (the
     old ``git worktree remove --force`` + ``git branch -D`` cleanup was
     deliberate — keeping the same envelope keeps downstream callers
     working).
  6. The hook keeps its ``set -uo pipefail`` + preamble + jq-fail-closed
     shape. The cut block is the only thing replaced.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "worktree-auto-cut.sh"


def _init_clean_main() -> "tempfile.TemporaryDirectory":
    """Throwaway main repo with one commit on ``main`` and no remote.

    Mirrors the fixture used by ``tests/test_worktree_auto_cut.py`` so
    the auto-cut hook falls back to local ``main`` when ``origin`` is
    not configured. The Python-helper path is fully exercised against
    this fixture via a recording ``python3`` shim (see
    ``TestHookInvokesCutWorktreeHelper``).

    Per the issue #845 brief the hook requires an ACCEPTED intent
    record before invoking ``cut_worktree``. Tests that exercise
    the cut path call ``_write_accepted_intent(root)`` after
    initialising the repo.
    """
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    return td


def _write_accepted_intent(root: Path) -> None:
    """Materialize an ACCEPTED intent.md at ``.dev-kit/round-0/intent.md``.

    The hook's cut path is gated on this record existing and being
    valid (decision_mode=accepted + acceptance_criteria)."""
    intent_dir = root / ".dev-kit" / "round-0"
    intent_dir.mkdir(parents=True, exist_ok=True)
    (intent_dir / "intent.md").write_text(
        "# Intent: add file foo\n"
        "\n"
        "request_id: req-deadbeefcafef00d\n"
        "client: claude-code\n"
        "decision_mode: accepted\n"
        "goal: add file foo\n"
        "\n"
        "## Acceptance criteria\n"
        "- foo file added\n",
        encoding="utf-8",
    )


def _run_hook_with_env(
    payload: dict,
    *,
    cwd: Path,
    extra_env: dict,
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.update(extra_env)
    full_env.setdefault("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT))
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(cwd),
        env=full_env,
    )


def _payload(prompt: str, cwd: str) -> dict:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "test",
        "prompt": prompt,
        "cwd": cwd,
    }


def _install_shim(
    *,
    bin_dir: Path,
    python_log: Path,
    rc: int,
    fail_on_cut_worktree: bool = False,
) -> None:
    """Module-level shim installer shared by the success + failure
    test classes. The shim records every invocation's args + script
    body to ``python_calls.log`` AND delegates to the real
    ``python3`` so the script actually runs.

    ``fail_on_cut_worktree=True`` makes the shim exit ``rc`` ONLY
    when the script body contains ``cut_worktree`` — the helper
    that performs the worktree cut. For the success path the shim
    forwards everything to the real python and exits with the
    real python's status.
    """
    shim = bin_dir / "python3"
    fail_marker = "1" if fail_on_cut_worktree else "0"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"echo '----INVOCATION----' >> '{python_log}'\n"
        "echo \"args: $*\" >> \"" + str(python_log) + "\"\n"
        "REAL_PY=\"${REAL_PY:-/usr/bin/python3}\"\n"
        "[ -x \"$REAL_PY\" ] || REAL_PY=\"$(command -v /usr/bin/env)\"\n"
        "FAIL_ON_CUT=" + fail_marker + "\n"
        "RC=" + str(rc) + "\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  echo \"script: $2\" >> \"" + str(python_log) + "\"\n"
        "  if [ \"$FAIL_ON_CUT\" = \"1\" ] && printf '%s' \"$2\" | grep -q 'cut_worktree'; then\n"
        "    exit $RC\n"
        "  fi\n"
        "  \"$REAL_PY\" \"$@\" 2>/dev/null\n"
        "  exit $?\n"
        "elif [ \"$1\" = \"-\" ]; then\n"
        "  body=$(cat)\n"
        "  printf '%s\\n' \"$body\" >> \"" + str(python_log) + "\"\n"
        "  if [ \"$FAIL_ON_CUT\" = \"1\" ] && printf '%s' \"$body\" | grep -q 'cut_worktree'; then\n"
        "    exit $RC\n"
        "  fi\n"
        "  printf '%s' \"$body\" | \"$REAL_PY\" \"$@\" 2>/dev/null\n"
        "  exit $?\n"
        "fi\n"
        "exit $RC\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


class TestHookSourceShape(unittest.TestCase):
    """Source-level assertions on hooks/worktree-auto-cut.sh.

    Each test below is a single grep gate from the issue acceptance
    criteria. Together they prove the inline ``git worktree add -b`` is
    gone and the canonical helper is wired in.
    """

    @classmethod
    def setUpClass(cls):
        if not HOOK.exists():
            raise unittest.SkipTest(f"worktree-auto-cut.sh not found: {HOOK}")
        cls.src = HOOK.read_text(encoding="utf-8")

    def test_inline_git_worktree_add_b_is_gone(self):
        """The literal ``git worktree add -b`` shape must NOT appear in the hook.

        This is the single grep gate from the issue acceptance: a
        contract change must NOT have to touch both Python and shell.
        """
        self.assertNotRegex(
            self.src,
            r"git worktree add -b",
            "Hook still inlines `git worktree add -b ...`; must route through "
            "lib.git_worktree.cut_worktree() per issue #322",
        )

    def test_hook_invokes_python_helper(self):
        """Hook must shell into Python and reference cut_worktree."""
        # python3 -c OR python3 - (heredoc) — both are valid.
        self.assertTrue(
            ("python3 -c" in self.src) or re.search(r"python3\s+-", self.src),
            "Hook must invoke the canonical helper via `python3 -c` or a "
            "heredoc fed to `python3 -`",
        )
        self.assertIn(
            "cut_worktree", self.src,
            "Hook must call `cut_worktree` via the Python helper",
        )
        # The import path for the helper module must be present.
        self.assertTrue(
            ("lib.git_worktree" in self.src) or ("from git_worktree" in self.src),
            "Hook must import the canonical helper module "
            "(`from lib.git_worktree import cut_worktree` or "
            "`from git_worktree import cut_worktree`)",
        )

    def test_safe_mode_is_preserved(self):
        """The hook historically used ``-b`` (safe). After the refactor,
        ``cut_worktree`` is called in safe mode — i.e. ``reset_branch``
        is NOT pinned to True.

        The default ``reset_branch=False`` corresponds to the
        historical ``-b`` flag (fail closed on existing branch).
        ``reset_branch=True`` corresponds to ``-B`` (per-step reset
        semantics used by ``lib/execute.py``), which is NOT what the
        auto-cut hook wants.
        """
        # If the hook explicitly mentions reset_branch, it must not be True.
        if "reset_branch" in self.src:
            self.assertNotRegex(
                self.src,
                r"reset_branch\s*=\s*True",
                "Hook must not pass reset_branch=True; the safe `-b` "
                "behavior is the historical contract for auto-cut.",
            )

    def test_preamble_invariants_intact(self):
        """Only the worktree-cut block changes. The preamble + jq-fail-closed
        guard + ``set -uo pipefail`` must remain.
        """
        # ``set -uo pipefail`` lives in the shared preamble (sourced via
        # ``lib/hook-preamble.sh``). Confirm the hook still sources it.
        self.assertIn(
            "lib/hook-preamble.sh", self.src,
            "Hook must still source lib/hook-preamble.sh (preamble invariants).",
        )
        # The standalone ``set -uo pipefail`` is in the preamble, but the
        # hook body itself should not have stripped it.
        # (Either inline or via the preamble — both are acceptable.)
        self.assertTrue(
            ("set -uo pipefail" in self.src)
            or ("hook-preamble.sh" in self.src),
            "Hook must keep `set -uo pipefail` either inline or via the preamble.",
        )
        # The jq-missing fail-open warning path must remain.
        self.assertIn(
            "jq >/dev/null 2>&1", self.src,
            "Hook must keep its `command -v jq` check (fail-open warning path).",
        )


class TestHookInvokesCutWorktreeHelper(unittest.TestCase):
    """End-to-end: with ``python3`` replaced by a recording shim, the hook
    actually invokes ``cut_worktree`` when given a task prompt in a clean
    main checkout. Connects the source-level assertions to runtime
    behavior.
    """

    @classmethod
    def setUpClass(cls):
        if not HOOK.exists():
            raise unittest.SkipTest(f"worktree-auto-cut.sh not found: {HOOK}")
        if not shutil.which("jq"):
            raise unittest.SkipTest("jq missing; hook fails open before reaching cut step")

    def setUp(self):
        self.tmp = _init_clean_main()
        self.bin_dir = Path(tempfile.mkdtemp())
        self.python_log = self.bin_dir / "python_calls.log"
        # Recording python3: writes each invocation's args + script body
        # to the log, then delegates to the real python3 so the
        # classifier + intent-check + cut chain actually executes.
        # Tests that need a failing helper can swap in a different
        # shim (rc=1).
        self._install_python_shim(rc=0)

    def tearDown(self):
        # Clean up any worktree the hook may have created.
        root = Path(self.tmp.name)
        wt_root = root / ".worktrees"
        if wt_root.exists():
            for child in wt_root.iterdir():
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force",
                     str(child)],
                    capture_output=True,
                )
        for ref in subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--format=%(refname)",
             "refs/heads/fix/"],
            capture_output=True, text=True,
        ).stdout.splitlines():
            subprocess.run(
                ["git", "-C", str(root), "branch", "-D",
                 ref.removeprefix("refs/heads/")],
                capture_output=True,
            )
        shutil.rmtree(self.bin_dir, ignore_errors=True)
        self.tmp.cleanup()

    def _install_python_shim(self, *, rc: int, fail_on_cut_worktree: bool = False) -> None:
        """Test-method entry point — delegates to the module-level
        ``_install_shim``. Keeps the per-class API stable while the
        underlying installer is shared."""
        _install_shim(
            bin_dir=self.bin_dir,
            python_log=self.python_log,
            rc=rc,
            fail_on_cut_worktree=fail_on_cut_worktree,
        )

    def _path_with_shim(self) -> str:
        return f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}"

    def test_hook_invokes_cut_worktree_for_task_prompt(self):
        """In clean main + ACCEPTED intent.md + task prompt: the
        hook MUST shell out to a Python invocation that references
        ``cut_worktree``.

        Per the issue #845 brief, the cut is gated on the accepted
        intent record. This is the runtime counterpart to the
        source-level grep assertions in ``TestHookSourceShape``.
        """
        _write_accepted_intent(Path(self.tmp.name))
        r = _run_hook_with_env(
            _payload(prompt="add file foo to the project", cwd=self.tmp.name),
            cwd=Path(self.tmp.name),
            extra_env={"PATH": self._path_with_shim()},
        )
        self.assertEqual(
            r.returncode, 0,
            f"rc={r.returncode}, stderr={r.stderr}, stdout={r.stdout!r}",
        )
        log = self.python_log.read_text(encoding="utf-8")
        self.assertIn(
            "cut_worktree", log,
            f"Hook did not invoke cut_worktree via python3; log:\n{log}\n"
            f"stderr={r.stderr}",
        )
        # The invocation must reference the canonical helper module.
        self.assertTrue(
            ("lib.git_worktree" in log) or ("from git_worktree" in log),
            f"Hook python invocation did not import lib.git_worktree; "
            f"log:\n{log}",
        )


class TestHookCutFailureCleanup(unittest.TestCase):
    """When ``cut_worktree`` fails, the hook must still emit the
    fallback envelope naming the failing branch. The pre-refactor
    cleanup shape was:
        if ! git worktree add -b ...; then
            git worktree remove --force "$WT_PATH" 2>/dev/null || true
            git branch -D "$BRANCH" 2>/dev/null || true
            fallback_context "git worktree add failed for branch $BRANCH"
            exit 0
        fi
    The refactor routes the ``git worktree add`` through
    ``cut_worktree``; the cleanup shape is delegated to
    ``cut_worktree`` itself (overwrite_worktree + pre-existing-branch
    survival in safe mode). The fallback envelope MUST still fire when
    ``cut_worktree`` raises.

    Per the issue #845 brief: failed cut leaves no ``ready``
    envelope; the fallback names the canonical retry path.

    Under the intent-first contract the failure case is: classify +
    intent-check succeed, but the cut itself fails. The shim is
    installed with ``fail_on_cut_worktree=True`` so only the cut
    python invocation exits non-zero.
    """

    @classmethod
    def setUpClass(cls):
        if not HOOK.exists():
            raise unittest.SkipTest(f"worktree-auto-cut.sh not found: {HOOK}")
        if not shutil.which("jq"):
            raise unittest.SkipTest("jq missing; hook fails open before reaching cut step")

    def setUp(self):
        self.tmp = _init_clean_main()
        self.bin_dir = Path(tempfile.mkdtemp())
        self.python_log = self.bin_dir / "python_calls.log"
        self._install_python_shim(rc=1, fail_on_cut_worktree=True)

    def tearDown(self):
        shutil.rmtree(self.bin_dir, ignore_errors=True)
        self.tmp.cleanup()

    def _install_python_shim(self, *, rc: int, fail_on_cut_worktree: bool = False) -> None:
        # Delegates to the module-level helper so the failure-test
        # shim carries the cut-only failure flag (the success-path
        # TestHookInvokesCutWorktreeHelper installs the same shim
        # shape with the flag off).
        _install_shim(
            bin_dir=self.bin_dir,
            python_log=self.python_log,
            rc=rc,
            fail_on_cut_worktree=fail_on_cut_worktree,
        )

    def _path_with_shim(self) -> str:
        return f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}"

    def test_fallback_envelope_fires_when_helper_fails(self):
        """When the Python helper exits non-zero, the hook must emit
        the same ``worktree cut failed for branch <BRANCH>`` envelope
        the pre-refactor inline implementation emitted.

        Per the issue #845 brief: failed cut leaves no ``ready``
        envelope; the fallback names the canonical retry path.
        """
        _write_accepted_intent(Path(self.tmp.name))
        r = _run_hook_with_env(
            _payload(prompt="add file foo to the project", cwd=self.tmp.name),
            cwd=Path(self.tmp.name),
            extra_env={"PATH": self._path_with_shim()},
        )
        self.assertEqual(r.returncode, 0, f"rc={r.returncode}, stderr={r.stderr}")
        # The hook MUST have invoked the Python helper.
        log = self.python_log.read_text(encoding="utf-8")
        self.assertIn(
            "cut_worktree", log,
            f"Hook did not invoke cut_worktree via python3; log:\n{log}",
        )
        # The hook MUST emit a fallback envelope naming the failing branch.
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"hook output not JSON: {r.stdout!r} ({exc})")
        ctx = doc.get("hookSpecificOutput", {}).get("additionalContext", "")
        # The intent-first shape replaces ``git worktree add failed``
        # with ``worktree cut failed`` (no worktree was created —
        # the envelope is a retry nudge, not a ready signal). Both
        # phrasings are accepted for transitional compatibility.
        self.assertTrue(
            ("git worktree add failed" in ctx)
            or ("worktree cut failed" in ctx)
            or ("cut_worktree failed" in ctx),
            f"Expected fallback envelope on helper failure; got: {ctx!r}",
        )
        # The branch must be named in the envelope so the user knows
        # what to clean up.
        branch_match = re.search(r"branch[: ]+(\S+)", ctx)
        self.assertIsNotNone(
            branch_match,
            f"Fallback envelope must name the failing branch; got: {ctx!r}",
        )


class TestHookBashSyntax(unittest.TestCase):
    """``bash -n`` must confirm the refactored hook is syntactically valid."""

    def test_bash_n_clean(self):
        if not HOOK.exists():
            self.skipTest(f"worktree-auto-cut.sh not found: {HOOK}")
        r = subprocess.run(
            ["bash", "-n", str(HOOK)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            r.returncode, 0,
            f"bash -n failed: rc={r.returncode}, stderr={r.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
