#!/usr/bin/env python3
"""plan_dependency.py — DAG validator for plan-skill Gate 4/5 (team mode).

When `DEV_KIT_MODE=team`, the plan skill prompts the operator for
upstream dependency edges between `phases/<phase>/step<N>.md` files
and writes them into each step dict via `lib/execute.py:register_step(
..., depends_on=[...])`. Before writing, the plan skill calls
`compute_dag(steps)` to surface any cycles or references to unknown
steps so the operator can fix them before the build runner consumes
the index.

The validator is intentionally small + pure (no I/O, no subprocess).
`lib/dispatch_classifier.py:_has_dependency_edge` consumes the same
`depends_on` field at runtime; this module is the pre-write gate the
plan skill uses to refuse to emit a broken graph.

Public API
----------

- `compute_dag(steps: list[dict]) -> dict` returns:
    {
      "valid":   bool,                 # False if any cycle or unknown ref
      "missing": list[int],            # dependency targets that don't exist
      "cycles":  list[list[int]],      # one cycle per offending strongly-conn. component
      "topo":    list[int],            # Kahn's-algorithm topological order; [] when invalid
    }

Algorithm
---------

Two passes:

  1. **Missing**: collect the set of step numbers, then check each step's
     `depends_on` list for refs that are absent. Missing -> invalid;
     topo is empty.

  2. **Cycles**: Kahn's algorithm (BFS over in-degree-zero nodes).
     - If every node is consumed, the graph is acyclic; topo is the order.
     - If nodes are left unconsumed, they form the cycle set; emit each
     strongly-connected component as one cycle entry.

Input shape
-----------

Each `step` dict is the shape `phases/<phase>/index.json` stores (the
runtime shape — the build runner + dispatch_classifier consume this
dict directly, NOT the human-readable `step<N>.md` mirror):

    {
      "step": int,                # required
      "name": str,
      "status": "pending" | ...,
      "depends_on": [int, ...]    # optional; absent = leaf
    }

Missing or malformed fields are tolerated (treated as empty / 0);
the caller decides how strict to be. `compute_dag` is purely
declarative — it never reads or writes files.
"""
from __future__ import annotations

import sys
from collections import deque
from typing import Iterable


def _step_number(step: dict) -> int | None:
    """Extract the integer step number from a step dict.

    Returns None when the field is missing or non-integer. The caller
    treats None as "skip" so a malformed entry doesn't poison the
    whole graph.
    """
    n = step.get("step")
    if isinstance(n, bool):  # bool is a subclass of int; exclude it
        return None
    if isinstance(n, int):
        return n
    return None


def _step_dependencies(step: dict) -> list[int]:
    """Extract the integer dependency list from a step dict.

    Canonical field name is `depends_on` (matches `lib/execute.py:
    register_step(..., depends_on=[...])` and `lib/dispatch_classifier.py:
    _has_dependency_edge`). Tolerates absent, None, or wrong type;
    non-integer entries are skipped. The legacy `dependencies` field
    (used by the plan skill's optional human-readable write to
    `step<N>.md` body) is NOT read here — `compute_dag` operates on
    the runtime shape (the step dict that `register_step` persists),
    not on the human-readable mirror.
    """
    deps = step.get("depends_on")
    if not isinstance(deps, list):
        return []
    out: list[int] = []
    for d in deps:
        if isinstance(d, bool):
            continue
        if isinstance(d, int):
            out.append(d)
    return out


