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

# UserPromptSubmit is a synchronous gate in front of every prompt — any
# hook that runs Python, hits the network, or walks a full file stalls
# the user. `tools/regenerate_active_hooks.py` enforces this with a
# regen-time lint (issue tracked in `fix/remove-userpromptsubmit-advisories`).
#
# Forbidden tokens are matched as bare substrings in the command string
# AFTER stripping the canonical `python3 -m lib.<module>` pattern (the
# in-plugin library invocation shape — used by the TDD scope judge
# hook, which has its own 45s subprocess timeout baked in and a
# fail-safe `tdd_required: true` default).
#
# Outside the canonical `python3 -m lib.<x>` shape, the lint rejects:
#   - `python` (any other invocation — `python -c`, heredoc `python3 -`,
#     bare `python script.py`) — those can stall on a long script.
#   - `curl` (any HTTP roundtrip).
#   - `git fetch` (network-bound).
#   - `git worktree` (touches the worktree config; the PreToolUse guard
#     is the right place).
#   - `jq -rs` (full-file slurp + reduce; the prior context-window-guard
#     shape).
#
# Tokens are matched in the post-strip command so the canonical patterns
# don't false-positive. `python3 -m lib.<x>` is the in-plugin library
# invocation shape (tdd-scope-judge); `command -v python3` is the
# interpreter-availability check that precedes the canonical call.
_USERPROMPT_SUBMIT_CANONICAL_PYTHON_PATTERNS = (
    "python3 -m lib.",
    "command -v python3",
)
_USERPROMPT_SUBMIT_FORBIDDEN_TOKENS = (
    "python",        # any python invocation OUTSIDE the canonical patterns
    "curl",          # any HTTP roundtrip
    "git fetch",     # network-bound
    "git worktree",  # touches the worktree config; the PreToolUse guard is the right place
    "jq -rs",        # full-file slurp + reduce; the prior context-window-guard shape
)
_USERPROMPT_SUBMIT_MAX_TIMEOUT = 5  # seconds; anything more is a design smell

# Path-prefix tokens we strip from a hook command string. The harness
# substitutes the env var at runtime; we only care about the script path.
_ENV_PREFIX_RE = re.compile(r"\$\{(?:CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT)\}/")

# A leading `DEV_KIT_AGENT=<value> ` command prefix (stamps producer
# identity for the harness-effectiveness stability submetric, issue
# #663) precedes the `bash` token. `_LEADING_BASH_RE` below is anchored
# (`^bash\s+`), so without stripping this prefix first the bash-strip
# silently no-ops and the corrupted string (env assignment + `bash` +
# path) lands in the regenerated matrix's `path` field.
_DEV_KIT_AGENT_PREFIX_RE = re.compile(r"^DEV_KIT_AGENT=\S+ ")


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
    """Strip a `DEV_KIT_AGENT=` prefix, `${CLAUDE_PLUGIN_ROOT}/` env
    prefix, and a leading `bash ` token, in that order.

    Examples:
        `bash ${CLAUDE_PLUGIN_ROOT}/hooks/tdd-guard.sh`
            -> `hooks/tdd-guard.sh`
        `${PLUGIN_ROOT}/hooks/worktree-guard.sh`
            -> `hooks/worktree-guard.sh`
        `hooks/sub-agent-handoff.sh`
            -> `hooks/sub-agent-handoff.sh` (no-op)
        `DEV_KIT_AGENT=claude-code bash ${CLAUDE_PLUGIN_ROOT}/hooks/tdd-guard.sh`
            -> `hooks/tdd-guard.sh`
    """
    s = raw.strip()
    s = _DEV_KIT_AGENT_PREFIX_RE.sub("", s)
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


