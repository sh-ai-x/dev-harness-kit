"""babysit_pr_reliability.py -- reliability helpers for /dev-kit:babysit-pr.

Pure-function primitives consumed by the babysit-pr skill
(`skills/babysit-pr/SKILL.md`):

  is_stale_lock(path, ttl_seconds=LOCK_TTL_SECONDS)
      Detect a stale babysit.lock left behind by a SIGKILL / OOM /
      network-partition during a previous run. Stale locks wedge every
      future babysit-pr iteration. Returns True when EITHER
        (a) the lock file mtime is older than ttl_seconds ago, OR
        (b) the recorded pid= field names a process that no longer
            exists (Linux: pid absent from /proc; macOS: kill(0)
            fails with ESRCH).

  classify_check(check, now_epoch, ghost_threshold_seconds=GHOST_CHECK_THRESHOLD_SECONDS)
      Classify a single `gh pr checks` entry. Returns one of
        approved  -- conclusion in {success, skipped, neutral}
        failing   -- conclusion in {failure, cancelled, timed_out,
                                    stale, error}
        pending   -- conclusion is None and the check looks alive
                     (startedAt/updatedAt within ghost_threshold_seconds,
                     OR neither is set yet -- a freshly requested/queued
                     check has no timestamp to measure elapsed time
                     against, so it stays pending rather than ghosting
                     at age zero)
        ghost     -- conclusion is None AND (startedAt/updatedAt is set
                     but older than ghost_threshold_seconds, OR explicit
                     databaseId is missing entirely -- GitHub's signal
                     that the workflow run has been pruned from the
                     checks table). The skill should stop waiting on a
                     ghost check and surface it as a recovery-required
                     failure.
      The function never raises: malformed inputs return "pending" (the
      most conservative non-alarming default).

  build_check_state(checks)
      Reduce a `gh pr checks --json name,conclusion,databaseId` listing
      to a compact {name: {conclusion, databaseId}} snapshot, suitable
      for persisting to `.dev-kit/babysit-checks.json` between
      iterations.

  diff_check_states(prev_state, curr_checks)
      Compare a cached build_check_state() snapshot against a fresh
      `gh pr checks` listing. Returns {"changed": [...], "unchanged":
      [...]} check names (sorted). A check is "changed" when it is new
      or its conclusion/databaseId moved since the cache; "unchanged"
      means the check is in the exact same state as last iteration.
      babysit-pr's FETCH LOGS step (§Algorithm step 5) skips re-fetching
      a failing check's log when it comes back "unchanged" -- the log
      content would be identical to what was already diagnosed, so
      re-fetching wastes a `gh run view --log-failed` round-trip per
      iteration.

All helpers are deterministic (no time-of-day randomness -- callers
pass `now_epoch`) so regression tests can reproduce ghost / fresh-lock
states without sleeping.

The default TTL (LOCK_TTL_SECONDS = 1800) and ghost threshold
(GHOST_CHECK_THRESHOLD_SECONDS = 300) are exported as module-level
constants so a future tweak in one place is reflected everywhere
(function defaults, docstrings, any future caller that wants the
canonical value).

This module is the exclusive home for babysit-pr reliability
primitives. It does not touch `lib/analysis_core/*` or
`tools/skill_usage.py`.
"""
from __future__ import annotations

import calendar
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

PathLike = Union[str, os.PathLike]

# Default TTL for an `is_stale_lock` check: 30 minutes. Generous for the
# babysit-pr per-iteration push cycle, but tight enough that a SIGKILL
# during a run leaves a stale lock the next iteration can detect.
LOCK_TTL_SECONDS: int = 1800

# Default ghost-check threshold for `classify_check`: 5 minutes. Any
# check that has been pending longer than this with no fresh
# startedAt/updatedAt (or no databaseId at all) is treated as a ghost
# the babysit-pr loop should stop waiting on.
GHOST_CHECK_THRESHOLD_SECONDS: int = 300

