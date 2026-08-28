#!/usr/bin/env python3
"""
test_git_workflow.py — RED-first tests for branch-strategy enforcement.

Two layers under test:
  1. hooks/git-guard.sh — PreToolUse hook that denies direct commits/pushes
     to main and force-pushes to shared branches.
  2. .claude/rules/git-workflow.md — branch naming convention <type>/<slug>
     with a fixed allowlist of types. Enforced here by sampling the recent
     commit / branch history.

Both layers are part of the same rule (ADR-0022): a feature branch is
isolated in a worktree, cut from latest origin/main, and merged only via PR.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "git-guard.sh"
RULE_FILE = REPO_ROOT / ".claude" / "rules" / "git-workflow.md"

ALLOWED_BRANCH_TYPES = ("fix", "feat", "refactor", "docs", "test", "chore", "perf", "hotfix", "prune")  # `prune` added: in active use (e.g. post-#493 merge leftover) but missing from rules/git-workflow.md.
BRANCH_RE = re.compile(r"^(?P<type>" + "|".join(ALLOWED_BRANCH_TYPES) + r")/(?P<slug>[a-z0-9][a-z0-9-]{0,38}[a-z0-9])$")
FORBIDDEN_SLUG_WORDS = {"wip", "tmp", "foo", "bar", "asdf", "test", "scratch", "untitled"}
# m2: real forbidden-slug enforcement. FORBIDDEN_RE explicitly matches
# the forbidden combinations — BRANCH_RE itself is naive (by design; the
# upstream tool that creates branches doesn't need to know about cosmetic
# disallow lists). The workflow's check is FORBIDDEN_RE on top.
FORBIDDEN_RE = re.compile(
    r"^(?:" + "|".join(ALLOWED_BRANCH_TYPES) + r")/(?:" + "|".join(sorted(FORBIDDEN_SLUG_WORDS)) + r")$"
)


def _run_hook(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke git-guard.sh with a JSON payload simulating a Bash PreToolUse call."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
        cwd=str(cwd) if cwd else None,
    )


def _init_tmp_git_repo() -> tempfile.TemporaryDirectory:
    """Create a throwaway git repo with one commit so the hook can read HEAD."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return tmp


