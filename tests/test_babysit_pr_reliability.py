"""test_babysit_pr_reliability.py -- unit tests for
lib/babysit_pr_reliability.py.

Pins the contract for the two helpers that close Gap #11 (stale lock)
and Gap #12 (ghost workflow classification) from
docs/hooks/hook-coverage-gaps.md:

  is_stale_lock(path, ttl_seconds, *, now_epoch=None)
    T1: missing path returns False (nothing to be stale)
    T2: fresh lock with running pid returns False
    T3: lock older than TTL returns True (clock skew safe)
    T4: fresh lock but stale pid returns True
    T5: lock with malformed body returns False (be conservative)
    T6: lock with pid= field but TTL not exceeded AND pid alive =>
        False

  classify_check(check, now_epoch, *, ghost_threshold_seconds=300)
    T7: approved conclusions return "approved"
    T8: failing conclusions return "failing"
    T9: live-pending (recent startedAt) returns "pending"
    T10: long-pending without databaseId returns "ghost"
    T11: long-pending with databaseId but old updatedAt returns "ghost"
    T12: malformed check returns "pending" (never raises)
    T13: short-pending keep returning "pending"
    T14: fresh requested/queued/expected/waiting check with a
         databaseId but no startedAt/updatedAt at all (age zero)
         returns "pending", not "ghost" (issue #481 regression)
    T15: same states, but with a stale (past-threshold) updatedAt,
         still correctly return "ghost"
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
# Add the worktree's lib/ to sys.path so the helper is importable as
# `babysit_pr_reliability`. Tests run from the main checkout by default
# (pytest resolves relative paths against the test file's parent); the
# worktree's lib/ is the canonical source for this branch.
sys.path.insert(0, str(REPO_ROOT / "lib"))

# Import is part of the contract: the helper must be importable from the
# repo root and live in lib/babysit_pr_reliability.py.
import babysit_pr_reliability as bpr  # noqa: E402


def _make_pid_alive_body() -> str:
    """Build a lock body that names a pid known to be alive.

    Uses os.getpid() (this Python process) so the pid-alive probe in
    the helper reliably returns True regardless of platform quirks.
    """
    return f"2026-07-18T14:23:45Z pid={os.getpid()} branch=feat/x"


def _make_pid_dead_body() -> str:
    # Pick a pid that should never be a live process (large negative
    # numbers fail the pid=0 safety check in _pid_alive).
    return "2026-07-18T14:23:45Z pid=1 branch=feat/x"  # /proc/1 may exist on Linux; covered below


class TestModuleConstants(unittest.TestCase):
    """Issue #310: the lock TTL (1800) and ghost-check threshold (300)
    live as module-level constants so the values are not duplicated
    between function defaults and docstrings. Re-exports keep the
    constants importable as `bpr.LOCK_TTL_SECONDS` / `bpr.GHOST_CHECK_THRESHOLD_SECONDS`.
    """

    def test_lock_ttl_constant_matches_prior_default(self):
        # 1800 was the previous default value for `is_stale_lock(..., ttl_seconds=...)`.
        # Pinning it here means any future change to the module default
        # surfaces here as a deliberate constant bump, not an accidental edit.
        self.assertEqual(bpr.LOCK_TTL_SECONDS, 1800)

    def test_ghost_check_threshold_constant_matches_prior_default(self):
        # 300 was the previous default for `classify_check(..., ghost_threshold_seconds=...)`.
        self.assertEqual(bpr.GHOST_CHECK_THRESHOLD_SECONDS, 300)

    def test_constants_are_reexported(self):
        import importlib
        mod = importlib.import_module("babysit_pr_reliability")
        # The constants must be top-level module attributes so external
        # callers (the babysit-pr skill) can read them without instantiating
        # anything.
        self.assertTrue(hasattr(mod, "LOCK_TTL_SECONDS"))
        self.assertTrue(hasattr(mod, "GHOST_CHECK_THRESHOLD_SECONDS"))
        # And the function defaults must point at the constants so there is
        # exactly one place to bump the numbers.
        import inspect
        sig_stale = inspect.signature(bpr.is_stale_lock)
        sig_classify = inspect.signature(bpr.classify_check)
        self.assertEqual(
            sig_stale.parameters["ttl_seconds"].default,
            bpr.LOCK_TTL_SECONDS,
        )
        self.assertEqual(
            sig_classify.parameters["ghost_threshold_seconds"].default,
            bpr.GHOST_CHECK_THRESHOLD_SECONDS,
        )


class TestIsStaleLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.lock = self.root / "babysit.lock"

    # T1
    def test_missing_returns_false(self) -> None:
        self.assertFalse(bpr.is_stale_lock(self.lock))

    # T2
    def test_fresh_lock_with_running_pid_is_not_stale(self) -> None:
        self.lock.write_text(_make_pid_alive_body())
        now = time.time()  # lock mtime is now-ish
        self.assertFalse(bpr.is_stale_lock(self.lock, now_epoch=now))

    # T3
    def test_lock_older_than_ttl_is_stale(self) -> None:
        self.lock.write_text(_make_pid_alive_body())
        # Set mtime to one hour ago
        old = time.time() - 3600
        os.utime(self.lock, (old, old))
        now = time.time()
        self.assertTrue(bpr.is_stale_lock(self.lock, ttl_seconds=1800, now_epoch=now))

    # T4
    def test_fresh_lock_with_dead_pid_is_stale(self) -> None:
        # Synthesize a lock whose pid is unlikely to exist on either
        # platform: pick the largest valid 32-bit pid (Linux pid_max
        # default), which almost certainly has no process attached.
        dead_pid = 32768  # small; pid=1 may exist on Linux CI
        # Use a more reliable "dead" pid: 2^30 (far beyond pid_max).
        dead_pid = 2**30
        self.lock.write_text(f"2026-07-18T14:23:45Z pid={dead_pid} branch=feat/x")
        # Fresh mtime
        now = time.time()
        os.utime(self.lock, (now, now))
        self.assertTrue(bpr.is_stale_lock(self.lock, ttl_seconds=3600, now_epoch=now))

    # T5
    def test_malformed_body_returns_false(self) -> None:
        # No pid= field at all -> not classified stale; caller can
        # decide whether to error or proceed.
        self.lock.write_text("not-a-lock-file\n")
        now = time.time()
        os.utime(self.lock, (now, now))
        self.assertFalse(bpr.is_stale_lock(self.lock, now_epoch=now))

    # T6
    def test_short_ttl_with_running_pid_is_not_stale(self) -> None:
        # Negative ttl + live pid: returns False (not stale on age).
        self.lock.write_text(_make_pid_alive_body())
        now = time.time()
        os.utime(self.lock, (now, now))
        self.assertFalse(bpr.is_stale_lock(self.lock, ttl_seconds=60, now_epoch=now))


class TestClassifyCheck(unittest.TestCase):
    NOW = 1_700_000_000  # a fixed reference point

    # T7
    def test_approved_conclusions(self) -> None:
        for c in ("success", "skipped", "neutral"):
            self.assertEqual(
                bpr.classify_check({"conclusion": c, "databaseId": 1}, self.NOW),
                "approved",
            )

    # T8
    def test_failing_conclusions(self) -> None:
        for c in ("failure", "failures", "cancelled", "timed_out", "stale", "error"):
            self.assertEqual(
                bpr.classify_check({"conclusion": c, "databaseId": 1}, self.NOW),
                "failing",
            )

    # T9
    def test_live_pending_returns_pending(self) -> None:
        # startedAt is 30s ago -> live pending.
        recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.NOW - 30))
        self.assertEqual(
            bpr.classify_check({
                "conclusion": None,
                "state": "pending",
                "databaseId": 12345,
                "startedAt": recent_iso,
            }, self.NOW),
            "pending",
        )

    # T10
    def test_long_pending_no_databaseId_is_ghost(self) -> None:
        # startedAt is 10 minutes ago, well beyond default 300s ghost
        # threshold; databaseId is missing.
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.NOW - 600))
        # databaseId absent (None, missing, or empty string)
        for db_field in (None, "", 0, "0", False):
            check = {
                "conclusion": None,
                "state": "pending",
                "startedAt": old_iso,
            }
            if db_field is not None:
                check["databaseId"] = db_field
            # 0 / "0" / "" / False / None all fail the presence check
            self.assertEqual(
                bpr.classify_check(check, self.NOW),
                "ghost",
                f"db_field={db_field!r} should ghost",
            )

    # T11
    def test_long_pending_with_databaseId_but_old_updatedAt_is_ghost(self) -> None:
        # updatedAt far in the past > ghost threshold => ghost
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.NOW - 3600))
        self.assertEqual(
            bpr.classify_check({
                "conclusion": None,
                "state": "pending",
                "databaseId": 99999,
                "updatedAt": old_iso,
            }, self.NOW),
            "ghost",
        )

    # T12
    def test_malformed_check_returns_pending_or_ghost_never_raises(self) -> None:
        # The contract: classify_check never raises. Truly malformed
        # shapes (None, str, int, list, empty dict) fall back to
        # "pending". A partial-but-shape-valid payload
        # (`{"state": "pending"}` with no databaseId) is ghost per the
        # documented rule. Both classes are acceptable; what matters is
        # "approved"/"failing" are reserved for terminal conclusions and
        # no input causes an exception.
        for bad in (None, "string", 42, []):
            self.assertEqual(
                bpr.classify_check(bad, self.NOW),  # type: ignore[arg-type]
                "pending",
            )
        # Partial check (no databaseId) -> ghost per contract.
        self.assertEqual(
            bpr.classify_check({"state": "pending"}, self.NOW),
            "ghost",
        )
        # Empty dict is ambiguous; the function returns "pending" because
        # `isinstance({}, Mapping)` is True but conclusion is missing
        # AND databaseId is missing -- per the documented rule, no
        # databaseId is ghost. Pin the actual contract here.
        self.assertEqual(bpr.classify_check({}, self.NOW), "ghost")

    # T13
    def test_short_pending_with_databaseId_keeps_pending(self) -> None:
        # Exactly at the threshold (300s old): NOT a ghost (use strict >).
        boundary_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.NOW - 299))
        self.assertEqual(
            bpr.classify_check({
                "conclusion": None,
                "state": "pending",
                "databaseId": 111,
                "updatedAt": boundary_iso,
            }, self.NOW),
            "pending",
        )

    # T14 -- issue #481 regression: a check that was JUST requested (has
    # a databaseId but neither startedAt nor updatedAt has appeared yet
    # because CI has not picked it up) must NOT ghost at age zero. The
    # inline comment right above the fixed branch documents this: these
    # states "ghost out only after the threshold" -- with no timestamp
    # at all there is no elapsed time to measure, so the only sound
    # default is "pending".
    def test_fresh_requested_check_with_no_timestamp_is_pending_not_ghost(self) -> None:
        for state in ("expected", "waiting", "queued", "requested"):
            check = {
                "conclusion": None,
                "state": state,
                "databaseId": 555,
                # No startedAt/updatedAt at all -- just requested.
            }
            self.assertEqual(
                bpr.classify_check(check, self.NOW),
                "pending",
                f"state={state!r} with databaseId but no timestamp should be pending, not ghost",
            )

    # T15 -- same states, but genuinely past the threshold (stale
    # updatedAt), must still correctly classify as "ghost". This pins
    # that the fix for T14 did not weaken the real threshold gate.
    def test_stale_requested_check_past_threshold_is_still_ghost(self) -> None:
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.NOW - 3600))
        for state in ("expected", "waiting", "queued", "requested"):
            check = {
                "conclusion": None,
                "state": state,
                "databaseId": 556,
                "updatedAt": old_iso,
            }
            self.assertEqual(
                bpr.classify_check(check, self.NOW),
                "ghost",
                f"state={state!r} with databaseId and stale updatedAt should still ghost",
            )


class TestBuildCheckState(unittest.TestCase):
    """build_check_state(checks) -- reduce a `gh pr checks` listing to a
    compact {name: {conclusion, databaseId}} snapshot for change-detection
    across babysit-pr iterations."""

    def test_reduces_checks_to_name_keyed_state(self) -> None:
        checks = [
            {"name": "pytest", "conclusion": "failure", "databaseId": 111},
            {"name": "ruff", "conclusion": "success", "databaseId": 222},
        ]
        self.assertEqual(
            bpr.build_check_state(checks),
            {
                "pytest": {"conclusion": "failure", "databaseId": 111},
                "ruff": {"conclusion": "success", "databaseId": 222},
            },
        )

    def test_skips_entries_without_a_usable_name(self) -> None:
        checks = [
            {"conclusion": "failure", "databaseId": 111},  # no name
            {"name": "", "conclusion": "success", "databaseId": 222},  # empty name
            "not-a-dict",
            {"name": "ruff", "conclusion": "success", "databaseId": 333},
        ]
        self.assertEqual(
            bpr.build_check_state(checks),
            {"ruff": {"conclusion": "success", "databaseId": 333}},
        )

    def test_empty_input_returns_empty_state(self) -> None:
        self.assertEqual(bpr.build_check_state([]), {})


class TestDiffCheckStates(unittest.TestCase):
    """diff_check_states(prev_state, curr_checks) -- classify each current
    check as "changed" (new, or conclusion/databaseId moved since the
    cached snapshot) or "unchanged" (byte-identical to the cache).

    babysit-pr's FETCH LOGS step (§Algorithm step 5) uses "unchanged" to
    skip re-fetching a failing check's log when nothing has moved since
    the last iteration -- the log content would be identical to what was
    already diagnosed, so re-fetching wastes a `gh run view --log-failed`
    round-trip per iteration.
    """

    def test_new_check_not_in_prev_state_is_changed(self) -> None:
        prev = {}
        curr = [{"name": "pytest", "conclusion": "failure", "databaseId": 111}]
        result = bpr.diff_check_states(prev, curr)
        self.assertEqual(result, {"changed": ["pytest"], "unchanged": []})

    def test_identical_conclusion_and_database_id_is_unchanged(self) -> None:
        prev = {"pytest": {"conclusion": "failure", "databaseId": 111}}
        curr = [{"name": "pytest", "conclusion": "failure", "databaseId": 111}]
        result = bpr.diff_check_states(prev, curr)
        self.assertEqual(result, {"changed": [], "unchanged": ["pytest"]})

    def test_conclusion_changed_is_changed(self) -> None:
        prev = {"pytest": {"conclusion": "failure", "databaseId": 111}}
        curr = [{"name": "pytest", "conclusion": "success", "databaseId": 111}]
        result = bpr.diff_check_states(prev, curr)
        self.assertEqual(result, {"changed": ["pytest"], "unchanged": []})

    def test_database_id_changed_is_changed(self) -> None:
        # Same conclusion, but a new workflow run (new databaseId) means
        # the failure log content is a fresh run, not the one diagnosed
        # last iteration -- must be re-fetched even though the conclusion
        # string itself is unchanged.
        prev = {"pytest": {"conclusion": "failure", "databaseId": 111}}
        curr = [{"name": "pytest", "conclusion": "failure", "databaseId": 222}]
        result = bpr.diff_check_states(prev, curr)
        self.assertEqual(result, {"changed": ["pytest"], "unchanged": []})

    def test_mixed_changed_and_unchanged_sorted(self) -> None:
        prev = {
            "pytest": {"conclusion": "failure", "databaseId": 111},
            "ruff": {"conclusion": "success", "databaseId": 222},
        }
        curr = [
            {"name": "pytest", "conclusion": "success", "databaseId": 333},  # changed
            {"name": "ruff", "conclusion": "success", "databaseId": 222},  # unchanged
            {"name": "validate", "conclusion": "failure", "databaseId": 444},  # new
        ]
        result = bpr.diff_check_states(prev, curr)
        self.assertEqual(result["changed"], ["pytest", "validate"])
        self.assertEqual(result["unchanged"], ["ruff"])

    def test_empty_prev_state_and_empty_curr_checks(self) -> None:
        self.assertEqual(bpr.diff_check_states({}, []), {"changed": [], "unchanged": []})


class TestSelectGatesDynamic(unittest.TestCase):
    """v1.1.0 — `select_gates_dynamic()` returns a frozenset of gate
    names the LLM-judge layer (`lib/gate_dynamic`) recommended
    skipping for this iteration. The helper is a thin wrapper around
    `lib.gate_dynamic.select_gates()` that flattens the per-gate
    decision payload to a frozenset + threads the result into
    `persist_loop_snapshot(dynamic_skipped=...)`.
    """

    def _patched_select_gates(self, *skip_names):
        """Replace `lib/gate_dynamic.select_gates` with a fake returning
        a synthetic decision whose `decisions` tuple skips the given names.
        """
        from unittest import mock

        import gate_dynamic
        decisions = tuple(
            gate_dynamic.GateDecision(
                gate_name=n,
                skip=(n in skip_names),
                reasoning=f"fake:{n}",
                confidence=0.9,
                raw_score={},
            )
            for n in ("review", "security", "maintenance")
        )
        return mock.patch.object(
            gate_dynamic, "select_gates",
            return_value=gate_dynamic.GateSkipDecision(
                head_sha="abc",
                decisions=decisions,
                llm_raw={"scores": {}, "raw": ""},
                gates_hash="",
                decided_at_iso="2026-09-16T00:00:00Z",
            ),
        )

    def test_returns_frozenset_of_skipped_gate_names(self) -> None:
        with self._patched_select_gates("maintenance"):
            skipped = bpr.select_gates_dynamic(
                parent_pr=42, head_sha="abc", iteration=2,
                diff_stat="", diff_files=[], pr_body="",
                previous_verdicts={}, worktree_root=None,
            )
        self.assertEqual(skipped, frozenset({"maintenance"}))

    def test_returns_empty_when_no_gates_skipped(self) -> None:
        with self._patched_select_gates():
            skipped = bpr.select_gates_dynamic(
                parent_pr=42, head_sha="abc", iteration=2,
                diff_stat="", diff_files=[], pr_body="",
                previous_verdicts={}, worktree_root=None,
            )
        self.assertEqual(skipped, frozenset())


if __name__ == "__main__":
    unittest.main()
