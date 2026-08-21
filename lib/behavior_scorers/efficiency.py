"""efficiency.py — D3 Efficiency.

Compares run metrics against a baseline TraceLog (same case_id, previous
run). When no baseline exists, the first run sets the baseline and the
scorer returns `value=3` (no signal yet) with `evidence.baseline_set=True`.

Metrics compared:
- step_count: number of TraceStep entries
- retry_total: sum of step.retries
- token_total: sum of input_tokens + output_tokens
- latency_total_ms: sum of step.latency_ms

Score mapping (proposal §01 D3):
- 5: all metrics ≤ baseline
- 4: exactly one metric > baseline (and ≤ 2x)
- 3: two metrics > baseline
- 1-2: three+ metrics > baseline OR any metric > 3x baseline
- The first run (no baseline) returns 3 with evidence.baseline_set=True.

Baseline path resolution:
- ctx.baseline_path if provided
- otherwise `<worktree>/.dev-kit/agent-behavior-baseline.json` (custom
  convention; created on first run)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from lib.behavior_scorers.types import Context, DimensionScore

DEFAULT_BASENAME = "agent-behavior-baseline.json"


def _baseline_path(worktree: Path, ctx: Context) -> Path:
    if ctx.baseline_path is not None:
        return Path(ctx.baseline_path)
    return worktree / ".dev-kit" / DEFAULT_BASENAME


def _load_baseline(path: Path) -> Optional[Dict[str, int]]:
    """Load the baseline metrics JSON. Returns None if missing/invalid.

    Refuses to follow symlinks: a worktree-controlled baseline_path
    could redirect reads to attacker-chosen JSON, smuggling hostile
    data into score evidence. Real files only.
    """
    if not path.is_file():
        return None
    if path.is_symlink():
        return None  # refuse symlink; treat as missing
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_baseline(path: Path, metrics: Dict[str, int]) -> None:
    """Write the baseline metrics JSON. Refuses to traverse symlinks.

    Use os.open with O_NOFOLLOW | O_CREAT | O_EXCL to atomically
    create the file without traversing symlinks. If a symlink already
    exists at the path, refuse silently (returns None-equivalent —
    caller treats it as no baseline).
    """
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return  # refuse to clobber a symlink
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(metrics, f, indent=2, sort_keys=True)
    except (OSError, ValueError, TypeError):
        # Best-effort cleanup on partial write
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def compute_metrics(trace_path: Path) -> Dict[str, int]:
    """Aggregate metrics from a TraceLog JSON file.

    Used by the diff tool too. Public so callers can pre-compute.

    Refuses to follow symlinks: a worktree-controlled symlink can redirect
    reads to evaluator-hostile paths. The baseline storage helpers below
    use the same guard.
    """
    if trace_path.is_symlink():
        raise ValueError(
            f"refusing to follow symlink for trace metrics: {trace_path}"
        )
    raw = json.loads(trace_path.read_text())
    steps = raw.get("steps", [])
    return {
        "step_count": len(steps),
        "retry_total": sum(int(s.get("retries", 0)) for s in steps),
        "token_total": sum(
            int(s.get("input_tokens", 0)) + int(s.get("output_tokens", 0))
            for s in steps
        ),
        "latency_total_ms": sum(int(s.get("latency_ms", 0)) for s in steps),
    }


def score(worktree: Path, ctx: Context) -> DimensionScore:
    """Score D3 by comparing the latest trace against the baseline."""
    transcripts = worktree / "eval" / "transcripts"
    if not transcripts.is_dir():
        return DimensionScore(
            dim="D3_efficiency",
            value=3,
            evidence={"reason": "no transcripts dir", "baseline_set": False},
        )

    # Find the case_id directory with the most recent trace.
    case_dirs = [p for p in transcripts.iterdir() if p.is_dir()]
    if not case_dirs:
        return DimensionScore(
            dim="D3_efficiency",
            value=3,
            evidence={"reason": "no transcripts", "baseline_set": False},
        )
    latest_dir = max(case_dirs, key=lambda d: d.stat().st_mtime)
    traces = sorted(latest_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not traces:
        return DimensionScore(
            dim="D3_efficiency",
            value=3,
            evidence={"reason": "no trace files", "baseline_set": False},
        )
    latest = traces[-1]

    current = compute_metrics(latest)
    baseline_path = _baseline_path(worktree, ctx)
    baseline = _load_baseline(baseline_path)
    if baseline is None:
        # First run: save baseline, return neutral score.
        _save_baseline(baseline_path, current)
        return DimensionScore(
            dim="D3_efficiency",
            value=3,
            evidence={"baseline_set": True, "metrics": current},
        )

    # Compare each metric against baseline. Count "worse" entries.
    worse = 0
    any_huge = False
    deltas: Dict[str, float] = {}
    for key, cur in current.items():
        base = baseline.get(key, 0)
        if base == 0:
            ratio = 1.0 if cur == 0 else float("inf")
        else:
            ratio = cur / base
        deltas[key] = round(ratio, 3)
        if ratio > 1.0:
            worse += 1
        if ratio > 3.0:
            any_huge = True

    if any_huge:
        value = 1
    elif worse == 0:
        value = 5
    elif worse == 1:
        value = 4
    elif worse == 2:
        value = 3
    else:
        value = 2

    return DimensionScore(
        dim="D3_efficiency",
        value=value,
        evidence={
            "baseline_set": False,
            "baseline": baseline,
            "current": current,
            "ratios": deltas,
            "worse_count": worse,
        },
    )
