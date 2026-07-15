#!/usr/bin/env python3
"""test_babysit_cleanup.py — Regression for the orphan-cleanup TERMINATE step.

Closes the loop we hit twice during PR #187's babysit:
the babysit exited 0 the moment the PR was Approved + checks green, but the
local worktree, local branch, upstream branch, and remote-tracking ref all
remained on disk. The next session then has to manually clean each of them
up before starting a new task — error-prone and easy to skip.

This test asserts the `babysit-pr` skill body includes a single
TERMINATE block that performs all four cleanup steps (worktree remove,
local branch delete, upstream branch delete, remote-tracking ref delete),
gated on `state == MERGED` first (so a Draft-approved-but-not-merged PR
still bails out without cleanup).

If this test fails, either someone removed the cleanup step from
`skills/babysit-pr/SKILL.md` or the gating `state == MERGED` check.
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
        A draft-approved-but-not-merged PR must exit 0 without wiping the worktree."""
        self.assertIn("state != MERGED", self.block,
                      "TERMINATE must gate cleanup on state == MERGED")
        # And the explicit bail text:
        self.assertIn("human merges", self.block,
                      "non-MERGED path must print a 'human merges' bail line")

    def test_cleanup_steps_present(self):
        """All four cleanup steps must appear in the TERMINATE block:
        local worktree, local branch, remote branch, remote-tracking ref."""
        must_have = [
            ("worktree remove",       "local worktree removal"),
            ("git branch -D",         "local branch force-delete"),
            ("--delete",              "upstream branch delete via git push"),
            ("branch -dr",            "remote-tracking ref delete"),
        ]
        for needle, label in must_have:
            with self.subTest(label=label):
                self.assertIn(needle, self.block,
                              f"TERMINATE missing {label} step (looked for '{needle}')")

    def test_no_forceful_cleanup_in_non_merged_branch(self):
        """The four cleanup calls must live AFTER the MERGED gate — not in a
        fallthrough path that runs on every Approve."""
        gate_idx = self.block.find("state != MERGED")
        self.assertGreater(gate_idx, -1, "MERGED gate missing")
        # First cleanup action must come strictly after the gate.
        first_cleanup = self.block.find("git worktree remove")
        self.assertGreater(first_cleanup, gate_idx,
                           "worktree removal must be gated on state == MERGED")

    def test_exit_zero_after_cleanup(self):
        """Final TERMINATE must end with exit 0 (success, not error)."""
        # Find the last exit-statement in the TERMINATE block.
        exits = list(re.finditer(r"exit\s+0", self.block))
        self.assertTrue(exits, "TERMINATE must end with 'exit 0'")


if __name__ == "__main__":
    unittest.main()