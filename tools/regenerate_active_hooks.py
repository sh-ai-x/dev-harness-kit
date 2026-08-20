#!/usr/bin/env python3
"""
regenerate_active_hooks.py — Emit .dev-kit/.active-hooks.json from hooks/hooks.json.

Walks the canonical Claude Code hook wiring (`hooks/hooks.json`) and emits
a fresh `.dev-kit/.active-hooks.json` describing which hooks are currently
wired for which event. Called from `hooks/session-start-check.sh` so the
matrix snapshot is always regenerated before any session-start check
that might depend on it.

Output shape (MUST-13 SSOT, see `hooks/index.md`). Schema-coexistence
(issue #676): the matrix writer (`lib/active_hooks_codec.py`) stores its
own slice under the top-level `matrix` key. To keep both slices on disk
without trampling each other, this tool writes the regen slice under
`events` and preserves any pre-existing `matrix` / `override` slice on
re-run. The two writers now share one file with two namespaced slices.

    {
      "schema_version": "1.0.0",
      "generated_at": "2026-08-19T12:34:56+00:00",
      "events": {
        "<event_name>": [
          {"name": "<hook_basename>",
           "path": "hooks/<file>.sh",
           "when": "<matcher>",
           "fail_closed": true|false}
        ]
      },
      "matrix":   { ...codec slice, preserved on re-run...  },
      "override": { ...codec slice, preserved on re-run...  }
    }

`fail_closed` is read from the explicit `fail_closed: true|false` field
that hooks.json carries on every entry (mirrored from .codex-plugin).
The script-text regex detection was removed because it drifted across
files; the explicit field is the SSOT.

Idempotency: re-running with an unchanged `hooks/hooks.json` produces
byte-identical output (sorted events, sorted hook entries, sorted keys).
The codec slice is preserved verbatim from the existing file.

Exit codes:
  0  on success (file created or rewritten)
  1  if `hooks/hooks.json` is missing or unreadable (caller treats as
     fatal because the matrix snapshot is the artifact of that file)

CLI: `python3 tools/regenerate_active_hooks.py [--root DIR] [--quiet]`
  --root   project root containing hooks/hooks.json (default: cwd)
  --quiet  suppress the "wrote <path>" status line
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from atomic import atomic_write_json, read_json_or_default  # noqa: E402

SCHEMA_VERSION = "1.0.0"

# Top-level keys owned by this tool. The codec-owned keys (`matrix`,
# `override`) are preserved verbatim across re-runs so the two writers
# coexist on disk.
_REGEN_OWNED_KEYS = ("schema_version", "generated_at", "events")

# Path-prefix tokens we strip from a hook command string. The harness
# substitutes the env var at runtime; we only care about the script path.
_ENV_PREFIX_RE = re.compile(r"\$\{(?:CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT)\}/")


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with explicit +00:00 offset.

    We do NOT use `datetime.utcnow()` (naive) — the harness spec
    requires an explicit offset so downstream tooling can parse without
    guessing the local timezone.
    """
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


_LEADING_BASH_RE = re.compile(r"^bash\s+")

def _normalize_path(raw: str) -> str:
    """Strip `${CLAUDE_PLUGIN_ROOT}/` env prefix and a leading `bash ` token.

    Examples:
        `bash ${CLAUDE_PLUGIN_ROOT}/hooks/tdd-guard.sh`
            -> `hooks/tdd-guard.sh`
        `${PLUGIN_ROOT}/hooks/worktree-guard.sh`
            -> `hooks/worktree-guard.sh`
        `hooks/sub-agent-handoff.sh`
            -> `hooks/sub-agent-handoff.sh` (no-op)
    """
    s = raw.strip()
    s = _ENV_PREFIX_RE.sub("", s)
    s = _LEADING_BASH_RE.sub("", s)
    return s


def _derive_name(path: str) -> str:
    """`hooks/tdd-guard.sh` -> `tdd-guard`.

    We strip everything up to and including the last `/`, then the
    `.sh` suffix. Falls back to the full string when the path doesn't
    end in `.sh` (defensive — future hooks might be Python).
    """
    base = path.rsplit("/", 1)[-1]
    if base.endswith(".sh"):
        base = base[:-3]
    elif base.endswith(".py"):
        base = base[:-3]
    return base