# Outcome strings observed by `gh pr checks --json name,state,conclusion`
# in the wild (union of GitHub Actions conclusion vocabulary).
APPROVED_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
FAILING_CONCLUSIONS = frozenset({
    "failure", "failures", "cancelled", "timed_out", "stale", "error",
})


def _pid_alive(pid: int) -> bool:
    """Return True when `pid` refers to a running process on this host.

    Linux: a running pid has an entry under /proc/<pid>.
    macOS: kill(pid, 0) succeeds when the pid exists, fails ESRCH when
    not, EPERM when the pid exists but we do not own it (treat as alive
    so we do not falsely classify the lock as stale).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _parse_pid_from_lock(content: str) -> int | None:
    """Best-effort parse of `pid=<int>` from a lock file body.

    Returns the first integer after `pid=` on any line, or None.
    Tolerates surrounding whitespace and arbitrary trailing characters
    -- the babysit-pr format is `<ISO> pid=<n> branch=<x>`.
    """
    for line in content.splitlines():
        m = re.search(r"pid=(\d+)", line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def is_stale_lock(
    path: PathLike,
    ttl_seconds: int = LOCK_TTL_SECONDS,
    *,
    now_epoch: float | None = None,
) -> bool:
    """Return True when the lock file at `path` is stale.

    A lock is stale when it is older than `ttl_seconds` (default 30
    minutes, generous for babysit-pr's per-iteration push cycle), OR the
    recorded pid no longer exists.

    Missing `path` returns False -- there is nothing to be stale -- so
    callers can short-circuit without a try/except dance:

        if not is_stale_lock(".dev-kit/babysit.lock"):
            return already_running_error

    The `now_epoch` parameter is for tests only.
    """
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return False

    now = now_epoch if now_epoch is not None else time.time()
    age = now - st.st_mtime
    if age > ttl_seconds:
        return True

    try:
        body = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Read failure on a lock we just stat()'d: be conservative and
        # call it non-stale. The next babysit-pr run can re-evaluate.
        return False

    pid = _parse_pid_from_lock(body)
    if pid is not None and not _pid_alive(pid):
        return True

    return False


def _epoch_from_iso(s: Any) -> float | None:
    """Convert an ISO-8601-ish timestamp to epoch seconds (UTC).

    Accepts the shapes GitHub emits in `startedAt` / `updatedAt`:
        2026-07-18T14:23:45Z
        2026-07-18T14:23:45.123Z
        2026-07-18T14:23:45+00:00
    Returns None on unparseable input. Never raises.

    Uses `calendar.timegm` so the result is a UTC epoch (not local-
    time-dependent) -- callers compare against their own UTC `now_epoch`.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    base = s.split(".")[0]
    if base.endswith("Z"):
        base = base[:-1]
    # Drop trailing "+HH:MM" / "-HH:MM" timezone marker if present.
    # Date-only strings or naive timestamps stay as-is.
    for marker in ("+", "-"):
        idx = base.rfind(marker)
        if idx > 10:
            base = base[:idx]
    base = base[:19]
    try:
        return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def classify_check(
    check: Mapping[str, Any],
    now_epoch: float,
    *,
    ghost_threshold_seconds: int = GHOST_CHECK_THRESHOLD_SECONDS,
) -> str:
    """Classify a `gh pr checks` entry.

    Returns one of: "approved" | "failing" | "pending" | "ghost".

    `ghost` is the recovery contract: a check that has been pending long
    enough that no live workflow will ever resolve it (worktree removed,
    workflow file renamed or deleted, OR the run was cancelled at the
    GitHub side and never reported back). The babysit-pr loop should
    stop blocking on ghost checks and surface them as recovery-required
    failures.
    """
    if not isinstance(check, Mapping):
        return "pending"

    conclusion = check.get("conclusion")

    # Terminal conclusions: never ghost.
    if isinstance(conclusion, str):
        c = conclusion.lower()
        if c in APPROVED_CONCLUSIONS:
            return "approved"
        if c in FAILING_CONCLUSIONS:
            return "failing"
        # Unknown conclusion string -- treat as pending rather than ghost
        # so the babysit does not silence a check the gateway may yet
        # report on.
        return "pending"

    # No conclusion yet. Distinguish live-pending from ghost.
    raw_db = check.get("databaseId")
    database_id_present = (
        isinstance(raw_db, int)
        or (isinstance(raw_db, str) and raw_db.strip().isdigit())
    )
    started_epoch = _epoch_from_iso(check.get("startedAt"))
    updated_epoch = _epoch_from_iso(check.get("updatedAt"))

    last_seen_candidates = [t for t in (started_epoch, updated_epoch) if t is not None]
    last_seen = max(last_seen_candidates) if last_seen_candidates else None

    if not database_id_present:
        # No databaseId is GitHub's "this run has been pruned from the
        # checks table" signal. Ghost regardless of state.
        return "ghost"

    if last_seen is None:
        # No timestamp to anchor against. "expected" / "waiting" /
        # "queued" / "requested" states mean a check that has never
        # started -- with no startedAt/updatedAt there is no elapsed
        # time to compare against ghost_threshold_seconds, so a
        # freshly-requested check (age zero) is pending, not ghost.
        # They ghost out only once the threshold below is actually
        # exceeded -- which requires a timestamp to measure against, so
        # a check that legitimately ages past the threshold will carry
        # a stale startedAt/updatedAt and fall through to the
        # `last_seen is not None` branch instead.
        return "pending"

    age = now_epoch - last_seen
    if age > ghost_threshold_seconds:
        return "ghost"
    return "pending"


def build_check_state(checks: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Reduce a `gh pr checks` listing to a {name: {conclusion,
    databaseId}} snapshot for change-detection across babysit-pr
    iterations.

    Entries that are not a mapping, or have no usable (non-empty
    string) `name`, are skipped -- they cannot be diffed by name.
    """
    state: dict[str, dict[str, Any]] = {}
    for c in checks:
        if not isinstance(c, Mapping):
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name:
            continue
        state[name] = {
            "conclusion": c.get("conclusion"),
            "databaseId": c.get("databaseId"),
        }
    return state


def diff_check_states(
    prev_state: Mapping[str, Mapping[str, Any]],
    curr_checks: Iterable[Any],
) -> dict[str, list[str]]:
    """Classify each check in `curr_checks` as "changed" or "unchanged"
    relative to a cached `build_check_state()` snapshot.

    A check is "changed" when it is new (absent from `prev_state`), or
    its `conclusion` or `databaseId` differs from the cached value --
    i.e. the workflow actually re-ran or resolved since the last
    snapshot. A check is "unchanged" when both fields are identical to
    the cache.

    Returns {"changed": [...], "unchanged": [...]}, both sorted.
    """
    curr_state = build_check_state(curr_checks)
    changed: list[str] = []
    unchanged: list[str] = []
    for name, cur in curr_state.items():
        prev = prev_state.get(name)
        if prev is None:
            changed.append(name)
            continue
        if (
            prev.get("conclusion") != cur.get("conclusion")
            or prev.get("databaseId") != cur.get("databaseId")
        ):
            changed.append(name)
        else:
            unchanged.append(name)
    return {"changed": sorted(changed), "unchanged": sorted(unchanged)}


def read_pr_lock_body(path: PathLike) -> str:
    """Return the raw body of a per-PR lock file, or "" on missing/unreadable.

    Pure helper, no I/O randomness: callers can pass it the path they
    just verified exists. Used by `bin/babysit-pr-local.sh` to print
    the *current* lock holder's PID + branch + ISO timestamp in its
    "already running" diagnostic so the operator can decide whether
    to kill the previous run or wait for it. Failure modes (missing,
    unreadable, permission denied, is-a-directory) all collapse to
    an empty string -- the caller already gated on existence, so an
    empty body just means "the lock body is gone, treat as stale".
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
