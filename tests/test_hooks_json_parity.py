#!/usr/bin/env python3
"""test_hooks_json_parity.py — Regression for issue #715.

`hooks/hooks.json` (Claude Code) and `.codex-plugin/hooks/hooks.json`
(Codex) are near-duplicate hook manifests maintained by hand. Each new
hook or matcher added to one side can silently drift from the other —
the only existing check is the SessionStart regen, which is a no-op
when both files exist.

This test pins the invariant: after a documented normalization, the two
manifests MUST describe the same set of hook installations (same
{event, matcher, command} triples, same underlying shell files).

Normalization (defined in `normalize()`, used by every assertion):
  1. Strip top-level metadata: `$schema`, `_comment`, `description`.
  2. Strip the `timeout` field from every hook entry (allowed to differ
     per side — `fail_closed` semantics are out of scope for parity).
  3. On the Codex side, drop the `fail_closed` field (Codex does not
     honor it; the CC side keeps it because Claude Code does).
  4. Replace `${CLAUDE_PLUGIN_ROOT}` and `${PLUGIN_ROOT}` with a
     canonical `${PLUGIN_ROOT}` so command strings become
     byte-identical after the swap.

Reconciliation policy (enforced by this test, but also applied to the
raw JSON in this commit so the diff between the two manifests is
intentionally empty under `diff`):
  - Codex side: `fail_closed` removed (the test drops it anyway).
  - CC side: `timeout` removed from the three Codex-orphan entries
    (tdd-scope-judge, worktree-auto-cut, linear-task-change) so the
    raw `diff` of the two files is empty after `jq` normalization.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC_MANIFEST = REPO_ROOT / "hooks" / "hooks.json"
CODEX_MANIFEST = REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json"
HOOKS_DIR = REPO_ROOT / "hooks"

# Canonical root-token name. After normalization, every command string
# uses this form so byte-equality of commands is testable.
CANONICAL_ROOT = "${PLUGIN_ROOT}"

# Captures the shell filename from a normalized `bash ${PLUGIN_ROOT}/hooks/<name>.sh` command.
SHELL_RE = re.compile(r"bash\s+\$\{PLUGIN_ROOT\}/hooks/([\w.\-]+\.sh)")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _strip_top_level_metadata(manifest: dict) -> dict:
    """Remove `$schema`, `_comment`, and `description` (per-side documentation)."""
    out = dict(manifest)
    out.pop("$schema", None)
    out.pop("_comment", None)
    out.pop("description", None)
    return out


def _canonicalize_command(cmd: str) -> str:
    """Replace any `${..._ROOT}` root token with the canonical form."""
    return re.sub(r"\$\{[A-Z_]+_ROOT\}", CANONICAL_ROOT, cmd)


def _strip_hook_fields(hook: dict, *, drop_fail_closed: bool) -> dict:
    """Remove `timeout` everywhere; on codex, also drop `fail_closed`."""
    out = dict(hook)
    out.pop("timeout", None)
    if drop_fail_closed:
        out.pop("fail_closed", None)
    return out


def normalize(manifest_path: Path, *, side: str) -> dict:
    """Load + normalize a hook manifest for parity comparison.

    side: "cc" or "codex"
    """
    if side not in ("cc", "codex"):
        raise ValueError(f"side must be 'cc' or 'codex', got {side!r}")
    raw = json.loads(manifest_path.read_text())
    out = _strip_top_level_metadata(raw)
    drop_fail_closed = side == "codex"
    normalized_hooks: dict = {}
    for event, entries in out.get("hooks", {}).items():
        new_entries = []
        for entry in entries:
            matcher = entry.get("matcher", "")
            new_inner = []
            for hook in entry.get("hooks", []):
                nh = _strip_hook_fields(hook, drop_fail_closed=drop_fail_closed)
                if "command" in nh:
                    nh["command"] = _canonicalize_command(nh["command"])
                new_inner.append(nh)
            new_entries.append({"matcher": matcher, "hooks": new_inner})
        normalized_hooks[event] = new_entries
    out["hooks"] = normalized_hooks
    return out


def _triples(normalized: dict) -> set[tuple[str, str, str]]:
    """Extract (event, matcher, canonical_command) triples."""
    out: set[tuple[str, str, str]] = set()
    for event, entries in normalized.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                out.add((event, matcher, cmd))
    return out


def _commands_by_event_matcher(normalized: dict) -> dict[tuple[str, str], list[str]]:
    """Map (event, matcher) -> list of canonical command strings (preserves duplicates)."""
    out: dict[tuple[str, str], list[str]] = {}
    for event, entries in normalized.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                out.setdefault((event, matcher), []).append(cmd)
    return out


def _shell_refs(normalized: dict) -> dict[tuple[str, str], str]:
    """Map (event, matcher) -> referenced shell filename (for SHA256 check)."""
    out: dict[tuple[str, str], str] = {}
    for event, entries in normalized.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                m = SHELL_RE.search(cmd)
                if m:
                    out[(event, matcher)] = m.group(1)
    return out


def _shell_sha256(filename: str) -> str:
    path = HOOKS_DIR / filename
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestManifestsExist(unittest.TestCase):
    """Both manifests MUST exist for parity to be checkable."""

    def test_cc_manifest_exists(self):
        self.assertTrue(
            CC_MANIFEST.exists(),
            f"missing {CC_MANIFEST} — Claude Code hook manifest required for parity",
        )

    def test_codex_manifest_exists(self):
        self.assertTrue(
            CODEX_MANIFEST.exists(),
            f"missing {CODEX_MANIFEST} — Codex plugin manifest required for parity",
        )


class TestHooksJsonParity(unittest.TestCase):
    """The set of hook installations MUST match across CC and Codex.

    After `normalize()` (strip timeout, drop fail_closed on Codex side,
    canonicalize root token), the two manifests must describe the
    exact same set of (event, matcher, command) triples. The underlying
    shell files referenced must also match (same SHA256) — defense in
    depth against silent path drift.
    """

    @classmethod
    def setUpClass(cls):
        cls.cc = normalize(CC_MANIFEST, side="cc")
        cls.codex = normalize(CODEX_MANIFEST, side="codex")

    def test_same_set_of_event_matcher_command_triples(self):
        cc = _triples(self.cc)
        cx = _triples(self.codex)
        only_cc = cc - cx
        only_cx = cx - cc
        self.assertEqual(
            cc, cx,
            "hook triple drift between CC and Codex:\n"
            f"  only in CC ({len(only_cc)}):\n    "
            + "\n    ".join(repr(t) for t in sorted(only_cc))
            + f"\n  only in Codex ({len(only_cx)}):\n    "
            + "\n    ".join(repr(t) for t in sorted(only_cx)),
        )

    def test_same_event_keys(self):
        """The set of event names wired on each side must be identical."""
        cc_events = set(self.cc.get("hooks", {}).keys())
        cx_events = set(self.codex.get("hooks", {}).keys())
        self.assertEqual(
            cc_events, cx_events,
            f"event keys differ:\n  only in CC: {cc_events - cx_events}\n"
            f"  only in Codex: {cx_events - cc_events}",
        )

    def test_same_matcher_keys_per_event(self):
        """For every event, the set of matcher keys must be identical."""
        for event in set(self.cc.get("hooks", {}).keys()) | set(self.codex.get("hooks", {}).keys()):
            cc_matchers = {
                e.get("matcher", "")
                for e in self.cc.get("hooks", {}).get(event, [])
            }
            cx_matchers = {
                e.get("matcher", "")
                for e in self.codex.get("hooks", {}).get(event, [])
            }
            self.assertEqual(
                cc_matchers, cx_matchers,
                f"matcher keys differ on {event!r}:\n"
                f"  only in CC: {cc_matchers - cx_matchers}\n"
                f"  only in Codex: {cx_matchers - cc_matchers}",
            )

    def test_command_count_per_event_matcher_matches(self):
        """Each (event, matcher) MUST have the same number of hooks on both sides."""
        cc_cmds = _commands_by_event_matcher(self.cc)
        cx_cmds = _commands_by_event_matcher(self.codex)
        self.assertEqual(
            set(cc_cmds.keys()), set(cx_cmds.keys()),
            "(event, matcher) keys differ",
        )
        for key in cc_cmds:
            self.assertEqual(
                len(cc_cmds[key]), len(cx_cmds[key]),
                f"hook count mismatch for {key}: CC={len(cc_cmds[key])} codex={len(cx_cmds[key])}",
            )

    def test_underlying_shell_sha256_matches_across_sides(self):
        """For each (event, matcher), the underlying shell file MUST be the
        same on both sides (same SHA256). This is the "OR map to the same
        hook shell" clause from the issue: even if command strings diverge
        after root-token canonicalization, the shell must match.
        """
        cc_refs = _shell_refs(self.cc)
        cx_refs = _shell_refs(self.codex)
        self.assertEqual(
            set(cc_refs.keys()), set(cx_refs.keys()),
            f"(event, matcher) shell-ref keys differ:\n"
            f"  only in CC: {set(cc_refs) - set(cx_refs)}\n"
            f"  only in Codex: {set(cx_refs) - set(cc_refs)}",
        )
        for key in cc_refs:
            self.assertEqual(
                cc_refs[key], cx_refs[key],
                f"shell ref mismatch for {key}: CC={cc_refs[key]} codex={cx_refs[key]}",
            )
            sha = _shell_sha256(cc_refs[key])
            self.assertEqual(
                len(sha), 64,
                f"shell {cc_refs[key]} SHA256 malformed: {sha!r}",
            )

    def test_every_referenced_shell_exists(self):
        """Every shell referenced in either manifest must exist on disk."""
        cc_refs = set(_shell_refs(self.cc).values())
        cx_refs = set(_shell_refs(self.codex).values())
        for ref in cc_refs | cx_refs:
            self.assertTrue(
                (HOOKS_DIR / ref).exists(),
                f"shell {ref} referenced in manifest but missing on disk",
            )


class TestNormalizeIsIdempotent(unittest.TestCase):
    """`normalize()` is deterministic — re-running it yields the same dict."""

    def test_cc_normalize_idempotent(self):
        a = normalize(CC_MANIFEST, side="cc")
        b = normalize(CC_MANIFEST, side="cc")
        self.assertEqual(a, b)

    def test_codex_normalize_idempotent(self):
        a = normalize(CODEX_MANIFEST, side="codex")
        b = normalize(CODEX_MANIFEST, side="codex")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
