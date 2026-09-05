"""Spec §Testing strategy: bootstrap with Y to the ci-setup prompt."""

from pathlib import Path


def test_bootstrap_with_ci_prompt_documented():
    """The bootstrap SKILL.md must document the [y/N] ci-setup prompt and the
    Y-branch install path; the ci-setup prompt default flipped from [Y/n]
    (default Y) to [y/N] (default N) in PR #786, so the Y branch is now the
    non-default "when chosen" path.
    """
    text = Path(__file__).parent.parent.joinpath("skills/bootstrap/SKILL.md").read_text()
    # Pin the EXACT ci-setup prompt string. The legacy substring `[Y/n]` is
    # still in the file (git-defaults uses it), so this assertion catches a
    # future revert of the ci-setup flip that the old `assert "[Y/n]" in text`
    # silently allowed.
    assert "Also install CI templates (ci-setup)? [y/N]" in text, \
        "ci-setup prompt is now `[y/N]` (PR #786); was `[Y/n]` before"
    # git-defaults prompt is a separate prompt with its own [Y/n] default.
    assert "Also configure operator-global git defaults" in text
    assert "(rebase.autoStash + pull.rebase)? [Y/n]" in text, \
        "git-defaults prompt keeps [Y/n] default (separate from ci-setup)"
    # The Y branch must invoke install_ci_config
    assert "install_ci_config" in text, "Y branch must delegate to install_ci_config"
    # Since the Y branch is no longer the default, its section heading must
    # read "y branch (when chosen)" — not "(default)", which would mislead
    # operators reading the docs.
    assert "### y branch (when chosen)" in text, \
        "Y branch heading must say '(when chosen)' since [y/N] flipped default"
    # The N branch IS now the default.
    assert "### n branch (default)" in text, \
        "N branch heading must say '(default)' since [y/N] flipped default"
    # The end state claim must reference the legacy bootstrap-full
    assert "bootstrap-full" in text or "legacy" in text.lower(), \
        "Y branch must document end-state parity with legacy /dev-kit:bootstrap-full"