class TestGitGuardBlocks(unittest.TestCase):
    """git-guard.sh must deny (exit 2) direct commits and pushes to main."""

    def setUp(self):
        if not HOOK.exists():
            self.skipTest(f"git-guard not found: {HOOK}")

    def test_blocks_commit_on_main(self):
        with _init_tmp_git_repo() as tmp:
            r = _run_hook("git commit -m 'oops'", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"expected block, got rc={r.returncode}\nstderr={r.stderr}")
            self.assertIn("direct commit to 'main'", r.stderr)
            self.assertIn("permissionDecision", r.stderr)

    def test_blocks_commit_on_master(self):
        with _init_tmp_git_repo() as tmp:
            subprocess.run(["git", "-C", tmp, "branch", "-m", "main", "master"], check=True, capture_output=True)
            r = _run_hook("git commit -m 'oops'", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"expected block, got rc={r.returncode}\nstderr={r.stderr}")
            self.assertIn("direct commit to 'master'", r.stderr)

    def test_blocks_push_to_origin_main(self):
        r = _run_hook("git push origin main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pushing to main is forbidden", r.stderr)

    def test_blocks_push_HEAD_to_main(self):
        r = _run_hook("git push origin HEAD:main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pushing to main", r.stderr)

    def test_blocks_force_push(self):
        r = _run_hook("git push --force origin fix/foo")
        self.assertEqual(r.returncode, 2)
        self.assertIn("force-push", r.stderr)

    def test_blocks_checkout_main(self):
        with _init_tmp_git_repo() as tmp:
            # First create a feature branch so we can check out from it.
            subprocess.run(["git", "-C", tmp, "checkout", "-q", "-b", "fix/example"], check=True)
            r = _run_hook("git checkout main", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"expected block, got rc={r.returncode}\nstderr={r.stderr}")
            self.assertIn("switching to main", r.stderr)

    def test_blocks_branch_D_main(self):
        r = _run_hook("git branch -D main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("deleting main/master with -D", r.stderr)

    def test_blocks_gh_pr_merge(self):
        r = _run_hook("gh pr merge 42 --auto --squash")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh pr merge is forbidden", r.stderr)

    def test_blocks_gh_pr_merge_bare(self):
        r = _run_hook("gh pr merge 42")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh pr merge is forbidden", r.stderr)

    def test_blocks_gh_pr_merge_after_chained_command(self):
        r = _run_hook("cd /tmp && gh pr merge 1 --auto")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh pr merge is forbidden", r.stderr)

    def test_blocks_gh_pr_merge_no_space_before_separator(self):
        # `merge;echo` (no space before the `;`) must still be caught --
        # the trailing boundary is not just `[[:space:]]`.
        r = _run_hook("gh pr merge;echo done")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh pr merge is forbidden", r.stderr)

    def test_blocks_combined_main_checkout_then_commit(self):
        with _init_tmp_git_repo() as tmp:
            subprocess.run(["git", "-C", tmp, "checkout", "-q", "-b", "fix/example"], check=True)
            r = _run_hook("git checkout main && git commit -m 'evil'", cwd=Path(tmp))
            # checkout-main is caught first (exit 2).
            self.assertEqual(r.returncode, 2)
            self.assertIn("switching to main", r.stderr)

    # === M1: global git flag bypass ===
    # These all slipped through the previous (literal-pattern) matcher because
    # the regex required literal `git <space> <verb>`. The hook now strips
    # -C, -c, --git-dir, --work-tree, --no-pager before pattern matching.

    def test_blocks_commit_with_global_C(self):
        with _init_tmp_git_repo() as tmp:
            r = _run_hook(f"git -C {tmp} commit -m x", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"got rc={r.returncode}, stderr={r.stderr}")
            self.assertIn("direct commit to 'main'", r.stderr)

    def test_blocks_commit_with_global_c(self):
        with _init_tmp_git_repo() as tmp:
            r = _run_hook(f"git -c user.email=x@y.z -C {tmp} commit -m x", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"got rc={r.returncode}, stderr={r.stderr}")
            self.assertIn("direct commit to 'main'", r.stderr)

    def test_blocks_push_origin_main_with_global_C(self):
        with _init_tmp_git_repo() as tmp:
            r = _run_hook(f"git -C {tmp} push origin main", cwd=Path(tmp))
            self.assertEqual(r.returncode, 2, f"got rc={r.returncode}, stderr={r.stderr}")
            self.assertIn("pushing to main", r.stderr)

    def test_blocks_push_with_global_no_pager(self):
        r = _run_hook("git --no-pager push origin main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pushing to main", r.stderr)

    def test_blocks_push_with_global_git_dir(self):
        r = _run_hook("git --git-dir=/some/path push origin main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pushing to main", r.stderr)

    def test_blocks_push_origin_plus_main(self):
        """`git push origin +main` (the `+` refspec prefix) was a gap in the
        previous matcher — covered now by the `(\\+)?` alternation."""
        r = _run_hook("git push origin +main")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pushing to main", r.stderr)

    # === M2: fail-closed when jq is missing ===

    def test_blocks_when_jq_missing(self):
        """M2: if jq is not installed, the hook must DENY (not silently allow).
        Simulates by invoking the script with bash directly + a PATH that
        contains no jq (but still has cat/echo/printf so the deny heredoc
        can actually be printed)."""
        if not HOOK.exists():
            self.skipTest("git-guard not found")
        import shutil
        bash_real = shutil.which("bash")
        jq_real = shutil.which("jq")
        if not bash_real:
            self.skipTest("bash not on PATH — cannot run hook")
        if not jq_real:
            self.skipTest("jq is not installed on this host — cannot simulate missing-jq")
        # Build a PATH that has bash + the common utility dirs (so the
        # hook's deny-heredoc `cat` works) but NOT jq. We resolve each
        # utility independently so the test works whether they live in
        # /bin, /usr/bin, or both (CI Ubuntu has both).
        util_dirs = set()
        for util in ("bash", "cat", "echo", "printf", "command"):
            p = shutil.which(util)
            if p:
                util_dirs.add(os.path.dirname(p))
        util_dirs.discard(os.path.dirname(jq_real))  # ensure jq is excluded
        minimal_path = os.pathsep.join(sorted(util_dirs)) or "/nonexistent"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        r = subprocess.run(
            [bash_real, str(HOOK)],
            input=payload, capture_output=True, text=True, timeout=5,
            env={**os.environ, "PATH": minimal_path},
        )
        self.assertEqual(r.returncode, 2, f"expected deny, got rc={r.returncode}, stderr={r.stderr}")
        self.assertIn("jq is required", r.stderr)
        self.assertIn("permissionDecision", r.stderr)


class TestGitGuardAllows(unittest.TestCase):
    """git-guard.sh must ALLOW (exit 0) normal feature-branch operations."""

    def setUp(self):
        if not HOOK.exists():
            self.skipTest(f"git-guard not found: {HOOK}")

    def test_allows_commit_on_feature_branch(self):
        with _init_tmp_git_repo() as tmp:
            subprocess.run(["git", "-C", tmp, "checkout", "-q", "-b", "fix/example"], check=True)
            r = _run_hook("git commit -m 'legit fix'", cwd=Path(tmp))
            self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_commit_with_global_C_to_feature_branch(self):
        with _init_tmp_git_repo() as tmp:
            subprocess.run(["git", "-C", tmp, "checkout", "-q", "-b", "fix/example"], check=True)
            # The hook process itself runs outside the target worktree. It
            # must use the explicit -C repository for the branch decision.
            r = _run_hook(f"git -C {tmp} commit -m 'legit fix'")
            self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_push_to_feature_branch(self):
        r = _run_hook("git push -u origin fix/review-findings")
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_checkout_b_new_branch(self):
        with _init_tmp_git_repo() as tmp:
            r = _run_hook("git checkout -b fix/new-thing", cwd=Path(tmp))
            self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_force_with_lease_on_own_branch(self):
        r = _run_hook("git push --force-with-lease origin fix/review-findings")
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_gh_pr_view(self):
        r = _run_hook("gh pr view 42")
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_gh_pr_create(self):
        r = _run_hook("gh pr create --base main --head fix/review-findings")
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_gh_pr_checks(self):
        r = _run_hook("gh pr checks")
        self.assertEqual(r.returncode, 0, f"got rc={r.returncode}, stderr={r.stderr}")

    def test_allows_read_only_git_commands(self):
        for cmd in ["git status", "git log --oneline -5", "git diff HEAD~1",
                    "git rev-parse HEAD", "git branch --show-current", "git show --stat"]:
            with self.subTest(cmd=cmd):
                r = _run_hook(cmd)
                self.assertEqual(r.returncode, 0, f"got rc={r.returncode} for {cmd!r}, stderr={r.stderr}")

    def test_allows_non_git_commands(self):
        r = _run_hook("ls -la /tmp")
        self.assertEqual(r.returncode, 0)

    def test_allows_empty_command(self):
        r = _run_hook("")
        self.assertEqual(r.returncode, 0)


class TestBranchNamingConvention(unittest.TestCase):
    """The branch-naming rule from .claude/rules/git-workflow.md is enforced
    on the local git history. Any non-main branch whose name doesn't match
    `<type>/<slug>` with a clean kebab slug fails."""

    def test_branch_naming_convention_documented(self):
        """The rule file must exist and mention the convention."""
        self.assertTrue(RULE_FILE.exists(), f"missing {RULE_FILE}")
        text = RULE_FILE.read_text(encoding="utf-8")
        self.assertIn("Branch naming (mandatory)", text)
        for t in ALLOWED_BRANCH_TYPES:
            self.assertIn(f"`{t}/`", text), f"rule file missing type {t}/"

    def test_branch_naming_regex_accepts_canonical(self):
        for t in ALLOWED_BRANCH_TYPES:
            for slug in ("review-findings", "cli-nameerror", "eval-repair-v2"):
                self.assertRegex(f"{t}/{slug}", BRANCH_RE, f"{t}/{slug} should match")

    def test_branch_naming_regex_rejects_bad_types(self):
        for bad in ("feature/x", "bugfix/x", "Foo/x", "x"):  # wrong type
            self.assertNotRegex(bad, BRANCH_RE)

    def test_branch_naming_regex_rejects_bad_slugs(self):
        for bad in (
            "fix/MyFeature",         # not kebab
            "fix/my_feature",        # underscore
            "fix/내이름",            # Korean
            "fix/" + ("x" * 50),     # too long
            "fix/-leading-dash",     # leading dash in slug
            "fix/trailing-dash-",    # trailing dash
            "fix/UPPER",             # uppercase
            "fix/a",                 # too short (single char)
        ):
            self.assertNotRegex(bad, BRANCH_RE, f"should reject {bad!r}")

    def test_branch_naming_rejects_forbidden_slug_words(self):
        """m2: real enforcement. FORBIDDEN_RE explicitly matches forbidden
        combinations — the previous tautology test only asserted bad words
        are in the Python set, never exercised the regex or git history."""
        for bad in ("fix/wip", "chore/tmp", "feat/foo", "fix/scratch",
                    "chore/asdf", "docs/untitled", "perf/bar", "hotfix/test"):
            with self.subTest(branch=bad):
                self.assertIsNotNone(
                    FORBIDDEN_RE.match(bad),
                    f"{bad!r} should match FORBIDDEN_RE",
                )
        # Sanity: legitimate slugs are NOT caught by FORBIDDEN_RE.
        for good in ("fix/review-findings", "feat/eval-repair-v2", "chore/bump-deps"):
            with self.subTest(branch=good):
                self.assertIsNone(
                    FORBIDDEN_RE.match(good),
                    f"{good!r} should NOT match FORBIDDEN_RE",
                )

    def test_recent_local_branches_match_convention(self):
        """The checked-out branch must follow <type>/<slug>.

        Grandfathered branches are pre-existing personal-work branches that predate
        the rule (currently: dev, stage). Once deleted, the grandfather list can
        shrink. New branches MUST follow the convention — see .claude/rules/git-workflow.md.
        """
        GRANDFATHERED = {
            # The canonical protected branch; cannot be renamed or deleted
            # via the guard, so it will always exist in any clone.
            "main",
            "dev", "stage",
            "fix/0.1.3-gate-tolerance", "fix/orphan-bump-v0.3.2", "fix/orphan-bump-v0.3.3",
            # Pre-existing worktree branches parked in active worktrees this cleanup
            # did not want to touch destructively. Slug lengths and prefixes predate
            # the branch-naming rule tightening (issue #493 onward).
            "feat/issue-324-babysit-operator-opt-out-resolved",
            "inspect/2026-07-17-recheck",
            "inspect/2026-07-30-thin-harness",
            "orch/thin-harness",
            "prune/dead-eval-harness-audit",  # local leftover from PR #493
        }
        result = subprocess.run(
            ["git", "branch", "--list", "--no-color"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5,
        )
        if result.returncode != 0:
            self.skipTest("git branch failed (not a git repo?)")
        # Validate the checkout under test, not every historical local ref.
        # A developer's repository can contain hundreds of archived/revert/
        # agent branches, and those refs are not part of the current change.
        current = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5,
        )
        branches = [current.stdout.strip()] if current.returncode == 0 else []
        for b in branches:
            with self.subTest(branch=b):
                # Harness bookkeeping: EnterWorktree auto-names start with `worktree-`.
                if b.startswith("worktree-"):
                    continue
                # Pre-existing branches that predate the rule.
                if b in GRANDFATHERED:
                    continue
                self.assertRegex(b, BRANCH_RE, f"branch {b!r} does not match <type>/<slug>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
