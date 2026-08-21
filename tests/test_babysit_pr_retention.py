from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.babysit_pr_retention import is_retained, load_retention, write_retention


class TestBabysitPrRetention(unittest.TestCase):
    def test_active_and_terminal_worktree_are_retained(self):
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            path = write_retention(
                worktree,
                parent_pr=697,
                current_pr=697,
                branch="feat/example",
                log_root="/tmp/agent-logs",
                now="2026-08-21T00:00:00Z",
            )
            self.assertTrue(path.exists())
            self.assertTrue(is_retained(worktree))
            self.assertEqual(load_retention(worktree)["phase"], "active")

            write_retention(
                worktree,
                parent_pr=697,
                current_pr=697,
                branch="feat/example",
                phase="terminal",
                log_root="/tmp/agent-logs",
                now="2026-08-22T00:00:00Z",
            )
            record = json.loads(path.read_text())
            self.assertEqual(record["phase"], "terminal")
            self.assertTrue(record["retain_worktree"])
            self.assertTrue(record["retain_logs"])

    def test_missing_marker_is_not_retained(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(is_retained(td))

