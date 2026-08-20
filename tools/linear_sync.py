#!/usr/bin/env python3
"""tools/linear_sync.py — Optional Linear auto-sync for Claude Code tasks.

Triggered by `hooks/linear-autosync.sh` on every Edit|Write|MultiEdit
so that work happening in Claude Code is reflected in the user's
Linear workspace without requiring a manual `/dev-kit:linear` call.
Gated by configuration (non-blocking; users without Linear
configured are unaffected).

# Activation

In priority order (first match wins):

1. `LINEAR_API_KEY` env var present  → enabled.
2. `.dev-kit/.enabled.json` has `mcp.linear` ∈ {`auto`, `on`}  → enabled.
3. otherwise  → no-op (exit 0).

When enabled, `LINEAR_TEAM_ID` and `LINEAR_PROJECT_NAME` env vars
override the auto-detected team / project. The project name defaults
to the canonical repository name (per #539: "A repository whose
Linear project name differs from its canonical repository name gets
a project named exactly after the repository.").

# Task context

The current task is derived from `.dev-kit/hand-off/linear.json`.
That file is the resume hint, not the authorization gate (per
#539: "Existing handoff files are hints only; their presence never
proves that the current task is already registered."). A new task
(e.g. branch change, fresh prompt) replaces the handoff before
this script decides whether to create or update an issue.

# Reconciliation contract

For the full reconciliation rules see `skills/linear/SKILL.md`.
This script's reduced contract:

  1. Find or create the project named after the repository.
  2. Search open issues in that project for a scope match.
  3. Create a new issue when no match exists or the match is stale.
  4. Update an existing issue when the scope still matches.
  5. Write `.dev-kit/hand-off/linear.json` with the result.

# Non-blocking contract

Per #539: "Linear failures are non-blocking for implicit workflow
calls." This script never raises. All failures are reported on
stderr and the exit code is always 0, so a flaky network or
misconfigured token never blocks a real edit.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from _repo_name import repo_name as _repo_name


class LinearTransportError(RuntimeError):
    """Raised by _linear_query when the transport layer (DNS, TLS handshake,
    connection refused) fails. Distinct from RuntimeError so callers can
    distinguish transport failure from API/GraphQL errors and surface the
    right diagnostic. Auto-sync round flow catches and bails silently
    (non-blocking per #539); CLI surface flow re-raises to stderr."""


class LinearFreeTierLimitError(RuntimeError):
    """Raised when Linear rejects issue creation due to the free-tier cap."""


def _is_free_tier_limit_message(message: str) -> bool:
    """Return True only for the known free-tier issue-cap failure.

    Linear emits one of a handful of canonical phrasings when the free
    plan's issue-count cap is hit. We accept those exact substrings —
    NOT a loose 3-token AND-match, which would false-positive on any
    future Linear policy / limit-change notice that happens to mention
    "free", "tier", "issue", and "limit" in passing and would archive
    up to 10 active issues on an opt-in workspace.
    """
    normalized = message.lower()
    canonical = (
        "free issue limit",
        "free tier issue limit",
        "issue limit for the free tier",
        "issue limit on the free plan",
        "issue limit reached for the free",
    )
    return any(phrase in normalized for phrase in canonical)


# Linear API endpoint (https://developers.linear.app/docs/graphql/working-with-the-graphql-api).
_LINEAR_API_URL = "https://api.linear.app/graphql"
# 15s absorbs cold DNS/TLS (measured 5.82s on first call vs 3.67s warm on macOS).
# 5s ceiling was hitting on initial network setup; >30s would block the Edit too long
# in a true outage.
_LINEAR_HTTP_TIMEOUT_S = 15
_HANDOFF_DIR = Path(".dev-kit") / "hand-off" / "linear"
_CONFIG_REL = Path(".dev-kit") / "linear-config.json"
_ENV_FILE_REL = Path(".dev-kit") / ".env.linear"
_ENABLED_REL = Path(".dev-kit") / ".enabled.json"
_SKIP_MARKERS = ("/", "#", "!", "?", "ls ", "cat ", "grep ", "git status")
# User-scope env file. XDG-aware: $XDG_CONFIG_HOME/dev-kit/.env falls back to
# $HOME/.config/dev-kit/.env. A single shared file across repos / worktrees.
_USER_ENV_REL = Path("dev-kit") / ".env"
_LINEAR_KEY_PREFIX = "LINEAR_"


def _user_env_path() -> Path:
    """Return the user-scope env file path, honoring XDG_CONFIG_HOME.

    Falls back to $HOME/.config/dev-kit/.env when $XDG_CONFIG_HOME is
    unset/empty. The file lives outside the repo so one key can be shared
    across all of the user's repositories.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / _USER_ENV_REL
    return Path.home() / ".config" / _USER_ENV_REL


def _read_env_file_lines(path: Path) -> list[tuple[str, str]]:
    """Parse a dotenv file into (key, value) pairs. Empty when missing.

    Lines starting with `#` and blanks are skipped. Values may be quoted
    with single or double quotes; trailing `# comment` is stripped. The
    caller decides which keys to accept via its own filter loop.
    """
    if not path.is_file():
        return []
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out.append((key, value))
    return out


def _load_env_file(repo: Path) -> None:
    """Load env-file pairs into os.environ from user-scope + per-worktree files.

    Two sources, in priority order (first loaded wins per key, since the
    file values only fill in keys already missing from os.environ):

    1. **shell env** — untouched, never overwritten by files.
    2. **user-scope** `~/.config/dev-kit/.env` (or
       `$XDG_CONFIG_HOME/dev-kit/.env`) — a single shared file across
       all repos / worktrees. Only `LINEAR_*` keys are injected so
       unrelated app env vars in the same file do not leak into the
       Python process.
    3. **per-worktree** `<repo>/.dev-kit/.env.linear` — Linear-only file
       by convention; all keys pass through (backward compat).

    Both files are untracked (`.dev-kit/` and `.env*` patterns are in
    `.gitignore`). Values may be quoted with single or double quotes;
    trailing `# comment` is stripped.
    """
    # User-scope first. Filter to LINEAR_* only — a generic .env may host
    # other apps' keys (GH_TOKEN, OPENAI_API_KEY, ...) and we must not
    # silently promote them into the Linear subprocess.
    for key, value in _read_env_file_lines(_user_env_path()):
        if key in os.environ:
            continue
        if not key.startswith(_LINEAR_KEY_PREFIX):
            continue
        os.environ[key] = value
    # Per-worktree fallback. No filter — the file is Linear-only by convention.
    for key, value in _read_env_file_lines(repo / _ENV_FILE_REL):
        if key in os.environ:
            continue
        os.environ[key] = value


def _repo_root() -> Path:
    """Return the repository root, falling back to cwd."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return Path(out.decode("utf-8", "ignore").strip() or ".").resolve()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return Path.cwd().resolve()


def _enabled() -> bool:
    """Return True iff Linear auto-sync is configured on for the current worktree.

    Precedence (first match wins):
      1. Per-worktree `.dev-kit/linear-config.json:enabled` — requires
         the API key to also be reachable.
      2. `LINEAR_API_KEY` env var (presence = enabled).
      3. Legacy `.dev-kit/.enabled.json:mcp.linear` in {`auto`, `on`}.

    A worktree config that exists without an `enabled` key falls
    through to (2)/(3) (defensive: partial writes cannot accidentally
    enable sync).

    Under `LINEAR_DEBUG=1`, the activation decision + reason are
    logged to stderr so silent failures (the original "sync doesn’t
    work" symptom) become visible.
    """
    repo = _repo_root()
    debug = os.environ.get("LINEAR_DEBUG", "").strip() in ("1", "true", "yes")

    def _log(decision: str, reason: str, key_state: str) -> None:
        if debug:
            print(
                f"[linear-sync] _enabled()={decision} key={key_state} — {reason}",
                file=sys.stderr,
            )

    # (1) Worktree config wins when it explicitly sets `enabled`.
    cfg = _read_worktree_config(repo)
    if cfg is not None and "enabled" in cfg:
        # Load env AFTER reading the config so a partial .env.linear
        # (LINEAR_API_KEY=) cannot blank the env var the gate needs.
        _load_env_file(repo)
        key = os.environ.get("LINEAR_API_KEY", "").strip()
        if not cfg.get("enabled"):
            _log("False", "linear-config.json:enabled is false", "set" if key else "missing")
            return False
        if not key:
            _log("False", "linear-config.json:enabled=true but LINEAR_API_KEY missing", "missing")
            return False
        _log("True", "linear-config.json:enabled=true with API key", "set")
        return True

    # (2)/(3) Without explicit worktree config, load env and fall through.
    _load_env_file(repo)
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key:
        _log("True", "LINEAR_API_KEY present", "set")
        return True

    enabled_path = repo / _ENABLED_REL
    if not enabled_path.is_file():
        _log("False", "no activation source matched", "missing")
        return False
    try:
        legacy = json.loads(enabled_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _log("False", "legacy .enabled.json unreadable", "missing")
        return False
    mcp = legacy.get("mcp") if isinstance(legacy, dict) else None
    if not isinstance(mcp, dict):
        _log("False", "legacy .enabled.json has no mcp block", "missing")
        return False
    state = str(mcp.get("linear", "off")).lower()
    if state not in ("auto", "on"):
        _log("False", f"legacy .enabled.json:mcp.linear={state!r} (not auto/on)", "missing")
        return False
    _log("False", "legacy .enabled.json says enabled but LINEAR_API_KEY missing", "missing")
    return False


def _read_worktree_config(repo: Path) -> dict[str, Any] | None:
    """Read the per-worktree Linear config at `.dev-kit/linear-config.json`.

    Returns the parsed dict, or `None` if the file is missing / invalid.
    A worktree that has never run `linear on|off|setup` returns None,
    and the sync falls back to env var + legacy `.enabled.json`.
    """
    path = repo / _CONFIG_REL
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_worktree_config(repo: Path, payload: dict[str, Any]) -> Path:
    path = repo / _CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _free_tier_cleanup_enabled(repo: Path) -> bool:
    """Return whether opt-in free-tier cleanup is enabled for this worktree."""
    config = _read_worktree_config(repo) or {}
    return config.get("free_tier_cleanup") is True


def _write_linear_config(repo: Path, **updates: Any) -> Path:
    """Update Linear config while preserving unrelated operator settings."""
    config = _read_worktree_config(repo) or {}
    config.update(updates)
    config.setdefault("enabled", True)
    config.setdefault("project_name", "")
    config.setdefault("team_id", "")
    config["set_at"] = _utc_now_iso()
    return _write_worktree_config(repo, config)


# Per-repo cache for `is_repo_owner`. Keyed on `str(repo.resolve())`
# so a single Python process that handles multiple repos (e.g. a
# shared `tools/save_log.py` invocation, or a future sibling-repo
# helper) does not leak a "yes" for repo A into a "yes" for repo B.
# The cache is process-local; every Edit|Write re-forks Python so the
# lifetime is short in practice. `True` and `False` are both cached
# so a confirmed-owner session does not re-run `gh api user` for
# every keystroke; a key present in the dict — regardless of value —
# is the "we already answered" sentinel.
_OWNER_CACHE: dict[str, bool] = {}


def _resolve_gh_login() -> str | None:
    """Return the GitHub login of the authenticated `gh` user, or None.

    Best-effort. Never raises. Bounded by a 3-second timeout so a slow or
    hung `gh` cannot stall the Edit/Write path. Returns None on any
    failure (gh not installed, not authenticated, network error,
    JSON shape mismatch) so the caller can fall through to "not the
    owner" and stay silent.
    """
    try:
        gh_path = __import__("shutil").which("gh")
    except (OSError, ValueError):
        gh_path = None
    if not gh_path:
        return None
    try:
        out = subprocess.check_output(
            ["gh", "api", "user", "--jq", ".login"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    login = out.decode("utf-8", "ignore").strip()
    return login or None


def _resolve_origin_owner(repo: Path) -> str | None:
    """Return the OWNER segment of the GitHub origin remote, or None.

    Mirrors `lib/ci_setup.detect_owner_repo`'s URL parser but is
    self-contained so this script has no lib/ dependency. SSH and
    HTTPS forms are both handled. Non-GitHub remotes return None so
    the caller treats the user as non-owner.
    """
    try:
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    url = out.decode("utf-8", "ignore").strip()
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    owner = m.group(1).strip()
    return owner or None


def is_repo_owner(repo: Path | None = None) -> bool:
    """Return True iff the current user is the owner of this repository.

    This is the gate that distinguishes "auto-sync triggers fire
    automatically" (owner) from "Linear only updates when the user
    explicitly invokes `/dev-kit:linear`" (everyone else). The
    three new trigger hooks (worktree-create, session-start,
    task-change) all bail when this returns False, so a contributor
    who clones the repo never has their work silently registered in
    the owner's Linear workspace.

    Resolution order (first match wins):

      1. `LINEAR_REPO_OWNER_AUTO_SYNC=1|true` — explicit opt-in,
         bypasses detection (e.g. for forks where gh auth points at
         the contributor, not the upstream).
      2. `LINEAR_REPO_OWNER_AUTO_SYNC=0|false` — explicit opt-out,
         never auto-syncs even if detection would say True.
      3. Detection — `gh api user --jq .login` must match the OWNER
         segment of `git remote get-url origin`. Case-insensitive
         comparison (GitHub logins are case-insensitive).

    Negative results are cached in `_OWNER_CACHE` for the lifetime
    of the process; positive results are cached too. The user can
    re-run after fixing `gh auth` by killing the Python process
    (every Edit|Write re-forks it, so the cache is short-lived in
    practice).

    Never raises. Returns False on any failure (gh missing, no
    remote, non-GitHub remote, timeout) — the safe default is
    "stay silent", never "auto-sync anyway".
    """
    global _OWNER_CACHE
    target = repo or _repo_root()
    cache_key = str(target.resolve())
    if cache_key in _OWNER_CACHE:
        return _OWNER_CACHE[cache_key]

    override = os.environ.get("LINEAR_REPO_OWNER_AUTO_SYNC", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        _OWNER_CACHE[cache_key] = True
        return True
    if override in ("0", "false", "no", "off"):
        _OWNER_CACHE[cache_key] = False
        return False

    login = _resolve_gh_login()
    owner = _resolve_origin_owner(target)
    if not login or not owner:
        _OWNER_CACHE[cache_key] = False
        return False
    _OWNER_CACHE[cache_key] = login.lower() == owner.lower()
    return _OWNER_CACHE[cache_key]


def _project_name_override(repo: Path) -> str:
    """Return the user-set project name override, or empty string.

    Resolution order:
      1. `LINEAR_PROJECT_NAME` env var.
      2. `project_name` field of `.dev-kit/linear-config.json`.
    The repo basename is the final fallback in `sync()`.
    """
    env = os.environ.get("LINEAR_PROJECT_NAME", "").strip()
    if env:
        return env
    cfg = _read_worktree_config(repo)
    if cfg is None:
        return ""
    return str(cfg.get("project_name", "")).strip()


def _team_id_override(repo: Path) -> str:
    env = os.environ.get("LINEAR_TEAM_ID", "").strip()
    if env:
        return env
    cfg = _read_worktree_config(repo)
    if cfg is None:
        return ""
    return str(cfg.get("team_id", "")).strip()


def _notes_override(repo: Path) -> str:
    """Return operator-written notes from `.dev-kit/linear-config.json`.

    The `notes` field is free-form Markdown that the operator can
    write per worktree to capture context that the auto-generated
    body cannot derive (narrative, reasoning, follow-up TODOs,
    anything in Korean the operator wants pinned to the issue).
    """
    env = os.environ.get("LINEAR_NOTES", "").strip()
    if env:
        return env
    cfg = _read_worktree_config(repo)
    if cfg is None:
        return ""
    return str(cfg.get("notes", "")).rstrip()


_WORK_VERBS = (
    "implement", "build", "fix", "refactor", "add", "create",
    "update", "remove", "delete", "ship", "migrate", "wire",
    "integrate", "sync", "register", "track",
)
_WORK_VERB_RE = re.compile(r"\b(?:{})\b".format("|".join(_WORK_VERBS)))


def _has_work_verb(text: str) -> bool:
    """Return True iff ``text`` contains a recognized work verb.

    Shared by ``_should_skip_prompt`` (skip-on-no-verb) and
    ``_resolve_prompt`` (branch-fallback when commit subject lacks
    a work verb on a fresh worktree).
    """
    if not text:
        return False
    return bool(_WORK_VERB_RE.search(text.lower()))


def _should_skip_prompt(prompt: str) -> bool:
    """Filter read-only / non-task prompts (per #539: no Linear for
    inspect / review / security / code-viz unless explicit)."""
    s = prompt.strip().lower()
    if not s:
        return True
    if any(s.startswith(m) for m in _SKIP_MARKERS):
        return True
    # Explicit registration keyword always passes.
    if "/dev-kit:linear" in s or "register in linear" in s:
        return False
    # Heuristic: needs at least one verb that looks like work.
    return not _has_work_verb(s)


def _read_handoff(repo: Path) -> dict[str, Any] | None:
    """Read the per-worktree hand-off record, falling back to the
    legacy single-file layout from #543.

    Migration contract: existing installations have their last
    reconciliation state in `.dev-kit/hand-off/linear.json` (the
    pre-#544 single-file layout). The new layout is
    `.dev-kit/hand-off/linear/<slug>.json` (per-worktree). The
    new file wins when both exist; the legacy file is consulted
    only as a fallback. The next sync round will write the new
    file with the authoritative API result, so the legacy file
    is implicitly migrated — no separate one-shot migration is
    required.
    """
    new_path = _handoff_path(repo)
    if new_path.is_file():
        try:
            return json.loads(new_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    legacy_path = repo / ".dev-kit" / "hand-off" / "linear.json"
    if legacy_path.is_file():
        try:
            return json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _write_handoff(repo: Path, payload: dict[str, Any]) -> None:
    """Write the per-worktree hand-off record.

    Priority model: **Linear API is the source of truth** (priority 1).
    This file is a *cache* (priority 2) — a resume hint to avoid
    round-trips on every Edit|Write. The `_find_issue` query always
    re-validates against the API before reusing an issue, so a stale
    or wrong issue id in this file cannot cause a duplicate or a
    wrong-target update. The next sync round will overwrite whatever
    is here.
    """
    payload_with_meta = {
        "_meta": {
            "priority": 2,
            "kind": "cache",
            "source_of_truth": "linear_api",
            "written_by": "tools/linear_sync.py",
        },
        **payload,
    }
    path = _handoff_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload_with_meta, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _current_branch(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode("utf-8", "ignore").strip() or "(detached)"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "(unknown)"


def _is_main_checkout(repo: Path) -> bool:
    """Return True iff ``repo`` is the main checkout (not a linked worktree).

    Discriminator: ``git rev-parse --git-dir`` returns the main
    checkout's .git/ for the main checkout, and a per-worktree path
    under .git/worktrees/<name>/ for any linked worktree. This is the
    same discriminator used by ``hooks/lib/worktree-detect.sh`` so the
    two rule-paths cannot drift.
    """
    try:
        git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore").strip()
        git_common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if not git_dir or not git_common:
        return False
    return Path(git_dir).resolve() == Path(git_common).resolve()


def _worktree_slug(repo: Path) -> str:
    """Stable, filesystem-safe identifier for the current worktree.

    Main checkout → ``main``. Linked worktree → the trailing path
    segment (e.g. ``fix-linear-autosync-prompt-source``). Falls back
    to a slugified absolute path when neither matches.
    """
    if _is_main_checkout(repo):
        return "main"
    name = repo.name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "worktree"


def _handoff_path(repo: Path) -> Path:
    """Per-worktree handoff path under ``.dev-kit/hand-off/linear/``.

    Each worktree (and the main checkout) gets its own JSON file, so
    two parallel sessions in two worktrees never share or overwrite
    each other's reconciliation state.
    """
    return repo / _HANDOFF_DIR / f"{_worktree_slug(repo)}.json"


def _latest_commit_subject(repo: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode("utf-8", "ignore").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _resolve_prompt(repo: Path) -> str:
    """Return the current task description for the active worktree.

    Priority is deliberately the **opposite** of "handoff-first":

      1. The latest commit subject on the current branch — the most
         recent signal of what the operator is actually working on.
         A fresh task within the same worktree (new commit) updates
         this immediately, so the script does not get stuck on the
         previous task's prompt. Returns immediately when it carries
         a work verb.
      2. The branch name itself (e.g. ``fix/linear-autosync-prompt-source``)
         — fallback when the commit subject lacks a work verb.
         Typical case: a fresh worktree still pointing at origin/main's
         release commit (no work verb in ``chore(release): ...``).
         Without this fallback the auto-sync silently skipped on
         every first Edit|Write of every fresh task branch, which
         read as "Linear auto-update isn't working" — issue #648-era
         regression. The branch name carries the ``<type>/<slug>``
         work-signal (``fix``, ``refactor``, ``feat``-style prefixes
         are not in ``_WORK_VERBS`` but the slug itself often is)
         so it qualifies as a task description.
      3. The active hand-off's ``prompt`` field is **not** used as
         a source. The handoff is a cache for the issue reference,
         not for the task description; trusting it would mean a stale
         prompt from a previous task would keep shadowing the current
         one forever (per the adversarial Codex review).

    Without this ordering the script would either silently bail on a
    fresh session (no commit, no hand-off) or, worse, keep updating
    the previous task's issue after the operator has moved on.
    """
    commit_subject = _latest_commit_subject(repo)
    branch = _current_branch(repo)
    # Priority 1: commit subject with a work verb wins immediately.
    if commit_subject and _has_work_verb(commit_subject):
        return commit_subject
    # Priority 2: branch name carries the task signal on a fresh
    # worktree. Useful even when the commit subject exists (release
    # bump from origin/main) but lacks a work verb.
    if branch and _has_work_verb(branch):
        return branch
    # Priority 3: any commit subject we have, even without a work verb.
    # _should_skip_prompt will gate this against the SKIP_MARKERS
    # list so a release commit on the main checkout still skips.
    if commit_subject:
        return commit_subject
    return branch


def _linear_query(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Execute a Linear GraphQL request and return the `data` payload.

    Returns the `data` payload on success. Raises RuntimeError on
    HTTP/GraphQL errors and on TLS handshake failures (MITM signal).
    Raises LinearTransportError on transport failure (DNS, connection
    refused, socket timeout, etc.) so the auto-sync non-blocking
    contract can catch-and-continue at the `sync()` boundary; CLI
    callers let it propagate to surface a real stderr diagnostic.
    """
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY not set")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        _LINEAR_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_LINEAR_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore") if exc.fp else ""
        if _is_free_tier_limit_message(body):
            raise LinearFreeTierLimitError(body[:300]) from exc
        raise RuntimeError(f"linear http {exc.code}: {body[:300] or exc.reason}") from exc
    except urllib.error.URLError as exc:
        # URLError wraps both DNS failures and SSL errors. SSL errors
        # can be a MITM signal — never silently swallow them. Re-raise
        # as a plain RuntimeError so the auto-sync gate still bails
        # non-blocking; the diagnostic is preserved.
        if isinstance(exc.reason, ssl.SSLError):
            raise RuntimeError(f"linear TLS: {exc.reason}") from exc
        # Other transport failures (DNS, connection refused) — surface
        # as LinearTransportError so callers can distinguish transport
        # from API errors. urllib.request.urlopen always wraps
        # socket.timeout as URLError(reason=socket.timeout(...)) in
        # production, but a raw TimeoutError can still leak through
        # mock-driven test paths.
        raise LinearTransportError(f"linear: {exc}") from exc
    except TimeoutError as exc:
        # Defensive catch for raw TimeoutError that bypasses urllib's
        # URLError wrapping (e.g. when urlopen is replaced with a
        # bare side_effect=TimeoutError in tests, or in unusual
        # transport adapters).
        raise LinearTransportError(f"linear: {exc}") from exc
    if "errors" in payload and payload["errors"]:
        first = payload["errors"][0]
        message = str(first.get("message", "unknown"))
        if _is_free_tier_limit_message(message):
            raise LinearFreeTierLimitError(message)
        raise RuntimeError(f"linear graphql: {message}")
    return payload.get("data") or {}


def _parse_list_args(rest: list[str]) -> dict[str, Any]:
    """Parse `linear list` argv into a filter dict + limit.

    Supported flags (all optional):
      --state=<name>     filter by issue state name (e.g. Backlog, Done)
      --team=<key>       filter by team key (e.g. SHO)
      --project=<name>   filter by project name (defaults to the active repo's
                         project = per-worktree override or repo basename;
                         pass --all-projects to see every project in the team)
      --all-projects     opt out of the default repo-scoping and list every
                         project the team key can see
      --assignee=<id|me|none>  filter by assignee (default: me)
      --limit=<N>        max rows (default 25)

    Unknown flags are silently ignored to keep the CLI forgiving.
    """
    out: dict[str, Any] = {
        "state": None,
        "team": None,
        "project": None,  # resolved in `_cmd_list` to repo default if omitted
        "all_projects": False,  # opt-out of the active-repo default scope
        "assignee": None,  # explicit --assignee=me|none|<id> required to filter
        "limit": 25,
    }
    for arg in rest:
        if arg.startswith("--state="):
            out["state"] = arg.split("=", 1)[1].strip() or None
        elif arg.startswith("--team="):
            out["team"] = arg.split("=", 1)[1].strip() or None
        elif arg.startswith("--project="):
            out["project"] = arg.split("=", 1)[1].strip() or None
        elif arg == "--all-projects":
            out["all_projects"] = True
        elif arg.startswith("--assignee="):
            v = arg.split("=", 1)[1].strip().lower()
            out["assignee"] = None if v in ("", "none", "unassigned") else v
        elif arg.startswith("--limit="):
            try:
                out["limit"] = max(1, min(100, int(arg.split("=", 1)[1])))
            except ValueError:
                pass
    return out


def _list_query(filters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the GraphQL query + variables for the `list` subcommand.

    Filters are joined with AND. Each clause + its variable declaration is
    gated on the filter being non-None, so Linear's GraphQL validator never
    sees an unused `$state` / `$teamKey` / `$assigneeId` variable.
    """
    variables: dict[str, Any] = {"first": filters["limit"]}
    param_decls: list[str] = ["$first: Int!"]
    clauses: list[str] = []
    if filters.get("state"):
        clauses.append("state: { name: { eq: $state } }")
        param_decls.append("$state: String")
        variables["state"] = filters["state"]
    if filters.get("team"):
        clauses.append("team: { key: { eq: $teamKey } }")
        param_decls.append("$teamKey: String")
        variables["teamKey"] = filters["team"]
    if filters.get("project") and not filters.get("all_projects"):
        clauses.append("project: { name: { eq: $projectName } }")
        param_decls.append("$projectName: String")
        variables["projectName"] = filters["project"]
    if filters.get("assignee"):
        clauses.append("assignee: { id: { eq: $assigneeId } }")
        param_decls.append("$assigneeId: String")
        variables["assigneeId"] = filters["assignee"]
    filter_str = f", filter: {{ {', '.join(clauses)} }}" if clauses else ""
    query = (
        "query(" + ", ".join(param_decls) + ") {"
        "  issues(first: $first" + filter_str + ", orderBy: updatedAt) {"
        "    nodes { identifier title state { name } priority updatedAt url project { name } }"
        "  }"
        "}"
    )
    return query, variables


def _format_issue_row(node: dict[str, Any]) -> str:
    """Render one issue as a fixed-width line for grep-ability.

    Columns: IDENT(10) [STATE(12)] pri=N(4) YYYY-MM-DD  TITLE
    """
    ident = str(node.get("identifier") or "")[:10]
    state = str((node.get("state") or {}).get("name") or "")[:12]
    prio = node.get("priority")
    prio_s = "-" if prio is None else str(prio)
    updated = str(node.get("updatedAt") or "")[:10]
    title = str(node.get("title") or "")
    project = str(((node.get("project") or {}).get("name") or "")).strip()
    if project:
        return f"{ident:10} [{state:12}] pri={prio_s:<4} {updated}  [{project}] {title}"
    return f"{ident:10} [{state:12}] pri={prio_s:<4} {updated}  {title}"


def _resolve_assignee_me() -> str | None:
    """Return the current viewer's Linear user id (for --assignee=me), or None."""
    try:
        data = _linear_query("query { viewer { id } }", {})
    except RuntimeError:
        return None
    viewer = data.get("viewer") or {}
    return str(viewer["id"]) if viewer.get("id") else None


def _cmd_list(rest: list[str]) -> int:
    """CLI `linear list` — non-blocking print of recent issues.

    Always exits 0; transport / GraphQL failures are reported on stderr
    so the command is safe to embed in shell pipelines and CI.
    """
    filters = _parse_list_args(rest)
    if not filters.get("project") and not filters.get("all_projects"):
        repo = _repo_root()
        filters["project"] = _project_name_override(repo) or _repo_name(repo)
        if filters.get("project"):
            print(
                f"linear: list: scoped to project '{filters['project']}' (pass --all-projects to disable)",
                file=sys.stderr,
            )
    try:
        if filters.get("assignee") == "me":
            me = _resolve_assignee_me()
            if me is None:
                print("linear: list: cannot resolve current viewer id", file=sys.stderr)
                return 0
            filters["assignee"] = me
        query, variables = _list_query(filters)
        data = _linear_query(query, variables)
        nodes = ((data.get("issues") or {}).get("nodes")) or []
        if not nodes:
            print("linear: list: no issues match", file=sys.stderr)
            return 0
        for n in nodes:
            print(_format_issue_row(n))
        return 0
    except RuntimeError as exc:
        print(f"linear: list: {exc}", file=sys.stderr)
        return 0


def _scope_key(prompt: str, branch: str) -> str:
    """Hash-free scope key for matching an issue to a task.

    Two prompts that share the same scope key map to the same issue.
    Key = `<branch>:: <first 12 words of the prompt, lowercased, alpha-num only>`.
    """
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", prompt.lower()).strip()
    head = " ".join(cleaned.split()[:12])
    return f"{branch}::{head}"


def _resolve_team_id() -> str:
    """Return the team id to use, auto-detecting the first available
    team when the user has not pinned one via env or worktree config.
    Caches the lookup in a module-level variable so subsequent calls
    in the same `sync()` invocation don't re-query."""
    global _TEAM_ID_CACHE
    if _TEAM_ID_CACHE is not None:
        return _TEAM_ID_CACHE
    repo = _repo_root()
    cached = _team_id_override(repo)
    if cached:
        _TEAM_ID_CACHE = cached
        return cached
    data = _linear_query(
        "query { viewer { teams { nodes { id name key } } } }",
        {},
    )
    teams = ((data.get("viewer") or {}).get("teams") or {}).get("nodes") or []
    if not teams:
        raise RuntimeError("linear: no teams visible to this API key")
    _TEAM_ID_CACHE = str(teams[0]["id"])
    return _TEAM_ID_CACHE


_TEAM_ID_CACHE: str | None = None


def _find_or_create_project(repo: Path, team_id: str | None) -> str:
    """Return the project id, creating it if needed."""
    project_name = _project_name_override(repo) or _repo_name(repo)
    query = (
        "query($name: String!) {"
        "  projects(filter: { name: { eq: $name } }, first: 1) {"
        "    nodes { id name }"
        "  }"
        "}"
    )
    data = _linear_query(query, {"name": project_name})
    nodes = (data.get("projects") or {}).get("nodes") or []
    if nodes:
        return str(nodes[0]["id"])
    resolved_team = team_id or _resolve_team_id()
    mutation = (
        "mutation($name: String!, $teamId: String!) {"
        "  projectCreate(input: { name: $name, teamIds: [$teamId] }) {"
        "    project { id }"
        "  }"
        "}"
    )
    data = _linear_query(mutation, {"name": project_name, "teamId": resolved_team})
    return str(data["projectCreate"]["project"]["id"])


def _utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with `Z` suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _extract_issue_id(issue_ref: str) -> str:
    """Strip the `SHO-123 (uuid)` wrapper to its bare uuid.

    `issue_ref` may be either a bare uuid, `SHO-123 (uuid)`, or the
    same wrapped without a space. Centralizing the unwrap keeps
    `_set_issue_state` / `_archive_issue` / `_update_issue` in sync.
    """
    s = issue_ref.strip()
    if "(" in s and s.endswith(")"):
        return s[s.rfind("(") + 1:-1].strip()
    return s


def _is_completion_signal(prompt: str) -> bool:
    """Return True iff the prompt declares completion of the task.

    A prompt qualifies when it contains a completion verb
    (`done`, `finished`, `complete[d]?`, `shipped`, `merged`, `closed`).
    Work-verb-wins was tried and dropped: phrases like "done with
    the auth refactor" describe a completed task (where "refactor"
    is a noun, not new work), and false-negatives were far more
    common than false-positives in practice.
    """
    s = prompt.strip().lower()
    if not s:
        return False
    completion = ("done", "finished", "completed", "complete",
                  "shipped", "merged", "closed")
    return any(re.search(rf"\b{v}\b", s) for v in completion)


def _is_work_signal(prompt: str) -> bool:
    """Return True iff the prompt looks like work is starting/continuing.

    Subset of the work-verb list — `fix`, `update`, `remove`, `delete`
    are intentionally excluded because they often apply to cleanup
    after completion (e.g. "fix the leftover test") rather than new
    starting work.
    """
    s = prompt.strip().lower()
    if not s:
        return False
    starters = ("implement", "build", "wire", "integrate",
                "start", "sync", "register", "track", "add", "create")
    return any(re.search(rf"\b{v}\b", s) for v in starters)


def _state_id(state_name: str) -> str | None:
    """Resolve a workflow-state name (e.g. 'Todo', 'In Progress',
    'Done') to its uuid for the configured team.

    Mirrors `tools/linear_pr_sync.py::_state_id`. Returns None when
    the team has no column with that name (custom workflows).
    """
    team_id = _resolve_team_id()
    if not team_id:
        return None
    query = (
        "query($teamId: ID!) {"
        "  workflowStates(filter: { team: { id: { eq: $teamId } } }, first: 100) {"
        "    nodes { id name }"
        "  }"
        "}"
    )
    try:
        data = _linear_query(query, {"teamId": team_id})
    except RuntimeError:
        return None
    target = state_name.strip().lower()
    for node in ((data.get("workflowStates") or {}).get("nodes") or []):
        if str(node.get("name") or "").strip().lower() == target:
            return str(node["id"])
    return None


def _set_issue_state(issue_ref: str, state_name: str) -> bool:
    """Transition an issue to the named state. Returns True on success
    or when the issue is already in the target state.

    Never raises (non-blocking per #539). Returns False on transport
    failure or unknown state name.
    """
    issue_id = _extract_issue_id(issue_ref)
    state_id = _state_id(state_name)
    if not state_id:
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"[linear-sync] unknown state: {state_name!r}", file=sys.stderr)
        return False
    mutation = (
        "mutation($issueId: String!, $stateId: String!) {"
        "  issueUpdate(id: $issueId, input: { stateId: $stateId }) {"
        "    success issue { id identifier state { name } }"
        "  }"
        "}"
    )
    try:
        data = _linear_query(mutation, {"issueId": issue_id, "stateId": state_id})
    except RuntimeError:
        return False
    success = bool((data.get("issueUpdate") or {}).get("success"))
    if not success and os.environ.get("LINEAR_DEBUG", "").strip() == "1":
        print(f"[linear-sync] set_state({state_name}) failed for {issue_id}", file=sys.stderr)
    return success


def _find_all_issues(project_id: str, scope_key: str) -> list[dict]:
    """Return every open issue whose description starts with `scope_key`,
    sorted newest-first by `updatedAt`.

    Duplicates happen when the same scope resolves in two separate
    auto-sync rounds (e.g. `linear off` + `linear on` cycles). The
    caller archives older matches and keeps the newest.
    """
    query = (
        "query($projectId: ID!) {"
        "  issues(filter: { project: { id: { eq: $projectId } } }, first: 50) {"
        "    nodes { id identifier description updatedAt state { name } }"
        "  }"
        "}"
    )
    try:
        data = _linear_query(query, {"projectId": project_id})
    except RuntimeError:
        return []
    nodes = (data.get("issues") or {}).get("nodes") or []
    TERMINAL = ("Done", "Canceled")
    matches = [
        n for n in nodes
        if str(n.get("description") or "").startswith(f"<!-- scope:{scope_key} -->")
        and str((n.get("state") or {}).get("name") or "") not in TERMINAL
    ]
    matches.sort(key=lambda n: str(n.get("updatedAt") or ""), reverse=True)
    return matches


def _archive_issue(issue_ref: str) -> bool:
    """Archive (Linear's soft-delete) an issue. Returns True on success.

    Archive is preferred over `issueDelete`:
      - reversible (Linear restores archived issues from the trash)
      - idempotent (re-archiving returns success)

    Used only by the auto-sync dedupe path. Never call from a
    user-facing CLI to remove issues the operator explicitly created.
    """
    issue_id = _extract_issue_id(issue_ref)
    mutation = (
        "mutation($id: String!) {"
        "  issueArchive(id: $id) {"
        "    success entity { id identifier archivedAt }"
        "  }"
        "}"
    )
    try:
        data = _linear_query(mutation, {"id": issue_id})
    except RuntimeError:
        return False
    success = bool((data.get("issueArchive") or {}).get("success"))
    if not success and os.environ.get("LINEAR_DEBUG", "").strip() == "1":
        print(f"[linear-sync] archive failed for {issue_id}", file=sys.stderr)
    return success


def _oldest_cleanup_candidates(project_id: str, limit: int = 10) -> list[dict]:
    """Return oldest non-terminal issues eligible for free-tier cleanup.

    ``orderBy: createdAt`` sorts ascending on the server side so the
    "oldest N" guarantee holds even when the project has more than 100
    non-terminal issues. Without it, Linear returns the default
    ``updatedAt``-desc page and the "10 oldest" silently degrades to
    "10 oldest of an arbitrary 100-row page".
    """
    query = (
        "query($projectId: ID!) {"
        "  issues(filter: { project: { id: { eq: $projectId } } },"
        "         first: 100, orderBy: createdAt) {"
        "    nodes { id identifier createdAt state { name } }"
        "  }"
        "}"
    )
    data = _linear_query(query, {"projectId": project_id})
    terminal = {"Done", "Canceled"}
    candidates = [
        node for node in ((data.get("issues") or {}).get("nodes") or [])
        if str((node.get("state") or {}).get("name") or "") not in terminal
    ]
    candidates.sort(key=lambda node: str(node.get("createdAt") or ""))
    return candidates[:max(0, limit)]


def _cleanup_free_tier_issues(project_id: str, limit: int = 10) -> tuple[int, str | None]:
    """Archive up to ``limit`` oldest eligible issues.

    Returns ``(archived, error_message)``. ``error_message`` is None
    on the happy path and a short diagnostic string when an exception
    was swallowed so the retry wrapper can surface a user-visible
    warning. The exception itself stays swallowed — cleanup must never
    block the retry path that already raised ``LinearFreeTierLimitError``
    — but the signal is now distinct from "nothing to clean up".
    """
    archived = 0
    error: str | None = None
    try:
        candidates = _oldest_cleanup_candidates(project_id, limit)
        for issue in candidates:
            if _archive_issue(str(issue["id"])):
                archived += 1
    except Exception as exc:  # noqa: BLE001 - cleanup must not block retry path
        error = f"{type(exc).__name__}: {exc}"
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"[linear-sync] free-tier cleanup failed: {error}", file=sys.stderr)
    return archived, error


def _find_issue(project_id: str, scope_key: str) -> str | None:
    """Return the issue id (newest match) whose description starts
    with `scope_key`, or None when no open issue matches.

    Kept for backward compatibility — the dedupe-aware auto-sync
    calls `_find_all_issues` directly. This delegates to it.
    """
    matches = _find_all_issues(project_id, scope_key)
    return str(matches[0]["id"]) if matches else None


def _create_issue(project_id: str, team_id: str, title: str, body: str, scope_key: str,
                  state_id: str | None = None) -> tuple[str, str | None]:
    # Linear’s `IssueCreateInput.{teamId, projectId}` are both `String`
    # (NOT `ID!` as in the `issues(filter:)` input). Match the schema.
    full_body = f"<!-- scope:{scope_key} -->\n{body}"
    if state_id:
        mutation = (
            "mutation($teamId: String!, $projectId: String, $title: String!, $body: String!, $stateId: String) {"
            "  issueCreate(input: {"
            "    teamId: $teamId"
            "    projectId: $projectId"
            "    title: $title"
            "    description: $body"
            "    stateId: $stateId"
            "  }) { issue { id identifier state { name } } }"
            "}"
        )
        data = _linear_query(mutation, {
            "teamId": team_id,
            "projectId": project_id,
            "title": title,
            "body": full_body,
            "stateId": state_id,
        })
    else:
        mutation = (
            "mutation($teamId: String!, $projectId: String, $title: String!, $body: String!) {"
            "  issueCreate(input: {"
            "    teamId: $teamId"
            "    projectId: $projectId"
            "    title: $title"
            "    description: $body"
            "  }) { issue { id identifier } }"
            "}"
        )
        data = _linear_query(mutation, {
            "teamId": team_id,
            "projectId": project_id,
            "title": title,
            "body": full_body,
        })
    issue = data["issueCreate"]["issue"]
    issue_ref = f"{issue['identifier']} ({issue['id']})"
    state_name = ((issue.get("state") or {}).get("name") if isinstance(issue.get("state"), dict) else None)
    return issue_ref, state_name


def _create_issue_with_free_tier_retry(
    repo: Path, project_id: str, team_id: str, title: str, body: str,
    scope_key: str, state_id: str | None = None,
) -> tuple[str, str | None]:
    """Create once, optionally archive ten old issues, then retry once."""
    try:
        return _create_issue(project_id, team_id, title, body, scope_key, state_id)
    except LinearFreeTierLimitError:
        if not _free_tier_cleanup_enabled(repo):
            raise
        archived, cleanup_error = _cleanup_free_tier_issues(project_id, limit=10)
        if cleanup_error is not None:
            print(
                f"[linear-sync] WARNING: free-tier cleanup raised {cleanup_error}; "
                f"archived {archived} before failing. Retrying anyway.",
                file=sys.stderr,
            )
        elif os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"[linear-sync] free-tier cleanup archived {archived} issue(s)", file=sys.stderr)
        return _create_issue(project_id, team_id, title, body, scope_key, state_id)


def _update_issue(issue_ref: str, body: str, project_id: str | None = None,
                  state_id: str | None = None) -> None:
    """Update the issue’s description (and optionally its project + state).

    `IssueUpdateInput` is `String!`-typed for the issue id and the
    project id, just like `IssueCreateInput`. Keep these in sync.
    `state_id` is used by the auto-In-progress + auto-Done paths.
    """
    issue_id = _extract_issue_id(issue_ref)
    fields = ["description: $body"]
    payload: dict[str, Any] = {"id": issue_id, "body": body}
    if project_id is not None:
        fields.append("projectId: $projectId")
        payload["projectId"] = project_id
    if state_id is not None:
        fields.append("stateId: $stateId")
        payload["stateId"] = state_id
    input_fields = ", ".join(fields)
    var_decl = "$id: String!, $body: String!"
    if project_id is not None:
        var_decl += ", $projectId: String"
    if state_id is not None:
        var_decl += ", $stateId: String"
    mutation = (
        f"mutation({var_decl}) {{"
        f"  issueUpdate(id: $id, input: {{ {input_fields} }}) {{"
        f"    issue {{ id identifier state {{ name }} }}"
        f"  }}"
        f"}}"
    )
    _linear_query(mutation, payload)


def _summarize_prompt(prompt: str) -> str:
    s = re.sub(r"\s+", " ", prompt.strip())
    return s[:160] + ("…" if len(s) > 160 else "")


def _last_commit_info(repo: Path) -> dict[str, str]:
    """Return short hash + subject + author + relative date for HEAD."""
    info = {"sha": "", "short": "", "subject": "", "author": "", "date": ""}
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%H%n%h%n%s%n%an%n%ar"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return info
    parts = out.split("\n", 4)
    if len(parts) >= 5:
        info["sha"], info["short"], info["subject"], info["author"], info["date"] = parts
    return info


def _changed_files_since(repo: Path, base: str = "origin/main") -> list[tuple[str, int, int]]:
    """Return ``[(path, added, removed), ...]`` for files changed
    since ``base``. Falls back to the empty list if `base` is missing
    or `git` is unavailable. The list is capped at 20 files to keep
    the Linear description under the 64 KiB limit."""
    try:
        out = subprocess.check_output(
            ["git", "dif", "--numstat", f"{base}...HEAD"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    rows: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        try:
            added, removed, path = line.split("\t", 2)
        except ValueError:
            continue
        try:
            rows.append((path, int(added or "0"), int(removed or "0")))
        except ValueError:
            rows.append((path, 0, 0))
        if len(rows) >= 20:
            break
    return rows


def _extract_acceptance_criteria(prompt: str, commit_body: str) -> list[str]:
    """Pull `- [ ] …` items out of the commit body or the prompt.

    Order of preference:
      1. Lines from the commit body that look like checkboxes.
      2. Same pattern from the user prompt.
    Each line is returned trimmed; the leading `- [ ] ` is stripped.
    """
    pat = re.compile(r"^[\s>*\-+]*\[[ xX]\]\s+(.+)$")
    found: list[str] = []
    for source in (commit_body, prompt):
        for raw in source.splitlines():
            m = pat.match(raw.strip())
            if m:
                item = m.group(1).strip()
                if item and item not in found:
                    found.append(item)
    return found[:10]


def _commit_body(repo: Path) -> str:
    """Return the body of the latest commit (subject line excluded)."""
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%b"],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "ignore").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return out


def _detect_pr(repo: Path) -> dict[str, str] | None:
    """Auto-detect the GitHub PR for the current branch via `gh`.

    Returns a dict with `url`, `number`, `title`, `state` when a PR
    exists, or `None` when `gh` is missing, the user is not
    authenticated, or there is no PR for the branch. The function
    fails closed: any exception becomes a no-op so auto-sync never
    blocks on a missing CLI.
    """
    try:
        out = subprocess.check_output(
            [
                "gh", "pr", "view", "--json",
                "url,number,title,state,isDraft",
            ],
            cwd=str(repo), stderr=subprocess.DEVNULL, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not data.get("url"):
        return None
    return {
        "url": str(data.get("url", "")),
        "number": str(data.get("number", "")),
        "title": str(data.get("title", "")),
        "state": str(data.get("state", "")),
        "draft": "true" if data.get("isDraft") else "false",
    }


def _build_issue_body(*, prompt: str, branch: str, repo: Path, scope: str) -> str:
    """Build a structured Markdown body for the Linear issue.

    Sections, in order: Summary, Context, Files changed, Acceptance
    criteria, Test plan, Related. The scope marker is the first
    line so `_find_issue()` can detect reuse by prefix match.
    """
    summary = _summarize_prompt(prompt)
    commit = _last_commit_info(repo)
    files = _changed_files_since(repo)
    criteria = _extract_acceptance_criteria(_commit_body(repo), prompt)
    pr = _detect_pr(repo)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    sections: list[str] = [f"<!-- scope:{scope} -->"]

    sections.append("## Summary")
    sections.append(summary)

    sections.append("## Context")
    sections.append(f"- **Branch:** `{branch}`")
    # Use the slug (e.g. `fix-linear-autosync-prompt-source`) rather
    # than the absolute worktree path. The absolute path leaks the
    # developer's home directory and filesystem layout to every
    # Linear viewer — non-sensitive identifier is sufficient.
    sections.append(f"- **Worktree slug:** `{_worktree_slug(repo)}`")
    if commit["short"]:
        sections.append(
            f"- **Last commit:** `{commit['short']}` — {commit['subject']}"
        )
        if commit["author"] or commit["date"]:
            sections.append(
                f"  - author: {commit['author']} · {commit['date']}"
            )
    sections.append(f"- **Auto-synced at:** {timestamp}")

    if files:
        sections.append("## Files changed")
        sections.append("| path | + | - |")
        sections.append("|---|---:|---:|")
        for path, added, removed in files:
            sections.append(f"| `{path}` | {added} | {removed} |")

    if criteria:
        sections.append("## Acceptance criteria")
        for item in criteria:
            sections.append(f"- [ ] {item}")

    sections.append("## Test plan")
    sections.append(
        "_Updated automatically by `tools/linear_sync.py`. "
        "Run the suite and paste the exit code + test count here._"
    )

    notes = _notes_override(repo)
    if notes:
        sections.append("## Notes")
        sections.append(notes)

    sections.append("## Related")
    sections.append("- Branch: `" + branch + "`")
    if commit["sha"]:
        sections.append(f"- Commit: `{commit['sha']}`")
    if pr and pr.get("url"):
        state_badge = ""
        if pr["state"].lower() == "merged":
            state_badge = " (merged)"
        elif pr["state"].lower() == "closed":
            state_badge = " (closed)"
        elif pr["draft"] == "true":
            state_badge = " (draft)"
        elif pr["state"]:
            state_badge = f" ({pr['state'].lower()})"
        sections.append(f"- PR: [#{pr['number']}{state_badge}]({pr['url']}) — {pr['title']}")

    sections.append(f"_Last updated: {timestamp}_")
    return "\n\n".join(sections) + "\n"


def auto_sync() -> int:
    """Auto-sync entry point used by every hook.

    Wraps `sync()` with the **repo-owner gate**: the hook-driven
    auto-sync path only fires for the repository owner. The manual
    CLI path (`/dev-kit:linear` → `sync()`) is intentionally
    ungated, so a contributor who has configured Linear can still
    register work explicitly.

    Why a separate entry point instead of a flag in `sync()`:
    keeps the gate decision out of the CLI surface, so a future
    `sync()` change cannot accidentally weaken the owner-only
    contract. The hook scripts (`hooks/linear-autosync.sh`,
    `hooks/linear-session-start.sh`,
    `hooks/linear-worktree-create.sh`,
    `hooks/linear-task-change.sh`) all call
    `python3 tools/linear_sync.py auto-sync`; the CLI never does.

    Always returns 0 (non-blocking). On a non-owner, the gate bails
    silently — no stderr noise, no extra Linear round-trip, the
    hook just exits 0 and the user's edit proceeds.
    """
    repo = _repo_root()
    if not is_repo_owner(repo):
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(
                f"[linear-sync] auto_sync: skipped (non-owner, repo={_repo_name(repo)})",
                file=sys.stderr,
            )
        return 0
    return sync()


def _last_handoff_scope(repo: Path) -> str | None:
    """Return the scope-key recorded in the handoff, or None.

    Used by `task_change_sync` to detect mid-session scope shifts
    (a new commit, a fresh prompt, a branch change) without
    round-tripping to Linear on every prompt. Returns `None` when:

      - the handoff is missing (no prior sync round),
      - the handoff is unreadable (corrupt JSON),
      - the handoff carries no `scope` key (brand-new handoff
        after a stale-handoff replacement), or
      - the `scope` value is the empty string (folded by the
        `scope or None` short-circuit; the empty-vs-`None`
        distinction is invisible to callers, which is the point —
        both mean "treat as a fresh sync").

    The empty-string case is intentionally collapsed to `None`:
    a `None` last-scope combined with any non-`None` current-scope
    is the "scope changed" trigger, and treating both "no record"
    and "empty record" as `None` keeps the trigger logic to a
    single `last != current` comparison.
    """
    handoff = _read_handoff(repo) or {}
    scope = handoff.get("scope")
    if not isinstance(scope, str):
        return None
    return scope or None


def _current_scope(repo: Path) -> str:
    """Return the scope-key that *would* be used if sync() ran now.

    Mirrors the prompt + branch resolution in `sync()` so the
    `task_change_sync` check stays consistent with the eventual
    sync round. Branch is read directly from git (cheap) and the
    prompt from the latest commit subject, falling back to the
    branch name when no commit exists yet on the branch.
    """
    prompt = _resolve_prompt(repo)
    branch = _current_branch(repo)
    return _scope_key(prompt, branch)


def task_change_sync() -> int:
    """Hook entry point for UserPromptSubmit (plan/task change).

    Compares the current scope (branch + latest commit subject)
    against the handoff's last-recorded scope. When they differ,
    delegates to `auto_sync` (which re-validates against the
    Linear API and creates / updates the matching issue). When
    they match, returns 0 without forking a Linear round-trip —
    the goal is "sync on change", not "sync on every prompt".

    The handoff scope is the only signal we trust: the handoff
    was written by a previous sync round and reflects the scope
    that was last registered. A new prompt with the same scope
    is a continuation, not a change.

    Always returns 0 (non-blocking). On a non-owner, the gate
    inside `auto_sync` bails silently — same contract as
    `auto_sync`.
    """
    repo = _repo_root()
    if not is_repo_owner(repo):
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(
                f"[linear-sync] task_change_sync: skipped (non-owner, repo={_repo_name(repo)})",
                file=sys.stderr,
            )
        return 0
    current = _current_scope(repo)
    last = _last_handoff_scope(repo)
    if last == current:
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(
                f"[linear-sync] task_change_sync: scope unchanged ({current!r})",
                file=sys.stderr,
            )
        return 0
    if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
        print(
            f"[linear-sync] task_change_sync: scope changed "
            f"last={last!r} current={current!r}",
            file=sys.stderr,
        )
    return auto_sync()


def sync() -> int:
    """Entry point. Returns 0 always (non-blocking contract).

    Per-round flow (each step may be a no-op; failures never raise):

      1. Activation gate — bail when not configured.
      2. Resolve prompt, branch, scope. Skip read-only prompts.
      3. Resolve project (find-or-create). Bail if no team.
      4. **Auto-archive duplicates** — if >1 open issue share the
         same scope-marker, archive the older ones and keep the
         newest. Source-of-truth is the Linear API.
      5. **Auto-Done** — if the prompt contains a completion verb
         AND an issue already exists, transition it to Done and
         exit without creating a new issue.
      6. **Auto-open** — create the issue in the team’s `Todo`
         state (falling back to `Backlog` when no Todo column).
      7. **Auto-In-progress** — on subsequent edits (work signal),
         transition the existing issue to `In Progress` (idempotent).
      8. Update the handoff cache.
    """
    if not _enabled():
        return 0
    repo = _repo_root()
    prompt = _resolve_prompt(repo)
    if not prompt or _should_skip_prompt(prompt):
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"linear_sync: skipped (prompt={prompt!r})", file=sys.stderr)
        return 0
    branch = _current_branch(repo)
    scope = _scope_key(prompt, branch)
    handoff = _read_handoff(repo) or {}
    try:
        team_id = _team_id_override(repo) or None
        project_id = _find_or_create_project(repo, team_id)
        summary = _summarize_prompt(prompt)
        body = _build_issue_body(prompt=prompt, branch=branch, repo=repo, scope=scope)

        # Step 4 — dedupe. Keep the newest match; archive the rest.
        matches = _find_all_issues(project_id, scope)
        existing_issue_ref: str | None = None
        if len(matches) > 1:
            keep = matches[0]
            archived_ids: list[str] = []
            for dup in matches[1:]:
                if _archive_issue(str(dup["id"])):
                    archived_ids.append(str(dup["id"]))
            if archived_ids and os.environ.get("LINEAR_DEBUG", "").strip() == "1":
                print(
                    f"[linear-sync] archived {len(archived_ids)} duplicate(s) for "
                    f"scope={scope}: {', '.join(archived_ids)}",
                    file=sys.stderr,
                )
            existing_issue_ref = f"{keep['identifier']} ({keep['id']})"
        elif len(matches) == 1:
            existing_issue_ref = f"{matches[0]['identifier']} ({matches[0]['id']})"

        # Resolve the API-fetched state once — it's the source of truth
        # for both the auto-Done state guard and the auto-In-progress
        # transition. The handoff cache can be stale or missing.
        if matches:
            api_state_name = str((matches[0].get("state") or {}).get("name") or "")

                # Step 5 — auto-Done. Completion verb + existing issue -> Done.
        # State guard: if the issue is already in a terminal state
        # (Done / Canceled), do NOT resurrect it — the user may have
        # moved it manually in the Linear UI.
        if existing_issue_ref and _is_completion_signal(prompt):
            if api_state_name in ("Done", "Canceled"):
                current_state = api_state_name
                _write_handoff(repo, {
                    **(handoff if isinstance(handoff, dict) else {}),
                    "issue": existing_issue_ref,
                    "project": _project_name_override(repo) or _repo_name(repo),
                    "branch": branch,
                    "prompt": prompt,
                    "scope": scope,
                    "action": "noop_terminal",
                    "state": current_state,
                })
                return 0
            if _set_issue_state(existing_issue_ref, "Done"):
                _write_handoff(repo, {
                    **(handoff if isinstance(handoff, dict) else {}),
                    "issue": existing_issue_ref,
                    "project": _project_name_override(repo) or _repo_name(repo),
                    "branch": branch,
                    "prompt": prompt,
                    "scope": scope,
                    "action": "completed",
                    "state": "Done",
                    "completed_at": _utc_now_iso(),
                })
                if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
                    print(f"[linear-sync] done {existing_issue_ref} (scope={scope})", file=sys.stderr)
            return 0

        current_state: str | None = None
        if existing_issue_ref:
            # Step 7 — auto-In-progress on subsequent work signals.
            # Use the API-fetched state (set above, source of truth).
            current_state = api_state_name
            if _is_work_signal(prompt) and current_state != "In Progress":
                if _set_issue_state(existing_issue_ref, "In Progress"):
                    current_state = "In Progress"
                    if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
                        print(f"[linear-sync] in_progress {existing_issue_ref}", file=sys.stderr)
            _update_issue(existing_issue_ref, body, state_id=None)
            issue_ref = existing_issue_ref
            action = "updated"
        else:
            # Step 6 — auto-open. Land in Todo, falling back to Backlog.
            title = f"[{branch}] {summary}"[:250]
            resolved_team = team_id or _resolve_team_id()
            target_state_id = _state_id("Todo") or _state_id("Backlog")
            issue_ref, returned_state = _create_issue_with_free_tier_retry(
                repo, project_id, resolved_team, title, body, scope,
                state_id=target_state_id,
            )
            action = "created"
            # Use the actual returned state from the API so the handoff
            # reflects the truth (Backlog fallback no longer records
            # a misleading "Todo" label).
            current_state = returned_state or ("Todo" if target_state_id else None)

        handoff_payload: dict[str, Any] = {
            "issue": issue_ref,
            "project": _project_name_override(repo) or _repo_name(repo),
            "branch": branch,
            "prompt": prompt,
            "scope": scope,
            "action": action,
            "timestamp": _utc_now_iso(),
        }
        if current_state:
            handoff_payload["state"] = current_state
        if action == "created":
            handoff_payload["created_at"] = _utc_now_iso()
        _write_handoff(repo, handoff_payload)
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"linear_sync: {action} {issue_ref} (scope={scope}, state={current_state})", file=sys.stderr)
    except LinearTransportError as exc:
        # Transport-layer failure (DNS, connection refused, socket timeout).
        # Surface only under LINEAR_DEBUG=1 — otherwise stay silent to honor
        # the non-blocking contract from #539: "Linear failures are non-blocking
        # for implicit workflow calls." The Edit must not be blocked by a
        # flaky first request.
        if os.environ.get("LINEAR_DEBUG", "").strip() == "1":
            print(f"[linear-sync] transport: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — non-blocking per #539 design.
        print(f"linear_sync: skipped ({exc.__class__.__name__}: {exc})", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Subcommands: setup|on|off|project-name|status|list|sync.

    Default (no args, or `sync`) runs the auto-sync once and returns
    its exit code. All other subcommands manipulate the per-worktree
    `.dev-kit/linear-config.json` and exit 0 on success.
    """
    if argv is None:
        argv = sys.argv[1:]
    repo = _repo_root()
    _load_env_file(repo)
    if not argv or argv[0] == "sync":
        return sync()
    if argv[0] == "auto-sync":
        return auto_sync()
    if argv[0] == "task-change-sync":
        return task_change_sync()
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        cfg = _read_worktree_config(repo)
        env_key = bool(os.environ.get("LINEAR_API_KEY", "").strip())
        env_file = (repo / _ENV_FILE_REL).is_file()
        user_env = _user_env_path()
        project = _project_name_override(repo) or _repo_name(repo)
        team = _team_id_override(repo)
        print(json.dumps({
            "worktree": str(repo),
            "slug": _worktree_slug(repo),
            "config": cfg,
            "linear_api_key_set": env_key,
            "user_env_path": str(user_env),
            "user_env_present": user_env.is_file(),
            "env_file_present": env_file,
            "resolved_project": project,
            "resolved_team_id": team or None,
        }, indent=2, sort_keys=True))
        return 0
    if cmd == "list":
        return _cmd_list(rest)
    if cmd == "on":
        path = _write_linear_config(repo, enabled=True)
        print(f"linear: on (worktree={_worktree_slug(repo)} config={path})")
        return 0
    if cmd == "off":
        path = _write_linear_config(repo, enabled=False)
        print(f"linear: off (worktree={_worktree_slug(repo)} config={path})")
        return 0
    if cmd == "project-name":
        if not rest:
            current = _project_name_override(repo) or _repo_name(repo)
            print(f"linear: project-name={current} (set with: linear project-name <name>)")
            return 0
        name = " ".join(rest).strip()
        path = _write_linear_config(repo, project_name=name)
        print(f"linear: project-name={name} (config={path})")
        return 0
    if cmd == "free-tier-cleanup":
        value = rest[0].strip().lower() if rest else "status"
        if value not in {"on", "off", "status"}:
            print("linear: free-tier-cleanup expects on|off|status")
            return 2
        enabled = _free_tier_cleanup_enabled(repo)
        if value == "status":
            print(f"linear: free-tier-cleanup={'on' if enabled else 'off'}")
            return 0
        path = _write_linear_config(repo, free_tier_cleanup=value == "on")
        print(f"linear: free-tier-cleanup={'on' if value == 'on' else 'off'} (config={path})")
        return 0
    if cmd == "setup":
        # Print the recommended setup steps. The script never reads or
        # writes the API key itself — that stays in the env.
        print("linear: setup checklist")
        print("  Option A — shell env (recommended for shared machines):")
        print("    1. export LINEAR_API_KEY=<your-token>   # required, env-only")
        print(f"    2. cd {repo}")
        print("    3. python3 tools/linear_sync.py on")
        print("    4. python3 tools/linear_sync.py project-name <name>   # optional")
        print("    5. python3 tools/linear_sync.py free-tier-cleanup on   # optional, archives 10 oldest issues only on free-tier limit errors")
        print()
        print("  Option B — user-scope env file (recommended for solo dev, shared across repos):")
        print("    1. mkdir -p ~/.config/dev-kit")
        print("    2. echo 'LINEAR_API_KEY=<your-token>' >> ~/.config/dev-kit/.env")
        print("       (or $XDG_CONFIG_HOME/dev-kit/.env if XDG_CONFIG_HOME is set)")
        print("         Optional: LINEAR_TEAM_ID=..., LINEAR_PROJECT_NAME=...")
        print("    3. python3 tools/linear_sync.py on")
        print("    4. python3 tools/linear_sync.py project-name <name>   # optional")
        print("    5. python3 tools/linear_sync.py free-tier-cleanup on   # optional")
        print()
        print("  Option C — per-worktree env file (backward compat, Linear-only):")
        print(f"    1. Add to {repo / _ENV_FILE_REL} (untracked, .gitignore'd):")
        print("         LINEAR_API_KEY=<your-token>")
        print("    2. python3 tools/linear_sync.py on")
        print("    3. python3 tools/linear_sync.py free-tier-cleanup on   # optional")
        env_key = bool(os.environ.get("LINEAR_API_KEY", "").strip())
        env_file = (repo / _ENV_FILE_REL).is_file()
        user_env = _user_env_path()
        user_env_present = user_env.is_file()
        print()
        print(f"  LINEAR_API_KEY set:                       {env_key}")
        print(f"  ~/.config/dev-kit/.env present:           {user_env_present}  ({user_env})")
        print(f"  .dev-kit/.env.linear present:             {env_file}")
        cfg = _read_worktree_config(repo)
        print(f"  worktree config: {cfg or '(none — defaults to env-only)'}")
        print("  free-tier cleanup: disabled by default; enable with `free-tier-cleanup on`")
        return 0
    print(f"linear: unknown command {cmd!r} (try: setup|on|off|free-tier-cleanup|project-name|status|list|sync)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
