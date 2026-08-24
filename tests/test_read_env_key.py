"""test_read_env_key.py — regression for `lib.read_env_key.read_env_key`.

Issue #711 promotes a single helper from the in-line
`lib/ci_setup._read_env_key()` to `lib/read_env_key.py` so that
`bin/set-provider.sh` (bash) and `lib/ci_setup.read_provider()` (Python)
share one parser. The bash-side parser is deleted and replaced by a
`python3 -c` call into this module. This file pins the contract.

Issue #310 (inspect-report overarch finding) had previously promoted
`ci_setup._read_env_key` (private) to `ci_setup.read_env_key` (public).
That public name now delegates to the new `lib.read_env_key.read_env_key`
so the helper has a single home; the existing test cases continue to
exercise the legacy name as a thin pass-through.

Coverage (>=8 cases; pinning the issue's acceptance criteria):
  C1  : missing file returns ""
  C2  : blank lines ignored
  C3  : comment lines (starting with `#`) ignored
  C4  : simple `KEY=value` returned verbatim
  C5  : double-quoted value strips one surrounding pair
  C6  : single-quoted value strips one surrounding pair
  C7  : last value wins on duplicate keys
  C8  : unrelated keys are not returned for the requested key
  C9  : `export KEY=value` prefix is handled (NEW behavior under #711)
  C10 : CRLF line endings (Windows-clone `.env`) are tolerated
  C11 : quoted value combined with `export` prefix
  C12 : asymmetric / malformed quotes are returned verbatim (no guess)
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
# `lib/` on sys.path so `ci_setup`'s consumer-side fallback
# (`from atomic import ...`) resolves when the module is loaded via
# spec_from_file_location (no parent package). Mirrors the historical
# path-injection in this file.
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
# Project root on sys.path so `from lib.read_env_key import read_env_key`
# resolves when the canonical helper is exercised via the package
# import (the canonical dev-harness-kit layout).
sys.path.insert(0, str(PROJECT_ROOT))


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / rel_path,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_ci_setup():
    return _load_module("ci_setup", "lib/ci_setup.py")


def _load_read_env_key():
    """Load `lib/read_env_key.py` as a top-level module named `read_env_key`.

    Using `spec_from_file_location` mirrors the consumer-side install
    (where `lib/` is a flat directory of `.py` files copied to the
    target repo without a package marker). The dev-harness-kit repo
    itself has `lib/__init__.py`, so the canonical import is
    `from lib.read_env_key import read_env_key`; the test exercises
    BOTH paths so neither side can silently regress.
    """
    return _load_module("read_env_key", "lib/read_env_key.py")


class TestReadEnvKeyPublicAPI(unittest.TestCase):
    """`ci_setup.read_env_key` exists and is a thin pass-through to the
    canonical helper."""

    @classmethod
    def setUpClass(cls):
        cls.cs = _load_ci_setup()

    def test_public_alias_exists(self):
        self.assertTrue(
            hasattr(self.cs, "read_env_key"),
            "ci_setup must expose `read_env_key` as a public function "
            "(issue #310 overarch)",
        )

    def test_public_alias_is_callable(self):
        self.assertTrue(callable(self.cs.read_env_key))

    def test_private_still_works_for_back_compat(self):
        """The old `_read_env_key` private alias must still work so existing
        call sites don't break on the same commit. Removal is a separate
        follow-up — this slice only adds the public API.
        """
        self.assertTrue(
            hasattr(self.cs, "_read_env_key"),
            "_read_env_key was removed; back-compat alias required",
        )
        self.assertIs(
            self.cs._read_env_key, self.cs.read_env_key,
            "_read_env_key must be the same function object as read_env_key",
        )

    def test_canonical_module_exists(self):
        """Issue #711: the canonical helper lives at `lib/read_env_key.py`."""
        helper = _load_read_env_key()
        self.assertTrue(
            hasattr(helper, "read_env_key"),
            "lib/read_env_key.py must expose `read_env_key`",
        )
        self.assertTrue(callable(helper.read_env_key))

    def test_ci_setup_delegates_to_canonical(self):
        """Issue #711: `ci_setup.read_env_key` must delegate to the canonical
        helper so the bash and Python sides cannot drift. Same returned
        value for the same `(path, key)` is the proof; identity is not
        required (a wrapper is fine).
        """
        helper = _load_read_env_key()
        ci = self.cs
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "x.env"
            p.write_text("FOO=bar\n", encoding="utf-8")
            self.assertEqual(
                ci.read_env_key(p, "FOO"),
                helper.read_env_key(p, "FOO"),
                "ci_setup.read_env_key must delegate to lib.read_env_key",
            )


