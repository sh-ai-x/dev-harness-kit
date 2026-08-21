#!/usr/bin/env python3
"""test_atomic.py — RED-first tests for lib/atomic.py read helpers.

Targets the `read_json_or_default` helper extracted from the codecs
(state_codec, active_hooks_codec, ci_setup) that all repeat the same
"file missing → default, corrupt → default, otherwise json.loads"
pattern. Pure stdlib; uses tmpdirs only.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import atomic  # noqa: E402


class TestReadJsonOrDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_default(self):
        sentinel = {"_default": True}
        result = atomic.read_json_or_default(self.root / "missing.json", sentinel)
        self.assertEqual(result, sentinel)

    def test_valid_json_returns_parsed_dict(self):
        p = self.root / "ok.json"
        p.write_text(json.dumps({"k": 1, "nested": {"x": [1, 2]}}), encoding="utf-8")
        result = atomic.read_json_or_default(p, {"_default": True})
        self.assertEqual(result, {"k": 1, "nested": {"x": [1, 2]}})

    def test_valid_json_list_returns_list(self):
        p = self.root / "list.json"
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = atomic.read_json_or_default(p, [])
        self.assertEqual(result, [1, 2, 3])

    def test_corrupt_json_returns_default(self):
        p = self.root / "corrupt.json"
        p.write_text("not-json{", encoding="utf-8")
        sentinel = {"_default": True}
        result = atomic.read_json_or_default(p, sentinel)
        self.assertEqual(result, sentinel)

    def test_empty_file_returns_default(self):
        p = self.root / "empty.json"
        p.write_text("", encoding="utf-8")
        sentinel = {"_default": True}
        result = atomic.read_json_or_default(p, sentinel)
        self.assertEqual(result, sentinel)

    def test_default_can_be_none(self):
        # load_transcript / _load_session_cache in eval_runner use
        # default=None; verify the helper honors it.
        result = atomic.read_json_or_default(self.root / "missing.json", None)
        self.assertIsNone(result)

    def test_default_is_returned_by_reference_not_copied(self):
        # Mutating the returned default must NOT mutate the caller's
        # object — the helper returns the *same* object only on miss,
        # but mutating it is the caller's responsibility. The contract
        # is: on miss, return the exact default; on hit, return the
        # parsed JSON (a fresh object).
        sentinel: dict = {"_default": True}
        result = atomic.read_json_or_default(self.root / "missing.json", sentinel)
        self.assertIs(result, sentinel)

    def test_unicode_content_round_trips(self):
        p = self.root / "ko.json"
        p.write_text(json.dumps({"name": "테스트", "emoji": "🚀"}), encoding="utf-8")
        result = atomic.read_json_or_default(p, {})
        self.assertEqual(result, {"name": "테스트", "emoji": "🚀"})


class TestAtomicWriteJsonExceptionNarrowing(unittest.TestCase):
    """Bare-except narrowing for atomic_write_json.

    The cleanup branch must trigger only on the realistic failure modes
    of json.dump + os.fdopen + os.replace: OSError (disk / perm),
    ValueError (malformed input that json rejects), TypeError (object
    not JSON-serializable). KeyboardInterrupt / SystemExit must NOT be
    caught — those propagate and skip the cleanup (the OS will reap the
    tmpfile on its own, and we don't want to mask process shutdown).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_serializable_payload_raises_typeerror_and_cleans_tmp(self):
        target = self.root / "out.json"
        with self.assertRaises(TypeError):
            atomic.atomic_write_json(target, {"oops": set()})  # set() not JSON-serializable
        # No .tmp* files left behind in the parent dir.
        leftover = [p for p in self.root.iterdir() if p.name.startswith(".out.json") and p.name.endswith(".tmp")]
        self.assertEqual(leftover, [], msg=f"tmpfile leftover after TypeError: {leftover}")

    def test_oserror_propagates_and_cleans_tmp(self):
        target = self.root / "out.json"
        # Replace os.replace with a callable that raises OSError — simulates
        # cross-device or perms failure during the atomic rename step.
        with patch("atomic.os.replace", side_effect=OSError("simulated rename failure")):
            with self.assertRaises(OSError):
                atomic.atomic_write_json(target, {"k": 1})
        leftover = [p for p in self.root.iterdir() if p.name.startswith(".out.json") and p.name.endswith(".tmp")]
        self.assertEqual(leftover, [], msg=f"tmpfile leftover after OSError: {leftover}")

    def test_keyboard_interrupt_propagates_without_cleanup_attempt(self):
        """KeyboardInterrupt is BaseException, NOT Exception. The narrowed
        `except (OSError, ValueError, TypeError)` MUST NOT swallow it;
        the function must let BaseException propagate."""
        target = self.root / "out.json"
        with patch("atomic.json.dump", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                atomic.atomic_write_json(target, {"k": 1})


class TestAtomicWriteTextExceptionNarrowing(unittest.TestCase):
    """Bare-except narrowing for atomic_write_text.

    Inner ops: f.write(content) + os.replace. Realistic failures:
    OSError (disk / perm), UnicodeEncodeError (encoding-incompatible
    bytes). Anything else (e.g. ValueError from a custom file subclass)
    must NOT trigger cleanup — that would hide programmer errors.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_oserror_propagates_and_cleans_tmp(self):
        target = self.root / "out.txt"
        with patch("atomic.os.replace", side_effect=OSError("simulated rename failure")):
            with self.assertRaises(OSError):
                atomic.atomic_write_text(target, "hello")
        leftover = [p for p in self.root.iterdir() if p.name.startswith(".out.txt") and p.name.endswith(".tmp")]
        self.assertEqual(leftover, [], msg=f"tmpfile leftover after OSError: {leftover}")

    def test_keyboard_interrupt_propagates_without_cleanup_attempt(self):
        target = self.root / "out.txt"
        with patch("atomic.os.fdopen", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                atomic.atomic_write_text(target, "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
