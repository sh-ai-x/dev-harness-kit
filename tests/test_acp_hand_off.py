"""test_acp_hand_off.py — regression tests for the canonical ACP
sub-agent prompt template at `lib/sub-agent-prompt.md`.

Closes #282 (hand-off-template half). Verifies the lint contract that
`tests/test_acp_dispatch.py::FillPlaceholders` enforces on the
template-level half:

  * The canonical template file exists at `lib/sub-agent-prompt.md`
    (lives next to `lib/acp_dispatch.py`, not under `skills/`).
  * The template's frontmatter (if any) declares it is a template, not a
    skill (no `name:` / `category:` matching the `skills/<name>/SKILL.md`
    shape).
  * All seven mandatory placeholders are present as literal `<NAME>`
    strings (mirror of `lib/acp_dispatch.py::SEVEN_PLACEHOLDERS`).
  * The `[tier-assert]` and `[tier-done]` literals from
    `docs/architecture/acp-harness.md` §2.2 and §2 are present.
  * A redacted sample dispatch (provided in the test fixture) parses
    and resolves every placeholder against a stub orch-worktree.

These tests are the prompt-template half of the tier-cognition enforcement
contract after the C-decision lateral_think (2026-10) deleted
`hooks/acp-tier-assert.sh`. The runtime hook previously caught a missing
tier-assert literal on every dispatched session's first tool call; this
file is the lint that catches the same mistake at template-fill time so a
future copy-paste or refactor cannot drop the literal without CI failure.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "lib" / "sub-agent-prompt.md"

# Seven mandatory placeholders, mirroring lib/acp_dispatch.py::SEVEN_PLACEHOLDERS.
# The test refuses any template missing one.
SEVEN_PLACEHOLDERS: tuple[str, ...] = (
    "<TASK>",
    "<BRANCH>",
    "<WORKTREE_PATH>",
    "<CWD>",
    "<PLUGIN_VERSION_TARGET>",
    "<LOCK_FILE>",
    "<PARENT_SESSION_CWD>",
)

# Tier-cognition literals from docs/architecture/acp-harness.md §2.2
# and §2.3. Both must survive any future template rewrite.
TIER_ASSERT_LITERAL = "[tier-assert] I am Tier <N> (<M|T|L>). cwd is <WORKTREE_PATH>. I own <OWNERSHIP_SENTENCE>."
TIER_DONE_LITERAL = "[tier-done] Tier <N> (<M|T|L>) on branch <BRANCH>: <one-line exit summary>."


class TemplateShape(unittest.TestCase):
    def test_template_file_exists(self) -> None:
        self.assertTrue(TEMPLATE.is_file(), f"missing canonical template: {TEMPLATE}")

    def test_template_declares_itself_as_template_in_frontmatter(self) -> None:
        # The first lines of the file are an HTML comment that names the
        # file as a TEMPLATE and explains why it lives in lib/ rather
        # than skills/. We assert on the textual signal so a future
        # rename of the template still trips this test.
        head = TEMPLATE.read_text(encoding="utf-8")[:512]
        self.assertIn("TEMPLATE", head, "canonical template frontmatter missing 'TEMPLATE' marker")
        self.assertIn("sub-agent-prompt", head.lower())

    def test_template_is_not_a_skill(self) -> None:
        # Skills live under skills/<name>/SKILL.md and carry a YAML
        # frontmatter with `name:` and (typically) `description:`. The
        # ACP template must NOT carry that shape — a copy-paste that
        # promoted it to a skill would put it in the slash-command
        # namespace, which the template cannot tolerate (it has
        # placeholders that would be rejected by the skill loader).
        head = TEMPLATE.read_text(encoding="utf-8")[:512]
        # The frontmatter is an HTML comment, not a YAML block.
        self.assertFalse(
            head.lstrip().startswith("---"),
            "template starts with '---' (YAML frontmatter); expected an HTML comment so it cannot be misloaded as a SKILL.md",
        )
        # No `name: <something>` line at the top of the file — would
        # make the skill loader think this is a SKILL.md.
        self.assertNotRegex(
            head,
            r"^name:\s*\S",
            "template carries a 'name:' line at the top — looks like a SKILL.md, which the skill loader would try to mount as a slash command",
        )


class TemplateContract(unittest.TestCase):
    def setUp(self) -> None:
        self.body = TEMPLATE.read_text(encoding="utf-8")

    def test_all_seven_placeholders_present_as_literal_strings(self) -> None:
        for placeholder in SEVEN_PLACEHOLDERS:
            self.assertIn(
                placeholder,
                self.body,
                f"canonical template is missing mandatory placeholder {placeholder}",
            )

    def test_tier_assert_literal_present(self) -> None:
        # Closes the prompt-template half of the tier-cognition
        # contract (docs/architecture/acp-harness.md §2.2 + §2.3). If
        # a future refactor drops the literal, this fails before any
        # dispatched T can be tempted to skip it.
        self.assertIn(TIER_ASSERT_LITERAL, self.body, "canonical template missing tier-assert literal")

    def test_tier_done_literal_present(self) -> None:
        # Companion to tier-assert: the done-condition marker from §2
        # of acp-harness.md. Same rationale — the template must carry
        # the literal so a dispatched T knows when to stop.
        self.assertIn(TIER_DONE_LITERAL, self.body, "canonical template missing tier-done literal")

    def test_ownership_sentence_table_lists_all_three_tiers(self) -> None:
        # The literal block enumerates one OWNERSHIP_SENTENCE per
        # tier (M / T / L). All three must be present so the M can
        # resolve the placeholder per dispatch.
        for sentence in (
            "the round state and dispatch decisions only",
            "ONE PR's lifecycle on branch <BRANCH>",
            "read-only investigation for T on branch <BRANCH>; no edits",
        ):
            self.assertIn(
                sentence,
                self.body,
                f"canonical template missing ownership sentence for tier that contains: {sentence!r}",
            )


class RedactedSampleDispatch(unittest.TestCase):
    """Substitute the seven placeholders with a redacted sample and
    verify the rendered output is well-formed.

    No git, no actual worktree — the test only verifies that the template
    is structurally sound (every placeholder resolves, the rendered
    output still carries the tier-assert and tier-done literals).
    """

    def test_redacted_dispatch_renders_cleanly(self) -> None:
        body = TEMPLATE.read_text(encoding="utf-8")
        sample = {
            "TASK": "do thing",
            "BRANCH": "feat/sample",
            "WORKTREE_PATH": "/tmp/sample",
            "CWD": "/tmp/sample",
            "PLUGIN_VERSION_TARGET": "0.0.0",
            "LOCK_FILE": "/tmp/sample.lock",
            "PARENT_SESSION_CWD": "/tmp",
        }
        rendered = body
        for placeholder in SEVEN_PLACEHOLDERS:
            rendered = rendered.replace(placeholder, sample[placeholder.strip("<>")])
        # All seven placeholders resolved — no `<NAME>` should survive.
        for placeholder in SEVEN_PLACEHOLDERS:
            self.assertNotIn(
                placeholder,
                rendered,
                f"placeholder {placeholder} not substituted — likely a typo or duplicate key",
            )
        # The two tier literals carry literal sentinel values that
        # the dispatched T reads to know its role. They must survive
        # the placeholder fill unmodified.
        self.assertIn("[tier-assert]", rendered)
        self.assertIn("[tier-done]", rendered)
        # The tier-assert literal still carries the worktree-path
        # placeholder format (the template only substitutes the
        # <WORKTREE_PATH> as a whole; the literal's middle "Worktree
        # path" placeholder is left literal for the agent to know
        # where to fill it).
        self.assertIn("I am Tier <N>", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
