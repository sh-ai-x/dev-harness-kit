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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_CODEX_CONV_EVENTS = ("user_message", "agent_message")
_CODEX_SYSTEM_PREFIXES = ("<permissions", "<environment_context", "<user_instructions")

_INVALID_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret|authorization)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\b(?:ghp|gho|ghs|ghr|github_pat|sk)-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}\b"),
)


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


def _redact_secrets(text: str) -> str:
    """Redact common credential-shaped values before telemetry persistence."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _repository_label(main_root: str) -> str:
    return _sanitize_branch(Path(main_root).name)


def resolve_log_root(main_root: str) -> tuple[Path, bool]:
    """Resolve the durable root and whether it is externally configured.

    The default keeps the existing main-checkout ``logs/`` contract. Setting
    ``AGENT_LOG_ROOT`` moves canonical telemetry outside both the main checkout
    and all worktrees, which is the cleanup-safe deployment mode.
    """
    configured = os.environ.get("AGENT_LOG_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser() / _repository_label(main_root), True
    return Path(main_root) / "logs", False


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _metadata_payload(*, tool: str, session_id: str, experiment_id: str | None,
                      cwd: str, main_root: str, branch: str,
                      worktree_dir: str | None, log_path: Path) -> bytes:
    metadata = {
        "schema_version": "1.0",
        "session_id": session_id,
        "experiment_id": experiment_id,
        "tool": tool,
        "repository": _repository_label(main_root),
        "branch": branch,
        "cwd": cwd,
        "worktree": worktree_dir,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "log_path": str(log_path),
    }
    return (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")


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


def _claude_has_usage(obj) -> bool:
    """True if a claude-code assistant line carries token-accounting usage.

    Tool-call-only assistant turns have no conversation text (so _claude_has_text
    drops them) but still carry message.usage (input/output/cache tokens) and the
    tool_use blocks the analyzer counts. Retaining them keeps token + tool + Read
    accounting accurate — the text-only filter under-counted claude-code spend by
    ~50%. Mirrors the codex-side _codex_has_event_tokens fix. isMeta lines
    (slash-command output, /context dumps) stay excluded.
    """
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return False
    if obj.get("isMeta"):
        return False
    message = obj.get("message")
    return isinstance(message, dict) and isinstance(message.get("usage"), dict)


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


def _codex_has_session_meta(obj) -> bool:
    """True if a codex line carries session identity or working directory."""
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return False
    payload = obj.get("payload")
    return isinstance(payload, dict) and bool(
        payload.get("session_id") or payload.get("sessionId") or payload.get("cwd")
    )


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
                or _codex_has_session_meta(obj)
                or _codex_has_turn_context(obj)
                or _codex_has_event_tokens(obj)
                or _codex_has_response_tool_call(obj)
            ):
                kept.append(line)
                seen.add(idx)
        if not kept:                            # fallback: response_item (no event_msg present)
            kept = [line for line, obj in pairs if _codex_has_response_text(obj)]
    else:
        # Union: keep conversation text (user + assistant) AND every usage-bearing
        # assistant turn. Tool-call-only turns have no text but carry message.usage +
        # tool_use blocks the analyzer needs; text-only filtering dropped ~50% of the
        # token accounting. A line matching both predicates is emitted once.
        kept = [line for line, obj in pairs
                if _claude_has_text(obj) or _claude_has_usage(obj)]
    if not kept:
        return None
    return "\n".join(kept) + "\n"


def _is_worktree_active(cwd: str, main_root: str) -> bool:
    """Return True when ``cwd``'s worktree is still listed in
    ``git -C <main_root> worktree list --porcelain``.

    Used by ``--archive-stale`` to detect sessions whose worktree has
    been ``git worktree remove``'d but whose logs would otherwise
    keep accumulating on disk. When git is unavailable or the call
    fails, we default to *active* (do not archive) so a degraded
    environment never silently drops logs.
    """
    if not cwd or not main_root:
        return True
    try:
        out = subprocess.run(
            ["git", "-C", main_root, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return True
    if out.returncode != 0:
        return True
    cwd_real = os.path.realpath(cwd)
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            if os.path.realpath(line.split(maxsplit=1)[1]) == cwd_real:
                return True
    return False


def main() -> int:
    # Never write to stdout — Codex parses Stop-hook stdout as a decision.
    # Always exit 0 so a logging failure never blocks the participant's session.
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=["claude-code", "codex"])
    parser.add_argument(
        "--archive-stale",
        action="store_true",
        help=(
            "When the cwd's worktree is no longer in `git worktree list`, "
            "route the worktree-side log write to logs/.archive/<branch>/<ts>/ "
            "instead of logs/<tool>/<branch>/. The main-checkout canonical "
            "write is unchanged so /dev-kit:token-analyzer still sees the "
            "session. Off by default to preserve current behavior."
        ),
    )
    args = parser.parse_args()

    # Env-var override (opt-in for hand-edited hooks): if the operator
    # sets SAVE_LOG_ARCHIVE_STALE=1 in the hook's environment, behave as
    # if --archive-stale was passed. This lets existing SessionEnd /
    # Stop hook commands pick up the feature without rewriting the
    # hook JSON. Flag wins over env (callers can force-disable).
    if os.environ.get("SAVE_LOG_ARCHIVE_STALE", "").strip() in ("1", "true", "yes"):
        if not args.archive_stale:
            args.archive_stale = True

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:  # narrow: stdin payload must be JSON
        print(f"save_log: failed to parse stdin JSON: {exc}", file=sys.stderr)
        return 0

    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or "session"
    experiment_id = payload.get("experiment_id") or payload.get("prompt_id")

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
    # Redact before slimming so dropped records cannot accidentally be copied
    # by the raw-transcript fallback path.
    raw = _redact_secrets(raw)
    slim = slim_transcript(raw, args.tool)
    content_bytes = slim.encode("utf-8") if slim is not None else None

    log_root, external_root = resolve_log_root(main_root)

    def _write(dest_dir: str, *, include_metadata: bool = False) -> None:
        dest = Path(dest_dir) / f"{safe_session}.jsonl"
        try:
            payload = content_bytes
            if payload is None:
                payload = raw.encode("utf-8")
            _atomic_write_bytes(dest, payload)
            if include_metadata:
                _atomic_write_bytes(
                    dest.with_suffix(".meta.json"),
                    _metadata_payload(
                        tool=args.tool,
                        session_id=safe_session,
                        experiment_id=str(experiment_id) if experiment_id else None,
                        cwd=cwd,
                        main_root=main_root,
                        branch=branch,
                        worktree_dir=worktree_dir,
                        log_path=dest,
                    ),
                )
        except OSError as exc:  # noqa: BLE001 - non-fatal
            print(f"save_log: write failed for {dest}: {exc}", file=sys.stderr)

    # Primary write: the main checkout's logs/<tool>/<branch>/. Every
    # session — main or worktree — converges here so the analyzer has
    # one canonical location to scan.
    _write(str(log_root / args.tool / branch), include_metadata=external_root)
    # Secondary write: the worktree's own logs/<tool>/<branch>/ when the
    # session actually started in a worktree. Lets the analyzer's
    # worktree_from_path() bucket the session under the right worktree
    # name. Without this, every worktree session falls back to (main)
    # attribution via the cwd field.
    if not external_root and worktree_dir and os.path.realpath(worktree_dir) != os.path.realpath(main_root):
        if args.archive_stale and not _is_worktree_active(worktree_dir, main_root):
            # Stale worktree: route to a dated archive subdir so the
            # analyzer can ignore it cleanly (the analyzer only walks
            # `logs/<tool>/<branch>/` paths; .archive is a sibling).
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            archive_dir = os.path.join(
                main_root, "logs", ".archive", branch, ts,
            )
            _write(archive_dir)
            print(
                f"save_log: archived stale worktree log to {archive_dir}",
                file=sys.stderr,
            )
        else:
            _write(os.path.join(worktree_dir, "logs", args.tool, branch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
