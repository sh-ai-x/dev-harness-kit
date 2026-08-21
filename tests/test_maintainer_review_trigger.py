from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent


class TestMaintainerReviewTrigger(unittest.TestCase):
    def test_review_and_maintenance_accept_trusted_maintainer_forks(self):
        for name in ("review.yml", "maintenance.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("pull_request_target:", text)
            self.assertIn("github.event.pull_request.author_association", text)
            self.assertIn('"OWNER","MEMBER","COLLABORATOR"', text)
            self.assertIn("github.event_name == 'pull_request_target'", text)

    def test_fork_approval_gate_does_not_duplicate_trusted_maintainers(self):
        text = (ROOT / ".github" / "workflows" / "fork-pr-review.yml").read_text()
        self.assertIn("github.event.pull_request.author_association", text)
        self.assertIn('"OWNER","MEMBER","COLLABORATOR"', text)
        self.assertIn("!contains(fromJSON", text)

