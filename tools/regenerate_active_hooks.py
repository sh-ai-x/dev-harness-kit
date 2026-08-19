#!/usr/bin/env python3
"""
regenerate_active_hooks.py — Emit .dev-kit/.active-hooks.json from hooks/hooks.json.

Walks the canonical Claude Code hook wiring (`hooks/hooks.json`) and emits
a fresh `.dev-kit/.active-hooks.json` describing which hooks are currently
wired for which event. Called from `hooks/session-start-check.sh` so the
matrix snapshot is always regenerated before any session-start check
that might depend on it.

Output shape (MUST-13 SSOT, see `hooks/index.md`):

    {
      "schema_version": "1.0.0",
      "generated_at": "2026-08-19T12:34:56+00:00",
      "hooks": {
        "<event_name>": [
          {"name": "<hook_basename>",
           "path": "hooks/<file>.sh",
           "when": "<matcher>",          // or "" when absent
           "fail_closed": true|false}
        ]
      }
    }

Idempotency: re-running with an unchanged `hooks/hooks.json` produces
byte-identical output (sorted events, sorted hook entries, sorted keys).

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

# Make `lib/` importable when invoked from any cwd (mirrors the bootstrap
# tools/ — `python3 tools/regenerate_active_hooks.py` works from the repo
# root without setting PYTHONPATH).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from atomic import atomic_write_json  # noqa: E402

SCHEMA_VERSION = "1.0.0"

# Path-prefix tokens we strip from a hook command string. The harness
# substitutes the env var at runtime; we only care about the script path.
_ENV_PREFIX_RE = re.compile(r"\$\{(?:CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT)\}/")

# Detects the fail mode from a hook script's header comments. Most hooks
# document their behaviour with a line like
#   `# Fails closed (exit 2 with deny JSON) when jq is missing.`
# We pattern-match the comment headers rather than the runtime code so
# the SSOT stays text-only (avoids importing shell scripts).
_FAIL_CLOSED_PATTERNS = (
    re.compile(r"fail[ -]closed", re.IGNORECASE),
    re.compile(r"fails closed", re.IGNORECASE),
)
_FAIL_OPEN_PATTERNS = (
    re.compile(r"always exit 0", re.IGNORECASE),
    re.compile(r"fails open", re.IGNORECASE),
    re.compile(r"never blocks", re.IGNORECASE),
    re.compile(r"non[- ]blocking", re.IGNORECASE),
)


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


def _detect_fail_closed(script_text: str) -> bool:
    """Best-effort: True iff the hook script's header says fail-closed.

    Order matters: explicit "fail-closed" wins over generic
    "non-blocking" wording because some hooks (e.g. `sub-agent-handoff`)
    are both advisory AND fail-closed on missing jq — see the comment
    in `sub-agent-handoff.sh`. Default is True (deny on error) because
    the hook layer leans toward hard blocks.
    """
    # Only scan the leading comment block (first 50 lines) — these
    # markers live in the file header, not in the body.
    header = "\n".join(script_text.splitlines()[:50])
    for pat in _FAIL_CLOSED_PATTERNS:
        if pat.search(header):
            return True
    for pat in _FAIL_OPEN_PATTERNS:
        if pat.search(header):
            return False
    return True  # default: fail-closed


def _read_hook_script(root: Path, rel_path: str) -> str:
    """Return the script body for fail-mode detection (best-effort)."""
    full = root / rel_path
    try:
        return full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _walk_hooks_json(hooks_json_path: Path) -> Dict[str, List[Dict[str, object]]]:
    """Read hooks/hooks.json and emit the event -> entries mapping.

    Returns a dict keyed by event name; each value is a list of hook
    entries derived from the matcher+hooks lists. Hooks without a
    matcher (`SessionStart`, `UserPromptSubmit`, `Stop`) get `when=""`
    — the harness fires them unconditionally on those events.
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
                entries.append({
                    "name": _derive_name(rel),
                    "path": rel,
                    "when": matcher,
                })
        out[event] = entries
    return out


def _build_payload(root: Path, hooks_by_event: Dict[str, List[Dict[str, object]]]) -> Dict[str, object]:
    """Attach fail-closed flags (script-text introspection) and wrap."""
    enriched: Dict[str, List[Dict[str, object]]] = {}
    for event, entries in sorted(hooks_by_event.items()):
        out_entries: List[Dict[str, object]] = []
        for entry in entries:
            rel_path = entry["path"]
            script_text = _read_hook_script(root, rel_path)
            entry_copy = dict(entry)
            entry_copy["fail_closed"] = _detect_fail_closed(script_text)
            out_entries.append(entry_copy)
        # Deterministic ordering — sort by (name, path, when) so the
        # bytes are stable across re-runs.
        out_entries.sort(key=lambda e: (e["name"], e["path"], e["when"]))
        enriched[event] = out_entries
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "hooks": enriched,
    }


def regenerate(root: Path) -> Path:
    """Walk hooks/hooks.json and write .dev-kit/.active-hooks.json. Returns the path."""
    hooks_json = root / "hooks" / "hooks.json"
    if not hooks_json.is_file():
        print(
            f"regenerate_active_hooks: hooks/hooks.json not found at {hooks_json}",
            file=sys.stderr,
        )
        sys.exit(1)
    hooks_by_event = _walk_hooks_json(hooks_json)
    payload = _build_payload(root, hooks_by_event)
    target = root / ".dev-kit" / ".active-hooks.json"
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
