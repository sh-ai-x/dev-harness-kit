#!/usr/bin/env python3
"""Tests for lib/role_config.py — team-mode role resolver + gate.

Covers:
  - missing `roles` block -> resolve_role returns None; allowed_skills returns empty set
  - valid `team` + empty `roles.members` -> empty set
  - valid `team` + user-declared roles -> returns the user's allowlist
  - `"*"` wildcard expands to the canonical dev-kit skill prefix list
  - mode gate: `full` / `lite` / `undev` + `roles` block raises `RoleConfigError`
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from role_config import (  # noqa: E402
    _DEV_KIT_SKILL_PREFIXES,
    RoleConfigError,
    allowed_skills,
    resolve_role,
)


def _project_with_settings(payload: dict | None) -> Path:
    """Materialize a temp project root with the given settings.json payload."""
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    claude = tmp_path / ".claude"
    claude.mkdir()
    if payload is not None:
        (claude / "settings.json").write_text(json.dumps(payload))
    # Register cleanup on the tempdir object; tests don't need to call
    # it explicitly because `addClassCleanup` (below) tears it down.
    RoleConfigTestCase._open_tmpdirs.append(tmp)
    return tmp_path


class RoleConfigTestCase(unittest.TestCase):
    """Shared base that tears down every temp project at the end of the run."""

    _open_tmpdirs: list[tempfile.TemporaryDirectory] = []

    @classmethod
    def tearDownClass(cls) -> None:
        for tmp in cls._open_tmpdirs:
            tmp.cleanup()
        cls._open_tmpdirs.clear()


class TestResolveRoleMissing(RoleConfigTestCase):
    def test_no_settings_file_returns_none(self):
        root = _project_with_settings(None)
        self.assertIsNone(resolve_role(root))

    def test_settings_without_roles_returns_none(self):
        root = _project_with_settings({"env": {"DEV_KIT_MODE": "team"}})
        self.assertIsNone(resolve_role(root))

    def test_empty_active_returns_none(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {"active": "", "members": {"alice": {"skills": ["dev-kit:plan"]}}},
        })
        self.assertIsNone(resolve_role(root))


class TestAllowedSkillsEmpty(RoleConfigTestCase):
    def test_no_settings_returns_empty_set(self):
        root = _project_with_settings(None)
        self.assertEqual(allowed_skills(root, "alice"), set())

    def test_unknown_role_returns_empty_set(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {"active": "alice", "members": {"alice": {"skills": ["dev-kit:plan"]}}},
        })
        self.assertEqual(allowed_skills(root, "bob"), set())

    def test_malformed_skills_returns_empty_set(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {"active": "alice", "members": {"alice": {"skills": "not-a-list"}}},
        })
        self.assertEqual(allowed_skills(root, "alice"), set())


class TestResolveRoleHappyPath(RoleConfigTestCase):
    def test_returns_active_role(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {
                "active": "reviewer",
                "members": {"reviewer": {"skills": ["dev-kit:review"]}},
            },
        })
        self.assertEqual(resolve_role(root), "reviewer")


class TestAllowedSkillsHappyPath(RoleConfigTestCase):
    def test_returns_declared_skills(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {
                "active": "implementer",
                "members": {
                    "implementer": {"skills": ["dev-kit:plan", "dev-kit:build", "dev-kit:ci-setup"]},
                },
            },
        })
        self.assertEqual(
            allowed_skills(root, "implementer"),
            {"dev-kit:plan", "dev-kit:build", "dev-kit:ci-setup"},
        )

    def test_wildcard_expands_to_canonical_set(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {
                "active": "owner",
                "members": {"owner": {"skills": ["*"]}},
            },
        })
        self.assertEqual(
            allowed_skills(root, "owner"),
            set(_DEV_KIT_SKILL_PREFIXES),
        )

    def test_wildcard_can_coexist_with_explicit_skills(self):
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "team"},
            "roles": {
                "active": "owner",
                "members": {"owner": {"skills": ["*", "custom:external-tool"]}},
            },
        })
        skills = allowed_skills(root, "owner")
        self.assertIn("dev-kit:plan", skills)
        self.assertIn("custom:external-tool", skills)


class TestModeGate(RoleConfigTestCase):
    """`roles` block present + mode != team -> RoleConfigError."""

    def _with_roles_block(self, mode: str) -> Path:
        return _project_with_settings({
            "env": {"DEV_KIT_MODE": mode},
            "roles": {
                "active": "reviewer",
                "members": {"reviewer": {"skills": ["dev-kit:review"]}},
            },
        })

    def test_full_mode_with_roles_raises(self):
        root = self._with_roles_block("full")
        with self.assertRaises(RoleConfigError):
            resolve_role(root)
        with self.assertRaises(RoleConfigError):
            allowed_skills(root, "reviewer")

    def test_lite_mode_with_roles_raises(self):
        root = self._with_roles_block("lite")
        with self.assertRaises(RoleConfigError):
            resolve_role(root)
        with self.assertRaises(RoleConfigError):
            allowed_skills(root, "reviewer")

    def test_undev_mode_with_roles_raises(self):
        root = self._with_roles_block("undev")
        with self.assertRaises(RoleConfigError):
            resolve_role(root)

    def test_invalid_mode_with_roles_raises(self):
        """A typo in `DEV_KIT_MODE` while a `roles` block exists fails closed."""
        root = _project_with_settings({
            "env": {"DEV_KIT_MODE": "teamish"},  # typo (deliberate; not a valid mode value)
            "roles": {"active": "reviewer", "members": {"reviewer": {"skills": ["dev-kit:review"]}}},
        })
        with self.assertRaises(RoleConfigError):
            resolve_role(root)


if __name__ == "__main__":
    unittest.main()
