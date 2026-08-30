"""lib/analysis_core — single analysis engine behind the 6 reasoning skills.

The 6 SKILL.md bodies (review/security/inspect/audit/prune/refactor) used
to carry their own fan-out playbook, verifier prompt, and renderer. With
the next-generation-model absorb trend that playbook is wasted tokens —
the model already knows how to do precise code review. What it does NOT
have is a deterministic evidence format, a dimensions registry, a
false-positive filter, and a uniform diff-suggestion shape. That's what
this package is.

Public API:
    run_analysis(dimensions, mode, paths, candidates=None) -> AnalysisResult

`candidates` is the list of per-dimension expert outputs the parent skill
has already gathered (typically by fanning out Agent calls). Passing them
in keeps the engine deterministic and testable; the SKILL.md body just
hands them off after the parallel fan-out step.
"""
from __future__ import annotations

from .cross_validate import (  # noqa: F401
    ESCALATE_VARIANCE_THRESHOLD,
    cross_validate_scores,
)
from .dimensions import REGISTRY, Dimension, get, group, resolve  # noqa: F401
from .evidence import (  # noqa: F401
    SEVERITY_ORDER,
    Evidence,
    Severity,
    Verdict,
    from_dict,
    parse_candidate,
    to_dict,
)
from .fp_filter import (  # noqa: F401
    apply_verifier,
    dedupe,
    deterministic_filter,
    threshold_by_mode,
)
from .runner import (  # noqa: F401
    AnalysisResult,
    emit_suggested_diffs,
    mask_secrets,
    render_markdown,
    run_analysis,
)
