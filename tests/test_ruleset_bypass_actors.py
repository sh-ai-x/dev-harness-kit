"""test_ruleset_bypass_actors.py — Regression test for the local ruleset
`bypass_actors` SSOT.

Iron Law L1: no prod code without a verification artifact. The
bypass_actors block in `.github/rulesets/protect-main.json` is the
local SSOT for the GitHub UI "Allow specified actors to bypass
required pull requests" checkbox list on ruleset 20232367
("protect main (admin PAT bypass)"). Without this artifact the
bypass list lives only on GitHub — invisible from the repo, and
silently lost on ruleset recreation. `lib/ci_ruleset.py` loads the
block + reports it; this test pins the load + cross-check behavior.

Layout:

  - Real fixture trees under `tests/fixtures/ruleset_bypass_actors/`
    exercise the loader against fully-formed JSON.
  - One unit test per interesting shape: admin-actor present
    (the contract the ruleset must satisfy), admin-actor missing
    (the regression case), RepositoryRole + User + Team actor
    shapes, malformed JSON is silently skipped, and the no-ruleset-
    file path is INFO-not-FAIL.

Every test loads files from disk via the real loaders
(`lib.ci_ruleset.py`) — no inline JSON strings — so a regression in
the loader and a regression in the cross-check logic are caught by
the same artifact.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))


def _load_ci_ruleset():
    spec = importlib.util.spec_from_file_location(
        "ci_ruleset", PROJECT_ROOT / "lib" / "ci_ruleset.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_ruleset"] = mod
    spec.loader.exec_module(mod)
    return mod


FIX_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "ruleset_bypass_actors"


def _fixture(subdir: str) -> Path:
    p = FIX_ROOT / subdir
    assert p.is_dir(), f"missing fixture dir: {p}"
    return p


class TestLoadBypassActors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cr = _load_ci_ruleset()

    def test_load_bypass_actors_admin_and_maintain_present(self):
        """The 'admin + maintain bypass' fixture — the canonical SSOT
        shape — loads both actors with the expected type/role/mode."""
        actors = self.cr.load_ruleset_bypass_actors(_fixture("admin_and_maintain"))
        self.assertEqual(len(actors), 2, f"expected 2 actors, got {actors}")
        by_role = {(a.actor_type, a.repository_role): a for a in actors}
        self.assertIn(("RepositoryRole", "ADMIN"), by_role)
        self.assertIn(("RepositoryRole", "MAINTAIN"), by_role)
        self.assertEqual(by_role[("RepositoryRole", "ADMIN")].bypass_mode, "always")
        self.assertEqual(by_role[("RepositoryRole", "MAINTAIN")].bypass_mode, "always")
        # Sanity: each BypassActor names its source file so error
        # messages can point the operator at the offending JSON.
        for a in actors:
            self.assertTrue(a.file.endswith("protect-main.json"))

    def test_load_bypass_actors_handles_user_and_team_shapes(self):
        """User + Team actors load with actor_id populated."""
        actors = self.cr.load_ruleset_bypass_actors(_fixture("user_and_team"))
        by_type: dict = {}
        for a in actors:
            by_type.setdefault(a.actor_type, []).append(a)
        self.assertEqual(len(by_type.get("User", [])), 1)
        self.assertEqual(by_type["User"][0].actor_id, 12345)
        self.assertEqual(by_type["User"][0].bypass_mode, "always")
        self.assertEqual(len(by_type.get("Team", [])), 1)
        self.assertEqual(by_type["Team"][0].actor_id, 67890)
        self.assertEqual(by_type["Team"][0].bypass_mode, "pull_request")

    def test_load_bypass_actors_returns_empty_when_no_ruleset_dir(self):
        """No `.github/rulesets/` -> empty list, not an error."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.cr.load_ruleset_bypass_actors(Path(td)), [])

    def test_load_bypass_actors_silently_skips_unparseable_json(self):
        """A corrupt ruleset file MUST NOT crash the loader."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ruleset = target / ".github" / "rulesets"
            ruleset.mkdir(parents=True)
            (ruleset / "broken.json").write_text("{not-json", encoding="utf-8")
            self.assertEqual(self.cr.load_ruleset_bypass_actors(target), [])

    def test_load_bypass_actors_skips_malformed_actor_entries(self):
        """A bypass_actor missing required keys is silently dropped
        (the ci-doctor row surfaces a WARN for the malformed file).
        This is the "fail-open on partial data, fail-loud on missing"
        contract: a half-broken actor never blocks the whole check."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ruleset = target / ".github" / "rulesets"
            ruleset.mkdir(parents=True)
            (ruleset / "protect-main.json").write_text(json.dumps({
                "name": "protect main",
                "bypass_actors": [
                    {"actor_type": "RepositoryRole", "repository_role": "ADMIN",
                     "bypass_mode": "always"},
                    {"actor_type": "RepositoryRole"},
                    "not-an-object",
                ],
            }), encoding="utf-8")
            actors = self.cr.load_ruleset_bypass_actors(target)
            self.assertEqual(len(actors), 1)
            self.assertEqual(actors[0].repository_role, "ADMIN")


