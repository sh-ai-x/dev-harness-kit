"""Spec §Testing strategy: bootstrap with N to the ci-setup prompt."""
from pathlib import Path


def test_bootstrap_no_ci_prompt_documented():
    """The bootstrap SKILL.md must document the [y/N] prompt and the unavailable-features list.

    The ci-setup prompt default flipped from [Y/n] (default Y) to [y/N] (default N)
    in PR #786. This test pins the new literal for the ci-setup prompt only --
    the git-defaults prompt (sub-stage 7) keeps its own [Y/n] default and is
    tested separately.
    """
    text = Path(__file__).parent.parent.joinpath("skills/bootstrap/SKILL.md").read_text()
    assert "ci-setup" in text.lower(), "expected ci-setup prompt documentation"
    # ci-setup prompt default flipped to [y/N] -- pin the exact prompt string.
    assert "Also install CI templates (ci-setup)? [y/N]" in text, \
        "ci-setup prompt is now `[y/N]` (PR #786); was `[Y/n]` before"
    # N branch is the default -- the "What is unavailable without ci-setup"
    # list is the path operators take when they answer N (or pass --skip-ci).
    assert "/dev-kit:ci-doctor" in text and "/dev-kit:bump" in text, \
        "unavailable-features list must include /dev-kit:ci-doctor and /dev-kit:bump"


# Mirror-fidelity test for the docs/skills/bootstrap.md mirror was
# dropped with the mirror collapse in the docs-skills-mirror PR.
# The mirror no longer exists, so the drift assertion is moot;
# the structural sections it asserted against (`## Usage`,
# `## When to use it`) lived only in the deleted mirror doc.