class TestReadEnvKeyBehavior(unittest.TestCase):
    """Behavioral coverage for `read_env_key` (canonical module).

    The tests are written against `lib.read_env_key.read_env_key` so the
    contract is pinned at the helper itself, not at a wrapper. The same
    expectations hold for `ci_setup.read_env_key` (which delegates).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.read_env_key = _load_read_env_key().read_env_key

    def tearDown(self):
        self.tmp.cleanup()

    # C1: missing file returns "".
    def test_missing_file_returns_empty(self):
        self.assertEqual(self.read_env_key(self.root / "missing.env", "KEY"), "")

    # C2: blank lines ignored.
    def test_blank_lines_ignored(self):
        p = self.root / "x.env"
        p.write_text("\n\n\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "")

    # C3: `#` comment lines ignored.
    def test_comment_lines_ignored(self):
        p = self.root / "x.env"
        p.write_text("# FOO=bar\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "")

    # C4: simple `KEY=value`.
    def test_simple_value(self):
        p = self.root / "x.env"
        p.write_text("FOO=hello\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "hello")

    # C5: double-quoted value strips one surrounding pair.
    def test_double_quoted_stripped(self):
        p = self.root / "x.env"
        p.write_text('FOO="hello"\n', encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "hello")

    # C6: single-quoted value strips one surrounding pair.
    def test_single_quoted_stripped(self):
        p = self.root / "x.env"
        p.write_text("FOO='hello'\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "hello")

    # C7: last value wins on duplicate keys.
    def test_last_value_wins_on_repeat(self):
        # The helper reads "last `KEY=...` value" — repeated keys collapse
        # to the latest one. This matches the historical behavior pinned
        # by test_set_provider.py::test_upsert_collapses_duplicates.
        p = self.root / "x.env"
        p.write_text("FOO=first\nFOO=second\nFOO=third\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "third")

    # C8: unrelated keys are not returned for the requested key.
    def test_unrelated_keys_ignored(self):
        p = self.root / "x.env"
        p.write_text("OTHER=val\nFOO=match\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "match")
        self.assertEqual(self.read_env_key(p, "OTHER"), "val")
        self.assertEqual(self.read_env_key(p, "MISSING"), "")

    # C9: `export KEY=value` prefix is handled (issue #711, NEW behavior).
    # The OLD `lib/ci_setup._read_env_key()` returned "" here because it
    # compared `key` (including the `export ` prefix) to the requested
    # key without stripping the prefix. Both bash and Python sides
    # silently dropped `export CI_REVIEW_PROVIDER=...` lines — closing
    # that gap is the whole point of extracting the helper.
    def test_export_prefix_handled(self):
        p = self.root / "x.env"
        p.write_text("export FOO=exported_value\n", encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "exported_value")

    # C10: CRLF line endings tolerated (Windows-clone `.env`).
    def test_crlf_line_endings_tolerated(self):
        p = self.root / "x.env"
        # Use explicit \r\n so the test is platform-independent.
        p.write_bytes(b"FOO=hello\r\nBAR=world\r\n")
        self.assertEqual(self.read_env_key(p, "FOO"), "hello")
        self.assertEqual(self.read_env_key(p, "BAR"), "world")

    # C11: quoted value with `export` prefix — both behaviors compose.
    def test_export_prefix_with_quotes(self):
        p = self.root / "x.env"
        p.write_text('export FOO="quoted_value"\n', encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), "quoted_value")

    # C12: asymmetric / malformed quotes returned verbatim.
    def test_malformed_quotes_verbatim(self):
        # Only one side has a quote — the user wrote garbage; surface it.
        p = self.root / "x.env"
        p.write_text('FOO="unterminated\n', encoding="utf-8")
        self.assertEqual(self.read_env_key(p, "FOO"), '"unterminated')


if __name__ == "__main__":
    unittest.main(verbosity=2)