def _walk_hooks_json(
    hooks_json_path: Path,
    repo_root: Path | None = None,
) -> Dict[str, List[Dict[str, object]]]:
    """Read hooks/hooks.json and emit the event -> entries mapping.

    Returns a dict keyed by event name; each value is a list of hook
    entries derived from the matcher+hooks lists. Hooks without a
    matcher (`SessionStart`, `UserPromptSubmit`, `Stop`) get `when=""`
    — the harness fires them unconditionally on those events.

    `fail_closed` MUST be present on every entry (explicit field, no
    inference). Missing entries raise SystemExit(1) — the explicit
    field is the SSOT and silent defaults would re-introduce the
    drift the field replaced.

    `repo_root` is used by the UserPromptSubmit lint to read each
    hook's script body and reject forbidden tokens there too (the
    command-string check alone leaves a gap: a script can call
    `python` from a wrapper without the word appearing in the
    command). Pass `None` to skip the body check (e.g. from tests
    that don't ship real hook files).
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
                # UserPromptSubmit is a synchronous gate in front of
                # every prompt. Anything that runs Python, hits the
                # network, walks a full file, or carries a generous
                # timeout stalls the user. The regen tool hard-rejects
                # such entries so a slow hook can never re-enter the
                # wiring — the only "allowed" UserPromptSubmit hook is
                # one that does an in-process regex / tail sample on
                # stdin within 5 seconds.
                if event == "UserPromptSubmit":
                    # Strip the canonical patterns before scanning —
                    # those are the in-plugin library invocation
                    # (`python3 -m lib.<x>`) and the interpreter check
                    # (`command -v python3`). Both are documented
                    # exceptions; everything else containing `python`
                    # is forbidden.
                    cmd_scanned = cmd
                    for pat in _USERPROMPT_SUBMIT_CANONICAL_PYTHON_PATTERNS:
                        cmd_scanned = cmd_scanned.replace(pat, "")
                    for token in _USERPROMPT_SUBMIT_FORBIDDEN_TOKENS:
                        if token in cmd_scanned:
                            print(
                                f"regenerate_active_hooks: UserPromptSubmit "
                                f"hook {rel} contains forbidden token "
                                f"{token!r}. UserPromptSubmit is a "
                                f"synchronous gate; anything that runs "
                                f"Python, hits the network, or walks a "
                                f"full file stalls the user. The "
                                f"documented exceptions are "
                                f"{_USERPROMPT_SUBMIT_CANONICAL_PYTHON_PATTERNS} "
                                f"(in-plugin library invocation / "
                                f"interpreter-availability check, both with "
                                f"subprocess timeouts + fail-safe defaults). "
                                f"Move the work to a PreToolUse / "
                                f"PostToolUse / SessionStart hook, an "
                                f"explicit skill, or a tail-sample regex.",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                    # Defense-in-depth: also read the script body and
                    # reject forbidden tokens there. A wrapper script
                    # can call `python` / `curl` / `git fetch` without
                    # the word appearing in the command string. The
                    # body check closes that gap; failure to read the
                    # script (missing file) is treated as a hard
                    # fail — a broken hook must not silently pass.
                    if repo_root is not None and rel.endswith(".sh"):
                        script_path = repo_root / rel
                        if not script_path.is_file():
                            print(
                                f"regenerate_active_hooks: UserPromptSubmit "
                                f"hook references missing script: {rel}",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                        try:
                            script_body = script_path.read_text(encoding="utf-8")
                        except OSError as exc:
                            print(
                                f"regenerate_active_hooks: UserPromptSubmit "
                                f"hook {rel} unreadable: {exc}",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                        # Strip comment lines so the lint doesn't false-
                        # positive on tokens that appear in docstrings
                        # describing the OLD shape. The lint is meant
                        # to gate NEW calls, not historical commentary.
                        body_non_comment = "\n".join(
                            line for line in script_body.splitlines()
                            if not line.lstrip().startswith("#")
                        )
                        # Strip the canonical patterns before scanning — the in-plugin
                        # library invocation (`python3 -m lib.<x>`,
                        # e.g. tdd-scope-judge) and the interpreter
                        # check (`command -v python3`) are documented
                        # exceptions; everything else containing
                        # `python` is forbidden.
                        body_scanned = body_non_comment
                        for pat in _USERPROMPT_SUBMIT_CANONICAL_PYTHON_PATTERNS:
                            body_scanned = body_scanned.replace(pat, "")
                        for token in _USERPROMPT_SUBMIT_FORBIDDEN_TOKENS:
                            if token in body_scanned:
                                print(
                                    f"regenerate_active_hooks: UserPromptSubmit "
                                    f"hook {rel} script body contains forbidden "
                                    f"token {token!r}. The command-string lint "
                                    f"alone is not sufficient — the body must "
                                    f"also stay cheap.",
                                    file=sys.stderr,
                                )
                                sys.exit(1)
                    timeout = hook.get("timeout", 0)
                    if isinstance(timeout, (int, float)) and timeout > _USERPROMPT_SUBMIT_MAX_TIMEOUT:
                        print(
                            f"regenerate_active_hooks: UserPromptSubmit "
                            f"hook {rel} has timeout={timeout}s, exceeds "
                            f"the {_USERPROMPT_SUBMIT_MAX_TIMEOUT}s cap. "
                            f"A UserPromptSubmit hook that needs a "
                            f"timeout is doing too much work — redesign "
                            f"or move it off UserPromptSubmit.",
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
    hooks_by_event = _walk_hooks_json(hooks_json, repo_root=root)
    target = root / ".dev-kit" / ".active-hooks.json"
    existing = read_json_or_default(target, {})
    payload = _build_payload(hooks_by_event, existing)
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