def _walk_hooks_json(hooks_json_path: Path) -> Dict[str, List[Dict[str, object]]]:
    """Read hooks/hooks.json and emit the event -> entries mapping.

    Returns a dict keyed by event name; each value is a list of hook
    entries derived from the matcher+hooks lists. Hooks without a
    matcher (`SessionStart`, `UserPromptSubmit`, `Stop`) get `when=""`
    — the harness fires them unconditionally on those events.

    `fail_closed` MUST be present on every entry (explicit field, no
    inference). Missing entries raise SystemExit(1) — the explicit
    field is the SSOT and silent defaults would re-introduce the
    drift the field replaced.
    """
    raw = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    hooks_section = raw.get("hooks", {})
    out: Dict[str, List[Dict[str, object]]] = {}
    for event in sorted(hooks_section.keys()):
        entries: List[Dict[str, object]] = []
        # hooks.json shape per event: list of {matcher?, hooks: [...]}.
        for group in hooks_section[event]:
            matcher = group.get("matcher", "") or ""
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                # Skip entries without a command (e.g. prompt-based hooks
                # that some configurations emit — not used in this repo).
                if not cmd:
                    continue
                rel = _normalize_path(cmd)
                if "fail_closed" not in hook:
                    print(
                        f"regenerate_active_hooks: hooks/hooks.json entry "
                        f"{event}/{rel} is missing explicit `fail_closed` "
                        f"field. Add `\"fail_closed\": true|false` to the "
                        f"entry before regenerating.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if not isinstance(hook["fail_closed"], bool):
                    print(
                        f"regenerate_active_hooks: hooks/hooks.json entry "
                        f"{event}/{rel} has non-boolean `fail_closed` "
                        f"value: {hook['fail_closed']!r}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                entries.append({
                    "name": _derive_name(rel),
                    "path": rel,
                    "when": matcher,
                    "fail_closed": hook["fail_closed"],
                })
        out[event] = entries
    return out


def _build_payload(
    root: Path,
    hooks_by_event: Dict[str, List[Dict[str, object]]],
    existing: Dict[str, object],
) -> Dict[str, object]:
    """Attach regen-owned keys while preserving codec-owned keys.

    The codec slice (`matrix`, `override`) is preserved verbatim from
    the on-disk file when present. If the file does not yet exist,
    the codec keys are omitted (a fresh `ensure_matrix` call will
    populate them on the next read).
    """
    payload: Dict[str, object] = {}
    # Codec-owned slice — preserve verbatim when present.
    if isinstance(existing, dict):
        for key in ("matrix", "override"):
            if key in existing:
                payload[key] = existing[key]
    # Regen-owned slice — always overwrite.
    payload["schema_version"] = SCHEMA_VERSION
    payload["generated_at"] = _utc_now_iso()
    enriched: Dict[str, List[Dict[str, object]]] = {}
    for event, entries in sorted(hooks_by_event.items()):
        # Deterministic ordering — sort by (name, path, when) so the
        # bytes are stable across re-runs.
        sorted_entries = sorted(entries, key=lambda e: (e["name"], e["path"], e["when"]))
        enriched[event] = sorted_entries
    payload["events"] = enriched
    return payload


def regenerate(root: Path) -> Path:
    """Walk hooks/hooks.json and write .dev-kit/.active-hooks.json. Returns the path.

    Reads the existing file (if any) so the codec-owned `matrix` /
    `override` slice is preserved across re-runs. Both writers now
    share the same file via namespaced slices.
    """
    hooks_json = root / "hooks" / "hooks.json"
    if not hooks_json.is_file():
        print(
            f"regenerate_active_hooks: hooks/hooks.json not found at {hooks_json}",
            file=sys.stderr,
        )
        sys.exit(1)
    hooks_by_event = _walk_hooks_json(hooks_json)
    target = root / ".dev-kit" / ".active-hooks.json"
    existing = read_json_or_default(target, {})
    payload = _build_payload(root, hooks_by_event, existing)
    atomic_write_json(target, payload)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="project root containing hooks/hooks.json (default: cwd)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the 'wrote <path>' status line")
    args = parser.parse_args()
    target = regenerate(args.root.resolve())
    if not args.quiet:
        print(f"regenerate_active_hooks: wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
