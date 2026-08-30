"""
test_hooks_json_parity.py — REGRESSION for issue #715.

`hooks/hooks.json` (Claude Code) and `.codex-plugin/hooks/hooks.json`
(Codex) are near-duplicates maintained by hand. Drift between them
silently installs a thinner harness on the losing side (CC or Codex),
and the only existing check is `session-start-check.sh` regen, which
is a no-op when both files already exist.

This test locks CC↔Codex hook-manifest parity:

  1. Loads both JSONs from the canonical plugin locations.
  2. Normalizes away the fields that are *allowed* to differ:
       - `timeout` (stripped — CC enforces a default; Codex currently
         does not expose the field, so values can differ intentionally
         without breaking parity).
       - `fail_closed` (stripped — Codex does not honor the field, so
         the CC-side value is irrelevant to actual runtime behavior).
       - `${CLAUDE_PLUGIN_ROOT}` (CC) and `${PLUGIN_ROOT}` (Codex)
         both substituted to the same `${PLUGIN_ROOT}` token so the
         string identities converge.
  3. Asserts both manifests yield the same set of
     `(event, matcher, command)` triples.
  4. Asserts every `command` resolves to an existing shell file under
     `hooks/` (the shells are shared; only the plugin-root prefix
     differs).

Failure modes that this test catches:

  * A new hook added to `hooks/hooks.json` but forgotten on the
    Codex side → asymmetric set, assertion fails.
  * `fail_closed` toggled on one side → irrelevant (field stripped).
  * New matcher wired on one side only → asymmetric set, assertion
    fails.
  * Command string references a shell that no longer exists → file
    resolution fails.
  * One side stops referencing a shared shell that the other still
    uses → asymmetric set, assertion fails.

The test is wired into `templates/ci/scripts/ci-local.sh` indirectly:
that script runs `scripts/test.sh`, which runs `pytest tests/`, which
picks this file up by name. Consumer repos installed via
`/dev-kit:ci-setup` therefore enforce parity automatically.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
CODEX_HOOKS_JSON = REPO_ROOT / ".codex-plugin" / "hooks" / "hooks.json"
HOOKS_DIR = REPO_ROOT / "hooks"

# Plugin-root prefix substitution. Both manifests reference shell files
# via env-var-prefixed paths; we collapse them to a single token so the
# residual comparison is just "same shell path".
_CC_ROOT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}")
# Codex may use ${PLUGIN_ROOT} or $PLUGIN_ROOT; either way substitute
# to the same token below so the two manifests compare string-equal.
_COMMON_PREFIX = "${PLUGIN_ROOT}"

# Regex to extract a `${...}/hooks/<name>.sh` style path out of a
# `bash ...` command. Captures the path the shell will execute.
_SHELL_PATH_RE = re.compile(r"(?:\$\{[A-Z_]+\})?/?hooks/[A-Za-z0-9_.\-]+\.sh")

# A leading `DEV_KIT_AGENT=<value> ` command prefix is an intentional,
# expected divergence between the two manifests — each runtime stamps
# its own identity so the harness-effectiveness stability submetric
# (issue #663) can see which agent emitted an event. Strip it before
# comparing triples, same as the plugin-root token substitution below.
_DEV_KIT_AGENT_RE = re.compile(r"^DEV_KIT_AGENT=\S+ ")


def _load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _normalize_command(cmd: str) -> str:
    """Collapse plugin-root tokens so CC and Codex paths align."""
    cmd = _DEV_KIT_AGENT_RE.sub("", cmd)
    return _CC_ROOT_RE.sub(_COMMON_PREFIX, cmd)


def _strip_volatile_fields(hook_entry: dict) -> dict:
    """Return a copy of a hook entry with `timeout` and `fail_closed` removed.

    These fields are allowed to differ between the two manifests:
      * `timeout` — CC may pin a per-hook timeout; Codex ignores the
        field, so equality would be a false positive.
      * `fail_closed` — Codex does not honor the field at runtime, so
        a value mismatch is benign.
    """
    return {
        k: v
        for k, v in hook_entry.items()
        if k not in ("timeout", "fail_closed")
    }


def _collect_triples(manifest: dict) -> set[tuple[str, str, str]]:
    """Walk a hook manifest and return the set of `(event, matcher, command)` tuples.

    `matcher` is the literal matcher string (`"*"`, `"Write|Edit|MultiEdit"`,
    etc.) or `""` if the manifest entry omits the matcher key. `command`
    is normalized to the common plugin-root token.
    """
    triples: set[tuple[str, str, str]] = set()
    hooks_section = manifest.get("hooks", {})
    for event, entries in hooks_section.items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            for hook in entry.get("hooks", []):
                if hook.get("type") != "command":
                    continue
                command = _normalize_command(hook["command"])
                triples.add((event, matcher, command))
    return triples


class TestHooksJsonParity(unittest.TestCase):
    """Regression suite for issue #715 — lock CC↔Codex hook-manifest parity."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cc_manifest = _load_manifest(CC_HOOKS_JSON)
        cls.codex_manifest = _load_manifest(CODEX_HOOKS_JSON)

    def test_both_manifests_load(self):
        """Sanity: both manifests exist and parse as JSON."""
        self.assertIsInstance(self.cc_manifest, dict)
        self.assertIsInstance(self.codex_manifest, dict)
        self.assertIn("hooks", self.cc_manifest)
        self.assertIn("hooks", self.codex_manifest)

    def test_event_keys_match(self):
        """Top-level event keys (`PreToolUse`, `SessionStart`, ...) must agree.

        A new event wired on one side without the other is a structural
        drift that the triple-comparison below would *not* catch on its
        own — the empty event section on the losing side would just
        contribute zero triples and the assertion would still pass.
        """
        cc_events = set(self.cc_manifest["hooks"].keys())
        codex_events = set(self.codex_manifest["hooks"].keys())
        self.assertEqual(
            cc_events,
            codex_events,
            f"Event keys differ.\n"
            f"  CC-only events: {sorted(cc_events - codex_events)}\n"
            f"  Codex-only events: {sorted(codex_events - cc_events)}",
        )

    def test_triple_sets_match(self):
        """Both manifests must yield the same `(event, matcher, command)` set.

        This is the headline assertion. After stripping `timeout`,
        `fail_closed`, and normalizing the plugin-root token,
        every hook on the CC side must have a counterpart on the Codex
        side and vice versa.
        """
        cc_triples = _collect_triples(self.cc_manifest)
        codex_triples = _collect_triples(self.codex_manifest)

        only_cc = cc_triples - codex_triples
        only_codex = codex_triples - cc_triples

        self.assertEqual(
            cc_triples,
            codex_triples,
            "Hook-manifest drift detected.\n"
            f"  CC-only entries ({len(only_cc)}):\n"
            + "\n".join(f"    {t}" for t in sorted(only_cc))
            + "\n"
            f"  Codex-only entries ({len(only_codex)}):\n"
            + "\n".join(f"    {t}" for t in sorted(only_codex)),
        )

    def test_no_timeout_field_in_normalized_view(self):
        """The normalized triple set must not depend on `timeout`.

        We re-derive the triples with `timeout` still present on the
        CC side and confirm the assertion holds — i.e. parity does
        not accidentally encode a timeout comparison.
        """
        cc_with_timeout = {
            event: [
                {
                    **entry,
                    "hooks": [
                        {**h, "timeout": 999} for h in entry.get("hooks", [])
                    ],
                }
                for entry in entries
            ]
            for event, entries in self.cc_manifest["hooks"].items()
        }
        mutated = {"hooks": cc_with_timeout}
        mutated_triples = _collect_triples(mutated)
        original_triples = _collect_triples(self.cc_manifest)
        self.assertEqual(
            mutated_triples,
            original_triples,
            "Normalization is leaking `timeout` into the triple set.",
        )

    def test_no_fail_closed_field_in_normalized_view(self):
        """The normalized triple set must not depend on `fail_closed`.

        Mirrors the `timeout` test: toggling `fail_closed` on a CC
        entry must not change the triple set, otherwise the parity
        check is silently enforcing a field Codex does not honor.
        """
        toggled = {
            "hooks": {
                event: [
                    {
                        **entry,
                        "hooks": [
                            {
                                **h,
                                "fail_closed": not h.get("fail_closed", False),
                            }
                            for h in entry.get("hooks", [])
                        ],
                    }
                    for entry in entries
                ]
                for event, entries in self.cc_manifest["hooks"].items()
            }
        }
        mutated_triples = _collect_triples(toggled)
        original_triples = _collect_triples(self.cc_manifest)
        self.assertEqual(
            mutated_triples,
            original_triples,
            "Normalization is leaking `fail_closed` into the triple set.",
        )

    def test_plugin_root_tokens_normalized(self):
        """After normalization no `${CLAUDE_PLUGIN_ROOT}` should remain.

        Both sides must converge to the common token; otherwise CC and
        Codex would silently keep distinct string identities.
        """
        cc_triples = _collect_triples(self.cc_manifest)
        codex_triples = _collect_triples(self.codex_manifest)
        all_commands = {cmd for _, _, cmd in cc_triples | codex_triples}
        for cmd in all_commands:
            self.assertNotIn(
                "${CLAUDE_PLUGIN_ROOT}",
                cmd,
                f"Command still references ${{CLAUDE_PLUGIN_ROOT}}: {cmd}",
            )

    def test_every_command_resolves_to_existing_shell(self):
        """Every shell referenced by either manifest must exist on disk.

        The shells are shared — both manifests point at the same
        `hooks/<name>.sh` files (only the plugin-root prefix differs).
        A missing shell file means the harness would silently fail at
        install time on one or both runtimes.
        """
        cc_triples = _collect_triples(self.cc_manifest)
        codex_triples = _collect_triples(self.codex_manifest)
        for _, _, cmd in cc_triples | codex_triples:
            m = _SHELL_PATH_RE.search(cmd)
            self.assertIsNotNone(
                m,
                f"Could not extract shell path from command: {cmd}",
            )
            rel_path = m.group(0)
            # Strip any leading `${...}/` so we land at the repo's
            # `hooks/<name>.sh` path.
            rel_path = re.sub(r"^\$\{[A-Z_]+\}/?", "", rel_path)
            full_path = REPO_ROOT / rel_path
            self.assertTrue(
                full_path.exists(),
                f"Hook shell referenced by command does not exist: "
                f"{cmd} -> {full_path}",
            )

    def test_normalize_command_collapses_cc_plugin_root(self):
        """Direct unit test for `_normalize_command`."""
        self.assertEqual(
            _normalize_command("bash ${CLAUDE_PLUGIN_ROOT}/hooks/foo.sh"),
            "bash ${PLUGIN_ROOT}/hooks/foo.sh",
        )
        # Already on Codex form — no-op.
        self.assertEqual(
            _normalize_command("bash ${PLUGIN_ROOT}/hooks/foo.sh"),
            "bash ${PLUGIN_ROOT}/hooks/foo.sh",
        )

    def test_normalize_command_strips_dev_kit_agent_prefix(self):
        """A leading `DEV_KIT_AGENT=<value> ` command prefix is an
        intentional, expected divergence between the two manifests (each
        runtime stamps its own identity for the harness-effectiveness
        stability submetric, issue #663) — normalization must strip it so
        CC's `DEV_KIT_AGENT=claude-code ...` and Codex's
        `DEV_KIT_AGENT=codex ...` converge to the same triple.
        """
        self.assertEqual(
            _normalize_command(
                "DEV_KIT_AGENT=claude-code bash ${CLAUDE_PLUGIN_ROOT}/hooks/foo.sh"
            ),
            "bash ${PLUGIN_ROOT}/hooks/foo.sh",
        )
        self.assertEqual(
            _normalize_command(
                "DEV_KIT_AGENT=codex bash ${PLUGIN_ROOT}/hooks/foo.sh"
            ),
            "bash ${PLUGIN_ROOT}/hooks/foo.sh",
        )

    def test_strip_volatile_fields_drops_timeout_and_fail_closed(self):
        """Direct unit test for `_strip_volatile_fields`."""
        entry = {
            "type": "command",
            "command": "bash ${PLUGIN_ROOT}/hooks/foo.sh",
            "timeout": 120,
            "fail_closed": True,
        }
        stripped = _strip_volatile_fields(entry)
        self.assertNotIn("timeout", stripped)
        self.assertNotIn("fail_closed", stripped)
        self.assertEqual(stripped["command"], entry["command"])


if __name__ == "__main__":
    unittest.main()