class TestCheckBypassActors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cr = _load_ci_ruleset()

    def test_admin_and_maintain_fixture_passes(self):
        rows = self.cr.check_ruleset_bypass_actors(_fixture("admin_and_maintain"))
        states = [(r.label, r.state) for r in rows]
        self.assertTrue(
            any(s == "PASS" for _, s in states),
            f"expected at least one PASS row, got {states}",
        )
        self.assertFalse(
            any(s == "FAIL" for _, s in states),
            f"admin_and_maintain fixture must not FAIL: {states}",
        )

    def test_no_admin_actor_fixture_fails(self):
        """A ruleset that lacks any ADMIN actor fails the check —
        the contract is 'at least one RepositoryRole:ADMIN bypass
        actor'. This is the regression case: the checkbox was
        silently lost and the ruleset now blocks admin merges."""
        rows = self.cr.check_ruleset_bypass_actors(_fixture("no_admin_actor"))
        fails = [r for r in rows if r.state == "FAIL"]
        self.assertTrue(
            fails,
            f"no_admin_actor fixture must produce a FAIL row, got {rows}",
        )
        joined = " ".join(r.detail for r in fails)
        self.assertIn("ADMIN", joined,
                      "FAIL detail must name the missing ADMIN role")
        self.assertIn("protect-main.json", joined,
                      "FAIL detail must point at the offending ruleset file")

    def test_empty_bypass_actors_list_fails(self):
        """An empty `bypass_actors: []` (the explicit checkbox-clear
        state) MUST surface as FAIL — the contract is at least one
        ADMIN actor. This is the failure mode the user reported:
        'the checkbox feature disappeared', which on GitHub maps to
        clearing the bypass list."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ruleset = target / ".github" / "rulesets"
            ruleset.mkdir(parents=True)
            (ruleset / "protect-main.json").write_text(json.dumps({
                "name": "protect main",
                "bypass_actors": [],
            }), encoding="utf-8")
            rows = self.cr.check_ruleset_bypass_actors(target)
            self.assertTrue(
                any(r.state == "FAIL" for r in rows),
                f"empty bypass_actors must FAIL; got {[(r.label, r.state) for r in rows]}",
            )

    def test_no_local_ruleset_file_emits_info_row(self):
        """`.github/rulesets/` absent -> INFO row, never an error.
        Mirrors `check_ruleset_contract`'s contract so the two
        surfaces stay in lock-step."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".github" / "workflows").mkdir(parents=True)
            rows = self.cr.check_ruleset_bypass_actors(target)
            self.assertEqual(len(rows), 1,
                             f"expected one row, got {len(rows)}")
            self.assertEqual(rows[0].state, "INFO",
                             f"expected INFO row, got {rows[0].state}")

    def test_real_repo_protect_main_json_present_passes(self):
        """Pin the 'real repo has the bypass SSOT' invariant. The
        protect-main.json SSOT is the whole reason this contract
        exists — if a future prune deletes it, this test fires."""
        rows = self.cr.check_ruleset_bypass_actors(PROJECT_ROOT)
        self.assertTrue(
            any(r.state == "PASS" for r in rows),
            f"real repo must PASS (protect-main.json SSOT present); "
            f"got {[(r.label, r.state, r.detail) for r in rows]}",
        )

    def test_admin_actor_with_wrong_bypass_mode_fails(self):
        """An ADMIN actor with bypass_mode != 'always' fails the
        contract. 'always' is the only mode that lets the actor
        push to main directly; 'pull_request' only bypasses the
        status-check on PR branches. The whole point of this SSOT
        is to guarantee an admin can push to main when needed."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            ruleset = target / ".github" / "rulesets"
            ruleset.mkdir(parents=True)
            (ruleset / "protect-main.json").write_text(json.dumps({
                "name": "protect main",
                "bypass_actors": [
                    {"actor_type": "RepositoryRole",
                     "repository_role": "ADMIN",
                     "bypass_mode": "pull_request"},
                ],
            }), encoding="utf-8")
            rows = self.cr.check_ruleset_bypass_actors(target)
            self.assertTrue(
                any(r.state == "FAIL" for r in rows),
                f"ADMIN actor with bypass_mode != 'always' must FAIL; "
                f"got {[(r.label, r.state, r.detail) for r in rows]}",
            )
