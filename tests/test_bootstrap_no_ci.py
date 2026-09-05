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


def test_bootstrap_docs_mirror_yN_default():
    """docs/skills/bootstrap.md and bootstrap.ko.md must mirror the [y/N] flip.

    PR #786 flipped ci-setup from default Y to default N. Three locations in
    the consumer docs missed the update and still claimed "default is Y" /
    "기본은 Y" — Korean-reading operators got contradictory defaults between
    the prompt literal (`[y/N]`) and the Usage table ("기본은 Y"). This test
    catches a re-flip or a re-merge that reintroduces the drift.
    """
    from pathlib import Path
    repo = Path(__file__).parent.parent

    # English doc: line 39 ("Usage" row) must NOT say "default is Y"; line 22
    # already says "Default is N" -- assert no contradiction.
    en = (repo / "docs/skills/bootstrap.md").read_text()
    assert "Also install CI templates (ci-setup)? [y/N]" in en, \
        "docs/skills/bootstrap.md must pin the [y/N] ci-setup prompt (line 22)"
    # Forbid the legacy phrase on the (0-arg) row (line 39) -- that row claims
    # "default is Y on both", which contradicts line 22 / line 40.
    en_usage_block = en.split("## Usage", 1)[1].split("## ", 1)[0]
    assert "default is Y" not in en_usage_block, \
        "docs/skills/bootstrap.md Usage table must drop 'default is Y' (PR #786 flipped to N)"
    # Forbid the Y-default claim in the opening paragraph (line 7).
    en_intro = en.split("## When to use it", 1)[0]
    assert "With Y (default)" not in en_intro and "Y (default), end state" not in en_intro, \
        "docs/skills/bootstrap.md intro must drop 'With Y (default)' claim"

    # Korean doc: line 10 (intro), line 72 (Usage table) -- forbid 기본은 Y / Y(기본).
    ko = (repo / "docs/skills/bootstrap.ko.md").read_text()
    assert "[y/N]" in ko, "docs/skills/bootstrap.ko.md must mention the [y/N] literal"
    ko_intro = ko.split("## 사용 시점", 1)[0]
    assert "Y(기본)일 경우" not in ko_intro and "Y(기본)" not in ko_intro, \
        "docs/skills/bootstrap.ko.md intro must drop 'Y(기본)' claim (PR #786 flipped to N)"
    if "## " in ko.split("## 사용 시점", 1)[1]:
        ko_usage_block = ko.split("## 사용 시점", 1)[1]
        ko_usage_block = ko_usage_block.split("## ", 1)[1] if "## " in ko_usage_block else ko_usage_block
    else:
        ko_usage_block = ""
    assert "기본은 Y" not in ko_usage_block, \
        "docs/skills/bootstrap.ko.md Usage table must drop '기본은 Y' (PR #786 flipped to N)"
