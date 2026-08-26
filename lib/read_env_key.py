"""read_env_key.py — single source of truth for dotenv-style KEY=... parsing.

Issue #711: `bin/set-provider.sh` (bash) and `lib/ci_setup.read_provider()`
(Python) each shipped their own parser for the same `.env` format. They
silently drifted — a quoting or `export` prefix change in one would not
be mirrored in the other. The drift class (and its repro) is documented
in #711's "Drift scenarios" block.

This module is the canonical implementation. Both sides now delegate to
it:

* `bin/set-provider.sh:read_provider_from_env_file()` invokes it via
  `python3 -c "from lib.read_env_key import read_env_key ..."`.
* `lib/ci_setup.read_env_key()` is a thin wrapper around this helper.

Rules (also pinned by `tests/test_read_env_key.py`):
  * Return the last `KEY=...` value in `path`.
  * Skip blank lines and lines starting with `#`.
  * Handle `export KEY=...` prefix (the bash idiom).
  * Strip a single surrounding pair of single OR double quotes.
  * Handle CRLF line endings (`splitlines()` strips the trailing `\r`).
  * Return empty string when the file is missing/unreadable or the key
    is not present (no exception is raised — callers chain this into
    fallbacks like `lib/ci_setup.read_provider`).
"""
from __future__ import annotations

from pathlib import Path


def read_env_key(path: Path, key: str) -> str:
    """Return the last `KEY=...` value from a dotenv-style file.

    Args:
        path: dotenv-style file (typically `.env` or `.env.example`).
        key: variable name to extract (case-sensitive, no prefix).

    Returns:
        The last value of `key` found in the file, with surrounding
        single or double quotes stripped. Empty string when the file
        is missing/unreadable or the key is not present.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Missing file, permission denied, etc. — fall through to "".
        # Callers chain this into provider-resolution fallbacks and
        # expect a sentinel, never an exception.
        return ""
    out = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        # Handle `export KEY=...` (bash idiom). The space after `export`
        # is optional in some shells; tolerate both shapes.
        k_stripped = k.strip()
        if k_stripped.startswith("export "):
            k_stripped = k_stripped[len("export "):].strip()
        elif k_stripped.startswith("export\t"):
            k_stripped = k_stripped[len("export\t"):].strip()
        if k_stripped != key:
            continue
        v = v.strip()
        # Strip a single surrounding pair of quotes (single OR double).
        # A bare value that happens to start AND end with the SAME quote
        # is treated as quoted; an asymmetric value (e.g. `"foo`) is
        # returned verbatim (the user's `.env` is malformed but we
        # surface what they wrote instead of guessing).
        if len(v) >= 2 and (
            (v[0] == '"' and v[-1] == '"')
            or (v[0] == "'" and v[-1] == "'")
        ):
            v = v[1:-1]
        out = v
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: read_env_key.py <path> <KEY>", file=sys.stderr)
        raise SystemExit(2)
    print(read_env_key(Path(sys.argv[1]), sys.argv[2]), end="")
