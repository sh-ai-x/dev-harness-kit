#!/usr/bin/env python3
"""test_review_yml_isolation.py — regression for the review.yml PR-isolation hook.

Verifies the bash-level behavior of `hooks/review-yml-isolation.sh`:

  - Denies `git commit` when `git diff --cached --name-only` contains a
    file named `review.yml` alongside any other path (PreToolUse deny
    JSON on stderr, exit 2).
  - Allows `git commit` when review.yml is staged alone.
  - Allows `git commit` when review.yml is NOT in the staged set.
  - Allows non-`git-commit` bash commands (the matcher filters by verb).
  - Allows empty stdin (probe calls).
  - Fails closed (deny) when `jq` is missing.

Why a dedicated test file: the existing `test_worktree_guard.py` covers
the worktree-rule PreToolUse guards, but the review.yml-isolation rule
operates on the staged file set (a git operation, not a filesystem
write), so it gets its own setup/teardown that synthesizes a real git
repo with `git init` + `git add` to drive `git diff --cached`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOKS = REPO_ROOT / "hooks"


def _run_hook(payload: dict, cwd: Path | None = None,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke review-yml-isolation.sh with a JSON payload on stdin."""
    p = HOOKS / "review-yml-isolation.sh"
    if not p.exists():
        raise FileNotFoundError(f"hook missing: {p}")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(p)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _bash_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _make_repo_with_staged(staged_paths: list[str]) -> tempfile.TemporaryDirectory:
    """Build a temp git repo with HEAD = an initial commit, then `git add`
    each path in `staged_paths` so `git diff --cached --name-only`
    returns them in order. Returns a TemporaryDirectory whose .name is
    the repo root."""
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True)
    # Seed an initial commit so HEAD exists and `git diff --cached` has
    # something to compare against. Content of the seed files does not
    # matter; we stage the same names later so the diff is empty for
    # the seed and full for the staged set.
    for p in staged_paths:
        abs_p = root / p
        abs_p.parent.mkdir(parents=True, exist_ok=True)
        if not abs_p.exists():
            abs_p.write_text("seed\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"],
                   check=True, capture_output=True)
    # Now overwrite each staged path with new content and re-stage so
    # `git diff --cached --name-only` lists them.
    for p in staged_paths:
        abs_p = root / p
        abs_p.write_text("new\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    return td


class TestReviewYmlIsolationDenies(unittest.TestCase):
    """review.yml staged alongside any other file → DENY (exit 2)."""

    def setUp(self):
        if not (HOOKS / "review-yml-isolation.sh").exists():
            self.skipTest("review-yml-isolation.sh not found")

    def test_denies_when_review_yml_staged_with_other_file(self):
        td = _make_repo_with_staged([".github/workflows/review.yml", "lib/foo.py"])
        try:
            r = _run_hook(_bash_payload("git commit -m mix"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 2,
                             f"expected deny rc=2, got rc={r.returncode}, stderr={r.stderr}")
            combined = r.stdout + r.stderr
            self.assertIn("REVIEW-YML ISOLATION", combined)
            self.assertIn("permissionDecision", combined)
            self.assertIn('"deny"', combined)
            # The reason must mention the offending other file.
            self.assertIn("foo.py", combined)
        finally:
            td.cleanup()

    def test_denies_when_review_yml_staged_with_template_mirror(self):
        """The template mirror `templates/ci/.github/workflows/review.yml`
        shares the basename — must still trigger when staged together
        with the installed copy. Both copies are 'review.yml' by name,
        so the rule treats them as a single logical workflow."""
        td = _make_repo_with_staged([
            ".github/workflows/review.yml",
            "templates/ci/.github/workflows/review.yml",
        ])
        try:
            r = _run_hook(_bash_payload("git commit -m dual"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 2,
                             f"expected deny rc=2, got rc={r.returncode}, stderr={r.stderr}")
            self.assertIn("REVIEW-YML ISOLATION", r.stdout + r.stderr)
        finally:
            td.cleanup()

    def test_denies_when_review_yml_staged_with_other_workflow(self):
        td = _make_repo_with_staged([".github/workflows/review.yml", ".github/workflows/ci.yml"])
        try:
            r = _run_hook(_bash_payload("git commit -m both"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 2, f"stderr={r.stderr}")
            self.assertIn("REVIEW-YML ISOLATION", r.stdout + r.stderr)
            self.assertIn("ci.yml", r.stdout + r.stderr)
        finally:
            td.cleanup()

    def test_deny_output_is_valid_pretooluse_json(self):
        """Minor 4: deny JSON shape must match the PreToolUse schema that
        Claude Code parses (hookSpecificOutput.permissionDecision)."""
        td = _make_repo_with_staged([".github/workflows/review.yml", "x.py"])
        try:
            r = _run_hook(_bash_payload("git commit -m x"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 2)
            deny_lines = [ln for ln in (r.stdout + r.stderr).splitlines()
                          if ln.strip().startswith("{")]
            self.assertTrue(deny_lines, f"no JSON line in output: stdout={r.stdout!r} stderr={r.stderr!r}")
            for line in deny_lines:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as e:
                    self.fail(f"deny output is not valid JSON: {line!r} ({e})")
                self.assertIn("hookSpecificOutput", doc)
                hso = doc["hookSpecificOutput"]
                self.assertEqual(hso.get("hookEventName"), "PreToolUse")
                self.assertEqual(hso.get("permissionDecision"), "deny")
                self.assertIn("permissionDecisionReason", hso)
                self.assertTrue(len(hso["permissionDecisionReason"]) > 0)
        finally:
            td.cleanup()


class TestReviewYmlIsolationAllows(unittest.TestCase):
    """review.yml staged alone OR not at all → ALLOW (exit 0)."""

    def setUp(self):
        if not (HOOKS / "review-yml-isolation.sh").exists():
            self.skipTest("review-yml-isolation.sh not found")

    def test_allows_when_review_yml_staged_alone(self):
        td = _make_repo_with_staged([".github/workflows/review.yml"])
        try:
            r = _run_hook(_bash_payload("git commit -m review only"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 0,
                             f"expected allow rc=0, got rc={r.returncode}, stderr={r.stderr}")
        finally:
            td.cleanup()

    def test_allows_when_review_yml_not_staged(self):
        """No review.yml in the staged set → rule does not apply."""
        td = _make_repo_with_staged(["lib/foo.py", "tests/test_foo.py"])
        try:
            r = _run_hook(_bash_payload("git commit -m regular fix"), cwd=Path(td.name))
            self.assertEqual(r.returncode, 0,
                             f"expected allow rc=0, got rc={r.returncode}, stderr={r.stderr}")
        finally:
            td.cleanup()

    def test_allows_non_commit_bash_commands(self):
        """The matcher filters by `git commit` verb — push, log, status, etc. exit 0."""
        td = _make_repo_with_staged([".github/workflows/review.yml"])
        try:
            for cmd in (
                "git push origin fix/review",
                "git status",
                "git log --oneline -5",
                "ls -la",
                "git diff --cached",
            ):
                r = _run_hook(_bash_payload(cmd), cwd=Path(td.name))
                self.assertEqual(r.returncode, 0,
                                 f"cmd={cmd!r} got rc={r.returncode}, stderr={r.stderr}")
        finally:
            td.cleanup()

    def test_allows_on_empty_stdin(self):
        """Empty stdin (probe call) → no-op, exit 0."""
        p = HOOKS / "review-yml-isolation.sh"
        r = subprocess.run(["bash", str(p)],
                           input="", capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_outside_any_git_repo(self):
        """Hook fires from a non-git cwd → `git diff --cached` is empty → exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            r = _run_hook(_bash_payload("git commit -m x"), cwd=Path(tmp))
            self.assertEqual(r.returncode, 0,
                             f"expected allow rc=0, got rc={r.returncode}, stderr={r.stderr}")


class TestReviewYmlIsolationJqMissing(unittest.TestCase):
    """review-yml-isolation.sh must FAIL CLOSED when jq is missing.

    Mirrors the contract documented at hooks/lib/payload-parse.sh:18
    (fail closed = deny rather than silently skip). Otherwise a stripped
    Docker / fresh macOS host silently disables the rule.
    """

    def setUp(self):
        if not (HOOKS / "review-yml-isolation.sh").exists():
            self.skipTest("review-yml-isolation.sh not found")
        import shutil as _sh
        self._bash = _sh.which("bash")
        self._jq = _sh.which("jq")
        if not self._bash:
            self.skipTest("bash not on PATH")
        if not self._jq:
            self.skipTest("jq not on host — cannot simulate missing-jq")

    def test_denies_when_jq_missing(self):
        # Build a minimal PATH that keeps bash + cat + printf + the
        # `command` builtin but removes jq. Some hosts have jq in
        # /usr/bin which dirname strips; we keep everything else.
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(self._jq))
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps(_bash_payload("git commit -m x"))
        r = subprocess.run(
            [self._bash, str(HOOKS / "review-yml-isolation.sh")],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2,
                         f"expected fail-closed deny rc=2, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("permissionDecision", r.stderr)


class TestReviewYmlIsolationHooksJsonWiring(unittest.TestCase):
    """`hooks/hooks.json` must wire review-yml-isolation.sh under PreToolUse:Bash.

    Regression: if a maintainer rewires the matcher (e.g. moves the hook
    to a different event), this test fails loud. The rule MUST fire on
    Bash events because `git commit` is a Bash tool invocation.
    """

    def test_wired_under_pretooluse_bash(self):
        p = HOOKS / "hooks.json"
        if not p.exists():
            self.skipTest("hooks.json missing")
        with p.open() as f:
            cfg = json.load(f)
        pretooluse = cfg.get("hooks", {}).get("PreToolUse", [])
        found = False
        for entry in pretooluse:
            if entry.get("matcher") != "Bash":
                continue
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "review-yml-isolation.sh" in cmd:
                    found = True
                    break
        self.assertTrue(found,
                        "review-yml-isolation.sh is not wired under PreToolUse/Bash in hooks.json")


if __name__ == "__main__":
    unittest.main()
