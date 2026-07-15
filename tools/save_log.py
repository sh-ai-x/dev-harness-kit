#!/usr/bin/env python3
"""Stop-hook helper: saves the AI chat transcript into the submission's logs/ folder.

Invoked automatically by the Claude Code / Codex Stop hook after each turn.
Output: logs/<tool>/<session_id>.jsonl  (tool = claude-code | codex).
You do not need to run or edit this file.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_CODEX_CONV_EVENTS = ("user_message", "agent_message")
_CODEX_SYSTEM_PREFIXES = ("<permissions", "<environment_context", "<user_instructions")

_INVALID_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_branch(name: str) -> str:
    """Map a git ref name to a filesystem-safe single-segment directory label.

    - empty / ``.`` / ``..`` / ``/`` → ``"detached"`` (caller didn't get a usable ref).
    - any char outside ``[A-Za-z0-9._/-]`` → ``-`` (so ``feature/foo`` → ``feature-foo``).
    - leading/trailing ``-`` stripped.
    - length capped at 120 chars (path-length safety).
    """
    if not name or name in (".", "..", "/"):
        return "detached"
    cleaned = _INVALID_BRANCH_CHARS.sub("-", name).strip("-")
    if not cleaned:
        return "detached"
    return cleaned[:120]


def find_worktree_for_cwd(cwd: str, main_root: str) -> str | None:
    """Return the worktree directory path if ``cwd`` is inside a registered
    worktree of ``main_root``; otherwise ``None``.

    A worktree checkout has a ``.git`` *file* (not directory) containing
    ``gitdir: <main>/.git/worktrees/<name>``. Walking up from ``cwd`` until we
    find such a marker gives the worktree root. Cheap and robust — no
    subprocess, no ``git`` CLI calls, safe to invoke on every save_log() call.

    Returns ``None`` for the main checkout itself, any unrelated directory, or
    when ``cwd``/``main_root`` are falsy.
    """
    if not cwd or not main_root:
        return None
    try:
        p = Path(cwd).resolve()
    except OSError:
        return None
    main_root_resolved = Path(main_root).resolve()
    main_worktrees_dir = (main_root_resolved / ".git" / "worktrees").resolve()
    while True:
        git_path = p / ".git"
        if git_path.is_file():
            try:
                content = git_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if not content.startswith("gitdir: "):
                return None
            gitdir_raw = content[len("gitdir: "):].strip()
            gitdir = Path(gitdir_raw) if os.path.isabs(gitdir_raw) else (p / gitdir_raw).resolve()
            try:
                gitdir.relative_to(main_worktrees_dir)
                return str(p)
            except ValueError:
                return None
        if p.parent == p:
            return None
        p = p.parent


def find_main_repo_root(cwd: str) -> str | None:
    """Return the canonical main-checkout path for ``cwd``, or ``None``.

    A session started inside a worktree has its ``.git`` lives at
    ``<main>/.git/worktrees/<name>/`` while the **shared** ``.git`` stays in
    the main checkout. ``git rev-parse --git-common-dir`` returns that shared
    path; its parent is the main repo root where every session transcript
    should land — regardless of which checkout the user is running in. Falls
    back to ``None`` when ``cwd`` is not inside a git repo or git is
    unavailable; the caller then writes to ``cwd`` (preserving the legacy
    behavior of test fixtures and bare repos).

    Never raises — a logging helper must not break the participant's session.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode != 0:
            return None
        common = out.stdout.strip()
        if not common:
            return None
        common_path = common if os.path.isabs(common) else os.path.normpath(os.path.join(cwd, common))
        parent = os.path.dirname(common_path)
        return parent if parent and os.path.isdir(parent) else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def detect_branch(cwd: str) -> str:
    """Return a filesystem-safe branch label for ``cwd``, or ``"no-git"``.

    Order of preference:
      1. ``git -C <cwd> symbolic-ref --short -q HEAD``  (attached HEAD → real branch name)
      2. ``git -C <cwd> rev-parse --abbrev-ref HEAD``   (returns ``"HEAD"`` if detached)
         → fall back to ``f"detached-<short-sha>"`` via ``rev-parse --short HEAD``
      3. any failure (``git`` missing, not a repo, timeout) → ``"no-git"``

    Never raises — a logging failure must never block the participant's session.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return _sanitize_branch(out.stdout.strip())

        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0:
            ref = out.stdout.strip()
            if ref == "HEAD":
                sha = subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=2, check=False,
                ).stdout.strip()
                return _sanitize_branch(f"detached-{sha}" if sha else "detached")
            return _sanitize_branch(ref)
        return "no-git"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "no-git"


def _content_text(content) -> str:
    """message content (str or block list) -> conversation text only.

    Ignores tool_use/tool_result/thinking blocks, so a line carrying only those
    yields "" (and is dropped). Used to decide whether a line is real conversation.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        return "\n".join(parts).strip()
    return ""


def _claude_has_text(obj) -> bool:
    """True if a claude-code line is a real user/assistant turn carrying conversation text.

    Skips isMeta lines: claude-code injects slash-command output (e.g. /context),
    local-command stdout, and system reminders as user messages flagged isMeta=true —
    these are not conversation and can be huge (a /context dump is ~17KB).
    """
    if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"):
        return False
    if obj.get("isMeta"):
        return False
    message = obj.get("message")
    return isinstance(message, dict) and bool(_content_text(message.get("content")))


def _codex_has_event_text(obj) -> bool:
    """True if a codex line is a user_message/agent_message event with text."""
    if not isinstance(obj, dict) or obj.get("type") != "event_msg":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") not in _CODEX_CONV_EVENTS:
        return False
    message = payload.get("message")
    return isinstance(message, str) and bool(message.strip())


def _codex_has_response_text(obj) -> bool:
    """True if a codex response_item carries conversation text (fallback when no event_msg)."""
    if not isinstance(obj, dict) or obj.get("type") != "response_item":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("role") not in ("user", "assistant"):
        return False
    text = _content_text(payload.get("content"))
    return bool(text) and not text.lstrip().startswith(_CODEX_SYSTEM_PREFIXES)


def _codex_has_turn_context(obj) -> bool:
    """True if a codex line is a turn_context record (carries model).

    turn_context events carry ``payload.model`` (e.g. ``"gpt-5.6-luna"``) but no
    text; the prior text-only filter dropped them, so the analyzer never saw
    the model.
    """
    if not isinstance(obj, dict) or obj.get("type") != "turn_context":
        return False
    payload = obj.get("payload")
    return isinstance(payload, dict) and bool(payload.get("model"))


def _codex_has_event_tokens(obj) -> bool:
    """True if a codex line is a ``token_count`` event (carries usage info).

    codex emits ``event_msg`` with ``payload.type == "token_count"`` whose
    ``payload.info.total_token_usage`` holds the per-session input/cached/
    output token totals. The text-only filter rejected these because they
    have no ``payload.message``.
    """
    if not isinstance(obj, dict) or obj.get("type") != "event_msg":
        return False
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return False
    info = payload.get("info")
    return isinstance(info, dict) and ("total_token_usage" in info or "last_token_usage" in info)


def _codex_has_response_tool_call(obj) -> bool:
    """True if a codex line is a response_item carrying a tool call.

    ``payload.type`` of ``"function_call"`` or ``"custom_tool_call"`` carries
    ``payload.name``. Outputs (``*_output``) are NOT retained (huge and the
    call already names the tool).
    """
    if not isinstance(obj, dict) or obj.get("type") != "response_item":
        return False
    payload = obj.get("payload")
    return isinstance(payload, dict) and payload.get("type") in ("function_call", "custom_tool_call")


def slim_transcript(raw: str, tool: str):
    """Keep only the conversation lines from a transcript, verbatim, as JSONL.

    Drops lines that carry no user/assistant conversation text — tool calls/results,
    thinking/reasoning, session metadata, skill listings. Kept lines are the original
    JSONL lines unchanged, so the output stays valid JSONL in the source schema.
    Returns None when nothing parses or no conversation line is found, so the caller
    falls back to copying the transcript verbatim. Unparseable lines are skipped.
    """
    pairs, any_parsed = [], False
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        any_parsed = True
        pairs.append((line, obj))
    if not any_parsed:
        return None
    if tool == "codex":
        # Multiple codex record types carry model, tokens, tool calls, or
        # conversation text. Union them and dedupe by raw line so no record
        # is emitted twice (e.g. an agent_message whose slimmed body also
        # carries a non-text payload would otherwise match twice).
        seen: set[int] = set()
        kept: list[str] = []
        for idx, (line, obj) in enumerate(pairs):
            if idx in seen:
                continue
            if (
                _codex_has_event_text(obj)
                or _codex_has_turn_context(obj)
                or _codex_has_event_tokens(obj)
                or _codex_has_response_tool_call(obj)
            ):
                kept.append(line)
                seen.add(idx)
        if not kept:                            # fallback: response_item (no event_msg present)
            kept = [line for line, obj in pairs if _codex_has_response_text(obj)]
    else:
        kept = [line for line, obj in pairs if _claude_has_text(obj)]
    if not kept:
        return None
    return "\n".join(kept) + "\n"


def main() -> int:
    # Never write to stdout — Codex parses Stop-hook stdout as a decision.
    # Always exit 0 so a logging failure never blocks the participant's session.
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=["claude-code", "codex"])
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        print(f"save_log: failed to parse stdin JSON: {exc}", file=sys.stderr)
        return 0

    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "session"

    if not transcript_path or not os.path.isfile(transcript_path):
        print(
            f"save_log: transcript_path missing or not a file: {transcript_path!r}",
            file=sys.stderr,
        )
        return 0

    safe_session = os.path.basename(str(session_id))
    if safe_session in ("", ".", ".."):
        safe_session = "session"
    branch = detect_branch(cwd)
    main_root = find_main_repo_root(cwd) or cwd
    worktree_dir = find_worktree_for_cwd(cwd, main_root) if main_root != cwd else None

    # Read transcript once, reuse for both writes.
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        print(f"save_log: cannot read transcript: {exc}", file=sys.stderr)
        return 0
    slim = slim_transcript(raw, args.tool)
    content_bytes = slim.encode("utf-8") if slim is not None else None

    def _write(dest_dir: str) -> None:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{safe_session}.jsonl")
        try:
            if content_bytes is None:
                shutil.copyfile(transcript_path, dest)
            else:
                with open(dest, "wb") as out:
                    out.write(content_bytes)
        except OSError as exc:  # noqa: BLE001 - non-fatal
            print(f"save_log: write failed for {dest}: {exc}", file=sys.stderr)

    # Primary write: the main checkout's logs/<tool>/<branch>/. Every
    # session — main or worktree — converges here so the analyzer has
    # one canonical location to scan.
    _write(os.path.join(main_root, "logs", args.tool, branch))
    # Secondary write: the worktree's own logs/<tool>/<branch>/ when the
    # session actually started in a worktree. Lets the analyzer's
    # worktree_from_path() bucket the session under the right worktree
    # name. Without this, every worktree session falls back to (main)
    # attribution via the cwd field.
    if worktree_dir and os.path.realpath(worktree_dir) != os.path.realpath(main_root):
        _write(os.path.join(worktree_dir, "logs", args.tool, branch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
