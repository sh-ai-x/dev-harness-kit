"""Contract tests for babysit-pr conversation-target handoff instructions."""
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKILL = (ROOT / "skills" / "babysit-pr" / "SKILL.md").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "skills" / "babysit-pr.md").read_text(encoding="utf-8")


class TestBabysitPrConversationHandoff(unittest.TestCase):
    def test_only_explicit_pr_evidence_establishes_handoff(self) -> None:
        self.assertIn("literal PR number", SKILL)
        self.assertIn("immediately preceding assistant tool result", SKILL)
        self.assertIn('"babysit the latest PR"', SKILL)

    def test_conversation_validation_precedes_candidate_enumeration(self) -> None:
        validate = SKILL.index("Validate a conversation handoff")
        enumerate_candidates = SKILL.index("list candidate PRs off main")
        self.assertLess(validate, enumerate_candidates)
        self.assertIn('gh pr view "$CONVERSATION_PR"', SKILL)
        self.assertIn('CONVERSATION_STATE" != "OPEN"', SKILL)

    def test_validated_handoff_bypasses_candidate_count(self) -> None:
        self.assertIn(
            "Exactly one candidate, or a validated conversation handoff",
            SKILL,
        )
        self.assertIn("goes directly to", SKILL)

    def test_ambiguous_discovery_remains_fail_safe(self) -> None:
        self.assertIn(
            "Multiple candidates without a conversation handoff",
            SKILL,
        )
        self.assertIn("Never auto-pick", SKILL)
        self.assertIn("never infer a target from recency or PR number", DOC)

    def test_public_docs_mirror_the_evidence_threshold(self) -> None:
        self.assertIn("CONVERSATION_PR", DOC)
        self.assertIn("Vague references", DOC)
        self.assertIn("before any candidate enumeration", DOC)


if __name__ == "__main__":
    unittest.main()
