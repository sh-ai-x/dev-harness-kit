"""Dispatch-mode classifier.

Pure function: takes a list of build steps (as parsed from
`phases/<phase>/index.json`), returns a `DispatchDecision` describing
whether the steps should be executed sequentially or in parallel.

Replaces the legacy `--parallel N` argparse flag on `/dev-kit:build`.
The classifier is the single source of truth for the dispatch decision;
the build runner only branches on the verdict.

Iron Law (L5 — one answer, no option lists unless asked):
    - Default = sequential. Parallel is opt-in by evidence, not by user toggle.
    - First-match-wins priority order; a single hit at any rule yields sequential.

tmux + long-running safety:
    - Pure Python function, no I/O, no subprocess. Safe under tmux + long-running.
    - Idempotent across re-invocation within a session: byte-identical output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DispatchDecision:
    """The classifier's verdict.

    Attributes:
        mode: "sequential" (default) or "parallel".
        reason: One-line human-readable reason, emitted as the first line of
                the build log. Auditable so the user can see why parallelism
                was rejected.
    """
    mode: str
    reason: str


# Minimum N at which parallel is even considered. Below this threshold,
# the (N+1)x supervisor cost exceeds the wall-clock savings on real
# builds (the harness runner pays the orchestrator re-read for every
# sub-agent).
_MIN_PARALLEL_N = 4

# Vague-scope markers: if a step's preamble contains any of these, the
# scope is treated as ambiguous and the classifier falls back to
# sequential. The match is intentionally narrow — substring, not regex —
# so a step that legitimately mentions "TODO" in a comment is not
# penalized. All markers are lowercased; the haystack is lowercased before
# the substring check.
#
# NOTE: single-character markers like "?" are intentionally NOT included.
# A literal "?" appears in URLs (`?foo=bar`), ternary expressions
# (`a ? b : c`), and legitimate documentation questions, none of which
# signal ambiguous scope. Ambiguity is captured by the multi-character
# words below.
_VAGUE_SCOPE_MARKERS = (
    "todo:", "fixme:", "tbd:", "tk:", "maybe", "perhaps", "either",
)


def _normalize_writes(writes: object) -> frozenset[str] | None:
    """Normalize the `writes` field to a frozenset of file paths.

    Accepts: list[str], tuple[str], scalar str (treated as a 1-path
    list), and None / empty (treated as "no declared writes").

    Path normalization: strips leading "./" so "./src/x" and "src/x"
    are treated as the same file. Does not resolve ".." or absolute
    paths — that's a separate concern (the runner cd's into the worktree
    first, so paths are relative to the worktree root by convention).

    Returns None when the input is unsupported (e.g. a dict, int, list
    of non-strings). Callers should treat None as a fail-closed signal:
    when we can't normalize, we cannot prove isolation, so default to
    sequential.
    """
    if writes is None:
        return None
    if isinstance(writes, str):
        items = [writes]
    elif isinstance(writes, (list, tuple)):
        items = list(writes)
    else:
        return None
    paths: list[str] = []
    for x in items:
        if not isinstance(x, str):
            return None
        # Strip leading "./" so "./src/x" and "src/x" are the same.
        p = x
        if p.startswith("./"):
            p = p[2:]
        paths.append(p)
    return frozenset(paths)


def _has_dependency_edge(steps: list[dict]) -> bool:
    """True if any step declares an explicit or implicit dependency edge.

    Dependency sources checked:
      - `depends_on` list field (explicit, e.g. ["step1", "step3"]).
      - `consumes` string field (implicit: this step consumes another's
        artifact by name reference).

    `depends_on` is constrained to a list per the helper docstring;
    strings (or any other truthy non-list) are ignored — the contract
    is explicit about the type.
    """
    for step in steps:
        deps = step.get("depends_on")
        if isinstance(deps, list) and deps:
            return True
        if step.get("consumes"):
            return True
    return False


def _normalize_ac(ac: object) -> list[str]:
    """Normalize the `ac` (acceptance-criteria) field to a list of strings.

    The canonical plan/index contract permits three shapes:
      - `str` (single criterion, joined word-by-word in some plan
        outputs — see PR #579 3-dim review round 8)
      - `list[str]` (preferred)
      - `dict[str, str]` (some plan outputs; the values are the
        criterion text, the keys are labels)

    Other shapes fall back to empty list (safe default — no AC
    evidence, treat as missing metadata per the fail-closed rule).
    """
    if ac is None:
        return []
    if isinstance(ac, str):
        return [ac]
    if isinstance(ac, list):
        return [str(x) for x in ac if isinstance(x, (str, int, float))]
    if isinstance(ac, dict):
        return [str(v) for v in ac.values() if isinstance(v, (str, int, float))]
    return []


def _has_vague_scope(step: dict) -> bool:
    """True if the step's preamble contains a vague-scope marker.

    Looks at `preamble` (string) and `ac` (acceptance criteria, any
    of: str / list / dict — see `_normalize_ac`). If either contains
    a marker, the scope is treated as ambiguous.

    Per the 3-dim review on PR #579 (round 8): unvalidated ac shapes
    can bypass the safety rule. `ac="TODO: investigate"` is joined
    character-by-character into "T O D O : ...", missing the `todo:`
    marker. The fix is to normalize ac to a list of strings before
    searching.
    """
    preamble = (step.get("preamble") or "").lower()
    ac_haystack = " ".join(_normalize_ac(step.get("ac"))).lower()
    haystack = preamble + "\n" + ac_haystack
    return any(marker in haystack for marker in _VAGUE_SCOPE_MARKERS)


def _has_overlap(steps: list[dict]) -> bool:
    """True if two steps declare any overlapping `writes:` paths.

    Overlap triggers sequential regardless of whether each step has a
    `partition` field. Partition documents *intent* (this step owns a
    region); overlap on writes is a *factual* collision. When both
    signals disagree, the factual collision wins — the user must split
    the writes or merge the steps before parallel is safe.

    Each step's `writes` is normalized via `_normalize_writes` (handles
    scalar / list / tuple / "./"-prefix shapes consistently). A
    `None` return (unsupported shape) fails closed — when we cannot
    normalize, we cannot prove isolation, so the gate defaults to
    sequential.

    inspect 2026-08-27 overeng-3: pre-normalize each step's `writes`
    once (O(N)) instead of recomputing inside the inner (i,j) loop
    (O(N^2)). For a 50-step phase this is ~50 normalizations vs the
    old ~2500.
    """
    n = len(steps)
    # Single pass to normalize every step's writes once. None means
    # the step had a `writes:` field in an unsupported shape; the
    # boolean second tuple element records whether `writes` was
    # explicitly present (None with no key is "absent", which is OK).
    normalized: list[tuple[frozenset | None, bool]] = []
    for step in steps:
        raw = step.get("writes")
        if raw is None:
            normalized.append((None, False))
            continue
        ws = _normalize_writes(raw)
        if ws is None:
            normalized.append((None, True))
            continue
        normalized.append((ws, True))

    for i in range(n):
        writes_i, present_i = normalized[i]
        if not present_i:
            continue
        if writes_i is None:
            return True
        if not writes_i:
            continue
        for j in range(i + 1, n):
            writes_j, present_j = normalized[j]
            if not present_j:
                continue
            if writes_j is None:
                return True
            if not writes_j:
                continue
            if writes_i & writes_j:
                return True
    return False


def _has_clean_isolation(steps: list[dict]) -> bool:
    """True if every step has BOTH an empty `writes` set AND an explicit
    `partition`. This is the fail-closed default: missing `writes` OR
    missing `partition` = not clean = sequential.

    Per the 3-dim review on PR #579 (round 8): the canonical
    plan/index contract supplies only `step`/`name`/`status` (no
    `writes`, no `partition`). Treating absent `writes` as empty is
    equivalent to treating absent isolation as proof of isolation,
    which fails open. The fix is to require explicit evidence for
    every step that wants to participate in the parallel gate.
    """
    for step in steps:
        writes = _normalize_writes(step.get("writes"))
        # Fail-closed: unsupported shape = not clean.
        if writes is None and step.get("writes") is not None:
            return False
        partition = step.get("partition")
        if writes and not partition:
            return False
        if not writes and not partition:
            return False
    return True


def classify(steps: Iterable[dict]) -> DispatchDecision:
    """Classify a batch of build steps as sequential or parallel.

    Priority order (first match wins):
      1. Dependency edge between any pair → sequential.
      2. Any step has vague scope → sequential.
      3. Two steps share declared writes → sequential.
      4. N >= 4 AND every step has clean worktree isolation → parallel.
      5. Otherwise → sequential.

    Args:
        steps: Iterable of step dicts as parsed from index.json. Each step
               may declare: `depends_on`, `consumes`, `preamble`, `ac`,
               `writes`, `partition`. None are required; missing fields
               are treated as empty.

    Returns:
        DispatchDecision with `mode` ("sequential" | "parallel") and a
        `reason` string suitable for the build log's first line.
    """
    steps_list = list(steps)
    n = len(steps_list)

    # Rule 1 — dependency edge.
    if _has_dependency_edge(steps_list):
        return DispatchDecision(
            mode="sequential",
            reason=f"{n} steps, dependency edge detected",
        )

    # Rule 2 — vague scope.
    for i, step in enumerate(steps_list):
        if _has_vague_scope(step):
            return DispatchDecision(
                mode="sequential",
                reason=f"step {str(step.get('step', i + 1)).splitlines()[0]} has vague scope",
            )

    # Rule 3 — overlap on shared writes.
    if _has_overlap(steps_list):
        return DispatchDecision(
            mode="sequential",
            reason=f"{n} steps, overlapping writes",
        )

    # Rule 4 — clean isolation AND sufficient N.
    if n >= _MIN_PARALLEL_N and _has_clean_isolation(steps_list):
        return DispatchDecision(
            mode="parallel",
            reason=f"{n} steps, clean worktree isolation",
        )

    # Rule 5 — default sequential.
    suffix = "insufficient N" if n < _MIN_PARALLEL_N else "non-clean isolation"
    return DispatchDecision(
        mode="sequential",
        reason=f"{n} steps, {suffix}",
    )
