"""
test_active_hooks_regen.py — RED-first regression for issue #664.

`hooks/index.md` line 3 documents `.dev-kit/.active-hooks.json` as the
MUST-13 hook-matrix SSOT. The file MUST be regenerated on every
SessionStart by `hooks/session-start-check.sh` so any check downstream
that consumes the matrix sees a fresh snapshot.

This test pins:

  1. Regenerating from scratch produces a JSON file with the right
     schema (`schema_version`, `generated_at`, `hooks` keyed by event
     with `{name, path, when, fail_closed}` entries).
  2. `schema_version` is a string.
  3. The `hooks` object has at least one entry (sanity — the repo
     currently wires PreToolUse / PostToolUse / SessionStart / Stop).
  4. Re-running the regeneration produces a deterministic output
     (excluding the volatile `generated_at` timestamp).

The test runs `tools/regenerate_active_hooks.py` against a copy of
`hooks/hooks.json` so we don't need the whole plugin checkout. This
keeps the test cheap (sub-second) and isolated from the rest of the
repo's `.dev-kit/` state.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "regenerate_active_hooks.py"
FIXTURE_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"


def _run_regen(root: Path) -> subprocess.CompletedProcess:
    """Invoke the regen tool against `root` (which must contain hooks/hooks.json)."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestActiveHooksRegeneration(unittest.TestCase):
    def setUp(self):
        # Snapshot hooks/hooks.json from the repo into a sandbox so the
        # test doesn't pollute the worktree's own `.dev-kit/`. We do
        # NOT delete the worktree's `.dev-kit/` — that file is harmless
        # to keep around and removing it would race with the session
        # itself.
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_HOOKS_JSON, self.root / "hooks" / "hooks.json")
        # Make sure no stale .dev-kit dir survives from a prior run.
        target = self.root / ".dev-kit" / ".active-hooks.json"
        if target.exists():
            target.unlink()

    def tearDown(self):
        self.tmp.cleanup()

    def test_regen_creates_file_when_missing(self):
        """Remove file -> run regen -> assert file exists and JSON parses."""
        target = self.root / ".dev-kit" / ".active-hooks.json"
        self.assertFalse(target.exists(), "precondition: target missing")
        result = _run_regen(self.root)
        self.assertEqual(result.returncode, 0, msg=f"regen stderr: {result.stderr}")
        self.assertTrue(target.exists(), "regen must create the file")
        # Must be valid JSON.
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)

    def test_schema_version_present_and_string(self):
        """schema_version MUST exist and MUST be a string (semver)."""
        target = self.root / ".dev-kit" / ".active-hooks.json"
        _run_regen(self.root)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("schema_version", data)
        self.assertIsInstance(data["schema_version"], str)
        # Non-empty sanity check.
        self.assertTrue(data["schema_version"].strip())

    def test_generated_at_iso8601_utc(self):
        """generated_at MUST be present and parseable as ISO-8601 UTC."""
        from datetime import datetime
        target = self.root / ".dev-kit" / ".active-hooks.json"
        _run_regen(self.root)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("generated_at", data)
        ts = data["generated_at"]
        # Must end in +00:00 (explicit UTC offset, not Z).
        self.assertTrue(
            ts.endswith("+00:00"),
            f"generated_at must end in +00:00 (UTC), got: {ts!r}",
        )
        # Must parse as ISO-8601.
        parsed = datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo, "timestamp must carry a tz offset")

    def test_hooks_object_has_at_least_one_entry(self):
        """Sanity: hooks object MUST contain at least one event entry."""
        target = self.root / ".dev-kit" / ".active-hooks.json"
        _run_regen(self.root)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("hooks", data)
        self.assertIsInstance(data["hooks"], dict)
        self.assertGreaterEqual(
            len(data["hooks"]),
            1,
            "hooks object must describe at least one event's wiring",
        )

    def test_hook_entry_shape(self):
        """Every hook entry MUST carry {name, path, when, fail_closed}."""
        target = self.root / ".dev-kit" / ".active-hooks.json"
        _run_regen(self.root)
        data = json.loads(target.read_text(encoding="utf-8"))
        required = {"name", "path", "when", "fail_closed"}
        for event, entries in data["hooks"].items():
            self.assertIsInstance(entries, list, f"{event} entries must be a list")
            self.assertGreater(len(entries), 0, f"{event} must have at least one hook")
            for entry in entries:
                self.assertEqual(
                    set(entry.keys()),
                    required,
                    f"{event} entry {entry.get('name')!r} keys must equal {required}",
                )
                self.assertIsInstance(entry["name"], str)
                self.assertIsInstance(entry["path"], str)
                self.assertIsInstance(entry["when"], str)
                self.assertIsInstance(entry["fail_closed"], bool)

    def test_rerun_produces_byte_identical_excluding_timestamp(self):
        """Re-running MUST produce byte-identical output modulo generated_at.

        `generated_at` is intentionally volatile (one timestamp per
        session start). All OTHER bytes MUST be deterministic — sorted
        event keys, sorted hook entries, sorted inner keys. The
        `atomic_write_json` helper already sorts keys; we additionally
        sort hook entries by (name, path, when) inside the script.
        """
        target = self.root / ".dev-kit" / ".active-hooks.json"
        _run_regen(self.root)
        first_bytes = target.read_bytes()
        # Tiny pause is unnecessary because replace(microsecond=0)
        # gives second-resolution; both runs within one second share
        # the same `generated_at` string.
        _run_regen(self.root)
        second_bytes = target.read_bytes()
        # Byte-for-byte equality including the timestamp is allowed
        # when the seconds match. If they straddle a second boundary
        # (rare), the timestamps will differ by one second — strip
        # `generated_at` from both parsed dicts and compare the rest.
        first = json.loads(first_bytes)
        second = json.loads(second_bytes)
        # Same shape (every key except generated_at must match).
        first.pop("generated_at", None)
        second.pop("generated_at", None)
        self.assertEqual(
            first,
            second,
            "regen output must be deterministic modulo generated_at",
        )
        # Also: when bytes DO match exactly, accept that as the
        # stronger "byte-identical" assertion the task spec asked for.
        # We don't fail if they differ only in `generated_at`.
        if first_bytes != second_bytes:
            # Confirm the only difference is generated_at.
            first_text = first_bytes.decode("utf-8")
            second_text = second_bytes.decode("utf-8")
            first_norm = re.sub(r'"generated_at": "[^"]+"', '"generated_at": "X"', first_text)
            second_norm = re.sub(r'"generated_at": "[^"]+"', '"generated_at": "X"', second_text)
            self.assertEqual(
                first_norm,
                second_norm,
                "bytes must be identical modulo generated_at when the "
                "two runs straddle a second boundary",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
