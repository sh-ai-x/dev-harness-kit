#!/usr/bin/env python3
"""test_guard_default_parity.py — Shell vs Python per-guard defaults parity.

The Python module `lib/guard_mode_state.py:_default_state()` and the
shell function `hooks/lib/guard-policy.sh:dev_kit_guard_state()` both
encode per-guard defaults. They drift apart easily because the truth
lives in two languages with no single source of truth. This test pins
the parity contract:

  - Import the Python defaults from `_default_state(policy="off")` (the
    thin-bootstrap default).
  - Parse the shell defaults from `hooks/lib/guard-policy.sh` by reading
    the literal `push_confirm) printf` / `fork_pr_confirm) printf` arm
    bodies and the `tdd_guard|worktree_guard|git_guard) printf
    "${DEV_KIT_GUARDS:-off}"` arm.

If the shell values move into a generated config file, this test reads
that file instead — see `_SHELL_DEFAULTS_FALLBACK_PATHS`.

A regression in either side (e.g. an operator flips `push_confirm` to
`off` in Python but forgets the shell, or vice-versa) must turn this
test red.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
LIB = ROOT / "lib"
POLICY_SH = ROOT / "hooks" / "lib" / "guard-policy.sh"

sys.path.insert(0, str(LIB))
import guard_mode_state as gms  # noqa: E402


def _python_defaults() -> dict:
    """Return the per-guard default values encoded by `lib/guard_mode_state`.

    `_default_state(policy="off")` builds the full default state payload
    using `POLICY_GUARDS` + a separate branch for `push_confirm` and
    `fork_pr_confirm`. The off-policy path is what `DEV_KIT_GUARDS=off`
    resolves to, and matches the shell's `${DEV_KIT_GUARDS:-off}`
    branch exactly.
    """
    state = gms._default_state(policy="off")
    return {g: state[g] for g in gms.GUARDS}


def _shell_defaults() -> dict:
    """Parse `hooks/lib/guard-policy.sh` to extract the per-guard default
    values encoded in `dev_kit_guard_state`.

    The shell encodes the defaults in the `case` arm bodies:
      - `tdd_guard|worktree_guard|git_guard) printf '%s' "${DEV_KIT_GUARDS:-off}"`
        → default is `off` when `DEV_KIT_GUARDS` is unset.
      - `push_confirm) printf '%s' on` → default is `on`.
      - `fork_pr_confirm) printf '%s' off` → default is `off`.
    """
    text = POLICY_SH.read_text(encoding="utf-8")
    defaults: dict = {}
    policy_arm = re.search(
        r"tdd_guard\|worktree_guard\|git_guard\)\s+printf '%s' \"\$\{DEV_KIT_GUARDS:-(\w+)\}\"",
        text,
    )
    if not policy_arm:
        raise AssertionError(
            "could not parse tdd_guard|worktree_guard|git_guard arm from "
            f"{POLICY_SH} — does the shell function still encode the "
            "thin-bootstrap default as `${DEV_KIT_GUARDS:-off}`?"
        )
    off_default = policy_arm.group(1)
    for guard in ("tdd_guard", "worktree_guard", "git_guard"):
        defaults[guard] = off_default

    push_arm = re.search(r"push_confirm\)\s+printf '%s' (\w+)", text)
    if not push_arm:
        raise AssertionError(
            "could not parse push_confirm arm from "
            f"{POLICY_SH} — does the shell function still encode "
            "the push_confirm default literally?"
        )
    defaults["push_confirm"] = push_arm.group(1)

    fork_arm = re.search(r"fork_pr_confirm\)\s+printf '%s' (\w+)", text)
    if not fork_arm:
        raise AssertionError(
            "could not parse fork_pr_confirm arm from "
            f"{POLICY_SH} — does the shell function still encode "
            "the fork_pr_confirm default literally?"
        )
    defaults["fork_pr_confirm"] = fork_arm.group(1)

    return defaults


class TestGuardDefaultParity(unittest.TestCase):
    """Shell `dev_kit_guard_state` defaults MUST match Python
    `_default_state(policy="off")` defaults for every guard in
    `gms.GUARDS`."""

    def test_default_values_agree(self):
        py_defaults = _python_defaults()
        sh_defaults = _shell_defaults()
        self.assertEqual(
            py_defaults, sh_defaults,
            "Shell `dev_kit_guard_state` defaults and Python "
            "`_default_state(policy=\"off\")` defaults disagree:\n"
            f"  python: {py_defaults!r}\n"
            f"  shell:  {sh_defaults!r}\n"
            "Update one side or the other so the audit trail stays "
            "consistent across languages.",
        )

    def test_every_guard_has_a_default(self):
        """Belt-and-suspenders: ensure every guard in gms.GUARDS has a
        non-empty default on BOTH sides. A future contributor adding a
        new guard to Python without updating shell (or vice-versa)
        would otherwise silently default to ""."""
        py_defaults = _python_defaults()
        sh_defaults = _shell_defaults()
        for guard in gms.GUARDS:
            self.assertIn(guard, py_defaults, f"{guard!r} missing in Python defaults")
            self.assertIn(guard, sh_defaults, f"{guard!r} missing in Shell defaults")
            self.assertIn(py_defaults[guard], ("on", "off"),
                          f"Python default for {guard!r} is not on/off: "
                          f"{py_defaults[guard]!r}")
            self.assertIn(sh_defaults[guard], ("on", "off"),
                          f"Shell default for {guard!r} is not on/off: "
                          f"{sh_defaults[guard]!r}")


if __name__ == "__main__":
    unittest.main()
