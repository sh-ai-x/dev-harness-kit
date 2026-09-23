"""intent_metrics.py — metrics emitter for the request classifier (issue #845).

Companion to ``lib/request_intent.py``. Emits the seven metrics the
issue brief requires:

  * ``time_to_intent``            — wall-clock from prompt to
                                    accepted ``Intent`` (seconds).
  * ``intent_correction_rate``    — fraction of accepted intents that
                                    were edited before acceptance.
  * ``false_cut_rate``            — fraction of cuts that were rolled
                                    back by an early ``worktree remove``.
  * ``missed_cut_rate``           — fraction of
                                    ``mode=implementation`` classifications
                                    that did NOT result in a cut
                                    (rejected intent, failure, etc).
  * ``client_parity``             — fraction of ``(prompt, client)``
                                    pairs that produced identical
                                    classification fields (excluding
                                    ``request_id`` / ``prompt_hash``).
  * ``cut_failure_rate``          — fraction of cut attempts that
                                    failed (helper exception, git rc!=0).
  * ``intent_survival_rate``      — fraction of accepted intents that
                                    survive into the feature worktree
                                    without a fresh ``Intent`` record
                                    being written.

The emitter is intentionally a thin wrapper over a single sink —
``.dev-kit/cache/intent-metrics.jsonl`` — so a downstream reducer
can compute ratios. The sink path is overridable via
``INTENT_METRICS_SINK`` (used by tests).

Definitions live alongside the emitter (not as prose elsewhere)
per the brief's "Definitions live alongside the metrics emitter"
clause. Each metric carries its unit + scale on the JSON event.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from atomic import atomic_write_text  # noqa: F401  (kept for parity with siblings)

# Default sink — gitignored via the existing ``.dev-kit/`` rule.
DEFAULT_SINK = Path(".dev-kit/cache/intent-metrics.jsonl")

# Env-var override so tests can point at a tmp path without
# polluting the real one. Empty string disables emission.
_SINK_ENV = "INTENT_METRICS_SINK"


@dataclass(frozen=True)
class MetricEvent:
    """One row in the metrics sink.

    ``metric`` is the metric name. ``value`` is the numeric value
    (or fraction, in [0.0, 1.0]). ``tags`` carries ``client`` /
    ``mode`` / ``request_id`` so a downstream reducer can slice.
    ``unit`` carries ``seconds`` or ``ratio`` (or any other label
    the emitter chooses — the schema is open). ``ts`` is the
    wall-clock time the event was emitted.
    """

    metric: str
    value: float
    tags: dict = field(default_factory=dict)
    unit: str = "ratio"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


# Threading lock so concurrent classifier callers don't interleave
# their writes. The sink is a JSONL append — interleaving would
# corrupt the file. A lock is the cheap-and-correct fix; in this
# codebase the request_intent path is invoked from hook contexts
# (UserPromptSubmit) which are themselves single-threaded per
# session, so contention is rare.
_LOCK = threading.Lock()


def _sink_path() -> Optional[Path]:
    """Resolve the active sink path. None disables emission."""
    override = os.environ.get(_SINK_ENV)
    if override is None:
        return DEFAULT_SINK
    if not override:
        return None
    return Path(override)


def emit(
    metric: str,
    value: float,
    *,
    tags: Optional[dict] = None,
    unit: str = "ratio",
) -> Optional[Path]:
    """Append one ``MetricEvent`` to the sink.

    Returns the path written (for test assertions) or ``None`` when
    emission is disabled (``INTENT_METRICS_SINK=""``). Errors
    during write are swallowed — the emitter must never break the
    classifier path.
    """
    sink = _sink_path()
    if sink is None:
        return None
    event = MetricEvent(
        metric=metric,
        value=float(value),
        tags=tags or {},
        unit=unit,
        ts=time.time(),
    )
    payload = json.dumps(event.to_dict(), sort_keys=True)
    try:
        with _LOCK:
            sink.parent.mkdir(parents=True, exist_ok=True)
            with sink.open("a", encoding="utf-8") as fh:
                fh.write(payload + "\n")
    except OSError:
        return None
    return sink


def read_events(sink: Path) -> list:
    """Parse a metrics sink back into a list of ``MetricEvent``.

    Used by tests and by the (future) reducer. Skips blank lines
    and lines that fail to parse — never raises so a corrupt row
    can't crash a downstream dashboard.
    """
    if not sink.exists():
        return []
    out = []
    for line in sink.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        try:
            out.append(MetricEvent(
                metric=data["metric"],
                value=float(data["value"]),
                tags=data.get("tags") or {},
                unit=data.get("unit") or "ratio",
                ts=float(data.get("ts") or 0.0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# Metric definitions. Kept here (not in prose elsewhere) per the
# issue brief's "Definitions live alongside the metrics emitter"
# clause. Each entry documents what the metric is, the unit,
# and what direction is good.

METRIC_DEFINITIONS = {
    "time_to_intent": {
        "unit": "seconds",
        "description": (
            "Wall-clock seconds from raw prompt intake to accepted "
            "Intent (mode=accepted) in .dev-kit/round-0/intent.md. "
            "Lower is better; expect 30-120s when the originator "
            "edits the proto-spec before accepting."
        ),
    },
    "intent_correction_rate": {
        "unit": "ratio",
        "description": (
            "Fraction of accepted intents whose Markdown body was "
            "modified after first capture (i.e. the classifier "
            "draft was corrected by the originator). 0.0-0.5 "
            "expected for typical prompts."
        ),
    },
    "false_cut_rate": {
        "unit": "ratio",
        "description": (
            "Fraction of cut_worktree calls later reverted by an "
            "early worktree remove (the user realized the cut was "
            "wrong). Should trend toward 0.0; rising signals the "
            "classifier is too eager to cut."
        ),
    },
    "missed_cut_rate": {
        "unit": "ratio",
        "description": (
            "Fraction of mode=implementation classifications that "
            "did NOT result in a cut (rejected intent, helper "
            "failure, etc.). 0.0-0.3 expected; rising signals "
            "the intent gate is too strict or the classifier is "
            "false-promoting to implementation."
        ),
    },
    "client_parity": {
        "unit": "ratio",
        "description": (
            "Fraction of (prompt, client) pairs that produced "
            "identical fields across Claude and Codex adapters "
            "(excluding request_id and prompt_hash). Target 1.0; "
            "any drift indicates a classifier regression that "
            "broke parity between clients."
        ),
    },
    "cut_failure_rate": {
        "unit": "ratio",
        "description": (
            "Fraction of cut_worktree attempts that raised or "
            "exited non-zero. Should be near 0.0; rising signals "
            "either a git-worktree contract change or repo state "
            "drift (dirty main, missing remote, etc.)."
        ),
    },
    "intent_survival_rate": {
        "unit": "ratio",
        "description": (
            "Fraction of accepted intents that survived into the "
            "feature worktree unchanged (no Intent rewrite, no "
            "decision_mode flip). Target 0.7-0.9; a drop means "
            "the classifier keeps re-writing intent after every "
            "client adapter call."
        ),
    },
}


__all__ = [
    "DEFAULT_SINK",
    "METRIC_DEFINITIONS",
    "MetricEvent",
    "emit",
    "read_events",
]
