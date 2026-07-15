#!/usr/bin/env python3
"""test_babysit_cleanup.py — Regression for the orphan-cleanup TERMINATE step.

Closes the gap we hit during PR #187's babysit: the loop exited 0 the
moment the PR was Approved + checks green, but the local branch,
upstream branch, and remote-tracking ref all remained on disk and had
to be cleaned up by hand before the next task could start.

This test asserts the `babysit-pr` skill body includes a single
TERMINATE block that performs the THREE git-tracker cleanup steps
(local branch, upstream branch, remote-tracking ref), gated on
`state == MERGED` first, and that the local WORKTREE is explicitly
preserved so the captured session logs under
``<worktree>/logs/claude-code/`` and ``<worktree>/logs/codex/`` remain
intact for post-hoc /dev-kit:token-analyzer and /dev-kit:inspect runs.

If the worktree-preservation contract is removed (e.g. by reintroducing
``git worktree remove``), ``test_local_worktree_preserved`` fails
loudly so a future PR can't silently destroy the captured logs.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "babysit-pr" / "SKILL.md"


def _terminate_block(body: str) -> str:
    """Extract everything from the first 'TERMINATE' up to the next numbered
    algorithm step (CLASSIFY/WAIT/...) so assertions stay scoped."""
    m = re.search(
        r"2\.\s*TERMINATE\b(.*?)(?=\n\s{0,4}3\.\s*(?:CLASSIFY|WAIT|FETCH)\b)",
        body,
        re.DOTALL,
    )
    return m.group(1) if m else ""


class TestBabysitCleanup(unittest.TestCase):

    def setUp(self):
        self.body = SKILL_PATH.read_text(encoding="utf-8")
        self.block = _terminate_block(self.body)

    def test_terminate_block_present(self):
        """Skill body must still expose a step 2. TERMINATE after the edit."""
        self.assertIn("2. TERMINATE", self.body,
                      "babysit-pr SKILL.md missing the TERMINATE step")

    def test_merge_state_gate_before_cleanup(self):
        """Cleanup must only fire when PR is actually MERGED, not just approved.
        A draft-approved-but-not-merged PR must exit 0 without wiping git state."""
        self.assertIn("state != MERGED", self.block,
                      "TERMINATE must gate cleanup on state == MERGED")
        self.assertIn("human merges", self.block,
                      "non-MERGED path must print a 'human merges' bail line")

    def test_cleanup_steps_present(self):
        """The three git-tracker cleanup steps must appear in the TERMINATE block:
        local branch delete, upstream branch delete, remote-tracking ref delete."""
        must_have = [
            ("git branch -D",  "local branch force-delete"),
            ("--delete",       "upstream branch delete via git push"),
            ("branch -dr",     "remote-tracking ref delete"),
        ]
        for needle, label in must_have:
            with self.subTest(label=label):
                self.assertIn(needle, self.block,
                              f"TERMINATE missing {label} step (looked for '{needle}')")

    def test_local_worktree_preserved(self):
        """The local worktree MUST NOT be removed by the babysit — its
        logs/ dir feeds /dev-kit:token-analyzer and /dev-kit:inspect.

        The skill may reference ``git worktree remove`` as a hint for
        later manual cleanup, but only inside an explicit
        user-facing/manual hint sentence. It must NEVER appear as one
        of the numbered action steps.
        """
        # Find every numbered action step in the CLEANUP block (each
        # step is "1. <text>", "2. <text>", ...).
        numbered_steps = re.findall(r"^\s*\d+\.\s+(.+?)$", self.block, flags=re.MULTILINE)
        for step in numbered_steps:
            with self.subTest(step=step[:80]):
                self.assertNotIn("worktree remove", step.lower(),
                                 f"numbered action step must NOT execute "
                                 f"'git worktree remove' (step: {step[:80]!r})")
                # Worktree step, if any, must say UNTOUCHED/PRESERVED.
                if "worktree" in step.lower():
                    self.assertTrue(
                        any(w in step.lower() for w in ("untouched", "preserved")),
                        f"worktree-related action step must be UNTOUCHED "
                        f"or PRESERVED (step: {step[:80]!r})",
                    )
        # The preservation rationale must be visible somewhere in the
        # skill body so future edits can't silently remove it.
        self.assertIn("preserved", self.body.lower(),
                      "SKILL.md body must explicitly state the worktree is preserved")

    def test_cleanup_gated_after_merge(self):
        """The cleanup actions must live AFTER the MERGED gate."""
        gate_idx = self.block.find("state != MERGED")
        self.assertGreater(gate_idx, -1, "MERGED gate missing")
        first_cleanup = self.block.find("git branch -D")
        self.assertGreater(first_cleanup, gate_idx,
                           "local branch delete must be gated on state == MERGED")

    def test_exit_zero_after_cleanup(self):
        """Final TERMINATE must end with exit 0 (success, not error)."""
        exits = list(re.finditer(r"exit\s+0", self.block))
        self.assertTrue(exits, "TERMINATE must end with 'exit 0'")


if __name__ == "__main__":
    unittest.main()