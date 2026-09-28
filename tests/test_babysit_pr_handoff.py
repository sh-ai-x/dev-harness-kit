"""Contract tests for babysit-pr conversation-target handoff instructions."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
# `babysit-pr.md` and the sibling `babysit-pr-local.md` mirrors lived
# under `docs/skills/`; the whole mirror dir was removed. SKILL.md
# is the sole source of truth; SKILL and DOC are aliases.
SKILL = (ROOT / "skills" / "babysit-pr" / "SKILL.md").read_text(encoding="utf-8")
DOC = SKILL


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
        # SKILL.md wraps "never infer a target from recency or PR
        # number" across a soft line break; normalize whitespace.
        normalized = re.sub(r"\s+", " ", SKILL)
        self.assertIn("never infer a target from recency or PR number", normalized)

    def test_public_docs_mirror_the_evidence_threshold(self) -> None:
        # The docs/skills/babysit-pr.md mirror is gone; mirror-fidelity
        # is no longer a separate invariant — clauses are present in
        # SKILL.md (single source), no mirror to drift from.
        normalized = re.sub(r"\s+", " ", SKILL)
        self.assertIn("CONVERSATION_PR", normalized)
        self.assertIn("before any candidate enumeration", normalized)


if __name__ == "__main__":
    unittest.main()