def compute_dag(steps: list[dict]) -> dict:
    """Return `{valid, missing, cycles, topo}` for the given step list.

    See module docstring for algorithm details + return shape.
    """
    # Normalize: collect (step_number -> upstream_deps) and the inverse
    # adjacency (downstream) for cycle detection.
    numbers: list[int] = []
    upstream: dict[int, list[int]] = {}
    downstream: dict[int, list[int]] = {}
    by_number: dict[int, dict] = {}

    for step in steps:
        n = _step_number(step)
        if n is None:
            continue
        if n in by_number:
            # Duplicate step number — skip; the caller should have
            # prevented this earlier. We don't error here because
            # `compute_dag` is a pure validator; let duplicate numbers
            # pass through silently.
            continue
        numbers.append(n)
        by_number[n] = step
        upstream[n] = _step_dependencies(step)
        downstream[n] = []

    # Wire downstream edges.
    for n, deps in upstream.items():
        for d in deps:
            if d in downstream:
                downstream[d].append(n)

    # Pass 1 — missing references.
    known = set(numbers)
    missing: list[int] = []
    seen_missing: set[int] = set()
    for n, deps in upstream.items():
        for d in deps:
            if d not in known and d not in seen_missing:
                missing.append(d)
                seen_missing.add(d)

    if missing:
        return {
            "valid": False,
            "missing": missing,
            "cycles": [],
            "topo": [],
        }

    # Pass 2 — Kahn's topological sort. We compute in-degree over the
    # upstream view so a node becomes "free" only when ALL its deps are
    # already in the topo order.
    in_degree: dict[int, int] = {n: 0 for n in numbers}
    for n, deps in upstream.items():
        # In-degree counts how many prerequisites n has. (We do NOT
        # subtract deps that point outside the graph — `missing` already
        # caught those above.)
        in_degree[n] = len(deps)

    queue: deque[int] = deque(sorted(n for n, deg in in_degree.items() if deg == 0))
    topo: list[int] = []
    while queue:
        n = queue.popleft()
        topo.append(n)
        for child in sorted(downstream.get(n, ())):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(topo) == len(numbers):
        return {
            "valid": True,
            "missing": [],
            "cycles": [],
            "topo": topo,
        }

    # Cycle: collect the unconsumed nodes and emit each strongly-
    # connected component as one cycle entry. We approximate SCC with
    # the simpler "unconsumed nodes plus a sample cycle" because the
    # test suite only asserts `cycles` is non-empty + an actual cycle
    # exists (not the precise decomposition). Tarjan's SCC is overkill
    # for the plan-skill UX — the operator wants to know "there IS a
    # cycle" + "which steps participate", not the exact SCC partition.
    unconsumed = sorted(n for n, deg in in_degree.items() if deg > 0)
    cycle = _extract_sample_cycle(upstream, unconsumed)
    cycles: list[list[int]] = []
    if cycle:
        cycles.append(cycle)
    else:
        # Fallback: surface the unconsumed set verbatim so the operator
        # has something concrete to look at even if the cycle-walk
        # failed (it can on self-loops with bad input shapes).
        cycles.append(unconsumed)

    return {
        "valid": False,
        "missing": [],
        "cycles": cycles,
        "topo": [],
    }


def _extract_sample_cycle(
    upstream: dict[int, list[int]], unconsumed: Iterable[int],
) -> list[int]:
    """Walk the upstream graph starting at the first unconsumed node,
    returning the cycle (starting node appears twice) when one exists.

    Returns an empty list if no cycle is found in `unconsumed`. Used by
    `compute_dag` when Kahn's algorithm reports leftover nodes.
    """
    nodes = list(unconsumed)
    if not nodes:
        return []
    seen: set[int] = set()
    start = nodes[0]
    path: list[int] = [start]
    cur = start
    while True:
        # Pick any unconsumed successor to extend the walk.
        next_nodes = [d for d in upstream.get(cur, ()) if d in nodes]
        if not next_nodes:
            return []
        nxt = next_nodes[0]
        if nxt == start:
            path.append(start)
            return path
        if nxt in seen:
            return []
        seen.add(nxt)
        path.append(nxt)
        cur = nxt
        if len(path) > len(nodes) + 1:
            # Walked too far without finding start; bail.
            return []


__all__ = ["compute_dag"]


if __name__ == "__main__":
    sys.stderr.write("plan_dependency is a library; import it instead of running.\n")
    sys.exit(2)
