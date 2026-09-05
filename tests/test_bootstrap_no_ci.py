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
    # The mirrored docs page must also reflect the new default, otherwise
    # consumers reading docs/skills/bootstrap.md get the wrong answer.
    docs_text = Path(__file__).parent.parent.joinpath("docs/skills/bootstrap.md").read_text()
    assert "Also install CI templates (ci-setup)? [y/N]" in docs_text, \
        "docs/skills/bootstrap.md must mirror the new [y/N] ci-setup prompt"
    # N branch is the default -- the "What is unavailable without ci-setup"
    # list is the path operators take when they answer N (or pass --skip-ci).
    assert "/dev-kit:ci-doctor" in text and "/dev-kit:bump" in text, \
        "unavailable-features list must include /dev-kit:ci-doctor and /dev-kit:bump"
