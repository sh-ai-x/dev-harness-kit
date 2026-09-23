#!/usr/bin/env python3
"""gate_dynamic.py — non-deterministic LLM-judge layer that decides
which CI gates to skip on babysit-pr iterations.

Sits on top of `lib/gates_state.py` (the schema SSOT) and
`lib/llm_judge.py` (the LLM invocation seam). On babysit-pr's
"not-first-push" iterations the helper inspects the diff + previous
verdicts + the gate catalog and recommends a per-gate skip set:

  - Critical gates (`review` / `security`) are protected by hard
    rules — never LLM-skipped when their `scope_globs` match the diff.
  - First-push (iteration == 1) is always deterministic — no skip.
  - LLM `confidence < 0.7` is vetoed (low-confidence rule).
  - `forced_run: true` (operator override) → never skip.

Decisions are cached per `head_sha` with a SHA-256 of `.dev-kit/gates.json`
invalidation tag. The audit trail at `.dev-kit/gate-dynamic/<sha>.json`
contains the full prompt, response, parsed scores, and reasoning.

CLI (`python -m lib.gate_dynamic`):

  select --head-sha SHA [--root PATH] [--dry-run] [--local]
                            # runs judge, prints + saves decision JSON
  load   <head_sha> [--root PATH]
                            # reads cached decision (or None)
  audit  [--root PATH] [--tail N]
                            # lists recent decisions newest-first

Standalone `bin/review-local.sh --dynamic-skip` invocation (no prior
`babysit-checks.json`) gracefully degrades: all confidences default
to 0.0, no gates skipped.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import fnmatch
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Dual-import so consumer installs that ship `lib/*.py` flat (no
# `__init__.py` in the consumer `lib/`) keep working.
try:
    from . import (
        gates_state,  # type: ignore
        llm_judge,  # type: ignore
    )
    from .atomic import atomic_write_json  # type: ignore
except ImportError:
    import gates_state  # noqa: E402
    import llm_judge  # noqa: E402
    from atomic import atomic_write_json  # noqa: E402

# Module-level logger for S1 (silent-exception fix). The wrapper
# `select_gates` body must `logger.exception(...)` on any failure
# so a silent fail-open is debuggable from logs; this logger is the
# single sink for the gate-dynamic layer's runtime errors.
logger = logging.getLogger(__name__)

# Audit-trail location, sibling of `.dev-kit/gates.json`.
DYNAMIC_AUDIT_DIR = Path(".dev-kit") / "gate-dynamic"

# Hard rules — confidence floor.
CONFIDENCE_FLOOR = 0.7

# Skip threshold for `gate_skippable`. The LLM judge emits a 0-10
# score; `>= SKIP_THRESHOLD` AND `confidence >= CONFIDENCE_FLOOR`
# together gate the skip. 7/10 maps to "pretty clearly safe to skip"
# without being too aggressive on marginal scores.
SKIP_THRESHOLD = 7.0

# Risk ceiling for `risk_level`. The LLM judge emits 0-10; a gate
# with risk_level > RISK_CEILING is never skipped regardless of how
# high gate_skippable or confidence scores are. Default 3.0 keeps
# "zero risk" (0-2 per the judge rubric) plus the borderline-low edge
# eligible for skip — a relaxed-but-bounded ceiling for a v1.1
# feature (per OE-1 philosophy). A MISSING `risk_level` key (partial
# LLM response) is handled separately at the call site via
# `MISSING_RISK_LEVEL_SENTINEL`, not via a 0.0 default — 0.0 is the
# safest possible score and would incorrectly PASS this rule, letting
# an incomplete judge response bypass the risk veto instead of
# failing closed.
#
# Named RISK_CEILING (was RISK_FLOOR pre-v1.1.1) because the value
# is an UPPER bound on risk: rule #6 fires when risk EXCEEDS it.
# The previous name invited future contributors to invert the
# comparison to `<` (parallel to `CONFIDENCE_FLOOR`, where the
# constant is genuinely a floor — confidence below the floor vetoes).
# Polarity-pinning test: `tests/test_gate_dynamic.py::TestHardRules
# ::test_risk_ceiling_polarity_pinned`.
RISK_CEILING = 3.0

# Sentinel used for `risk_level` when the LLM response omits the key
# entirely (as opposed to explicitly scoring it 0.0). Chosen above the
# judge's 0-10 scale so a missing key always fails `risk_level <=
# RISK_CEILING` and forces skip=False — fail-closed on incomplete LLM
# output, matching the CONFIDENCE_FLOOR / SKIP_THRESHOLD posture for
# the other two axes.
#
# Doubles as the dataclass default for `GateDecision.risk_level` so
# legacy cache entries written before v1.1 (no `risk_level` field) load
# without raising TypeError, and the sentinel value keeps rule #6's
# fail-closed veto intact for those entries.
MISSING_RISK_LEVEL_SENTINEL = 11.0

# Coerced-response sanity-check constants. A genuine judge response
# on a real diff almost never emits a high skip-score together with a
# low risk-level — that pair is the signature of a PR-body
# prompt-injected judge response (the attacker steers the LLM toward
# "every gate is skippable, nothing is risky"). The original v1.1
# check used an AND of the two thresholds; the v1.1.1 follow-up
# replaces it with a combined-score: `skip_score + (10 - risk_level)
# >= COERCED_RESPONSE_COMBINED_FLOOR`. An attacker must push BOTH
# axes to the extremes; lowering one alone is insufficient (closes
# the LLM01 follow-up finding where the AND check missed the
# sub-extreme `(skip=8.5, risk=1.5)` attack triple).
COERCED_RESPONSE_COMBINED_FLOOR = 17.0

# Near-max-skip coerced-response sanity check (v1.1.3 follow-up).
# Closes the LLM01 high finding where the combined-score check still
# let through the `(skip=9.99, risk=3.0)` attack triple: 9.99 + (10 - 3.0)
# = 16.99 < 17.0, so the combined-score check did NOT fire, and the
# skip predicate (`skip_score >= 7`, `confidence >= 0.7`,
# `risk_level <= RISK_CEILING`) all passed — SKIP granted on a coerced
# response. The signature of this attack is "skip_score pushed to
# near-max AND risk_level at the documented borderline-low edge".
# If skip_score is near max (>= 9.0) AND risk_level is at the ceiling
# (>= RISK_CEILING), force risk_level to the sentinel so rule #6 vetoes.
# Legitimate borderline-low skips have moderate skip_score (typically
# 7-8, well below 9.0) so the new check does not over-trigger.
COERCED_RESPONSE_NEAR_MAX_SKIP = 9.0

# Body truncation budget for `diff_sample` in the LLM prompt.
# ~2 KB is enough for the judge to ground its scope-discipline
# judgment without blowing the input-token budget on long diffs.
DIFF_SAMPLE_MAX_BYTES = 2048

# Stale-decision TTL. Decisions older than this are pruned on every
# `select_gates` call. 7 days matches the worktree-prune cadence so
# abandoned branches don't accumulate forever.
DYNAMIC_AUDIT_TTL_DAYS = 7

# Critical gates that the LLM is forbidden to skip when their scope
# matches the diff. Mirrors the `must_run` policy in the plan.
IN_SCOPE_GATES = frozenset({"review", "security"})


# ----------------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class GateContext:
    parent_pr: int
    head_sha: str
    iteration: int
    diff_stat: str
    diff_sample: str
    pr_body: Optional[str]
    previous_verdicts: dict          # gate_name -> Approve|Changes|Blocked|missing
    gate_catalog: dict               # parsed .dev-kit/gates.json (top-level shape)


@dataclasses.dataclass(frozen=True)
class GateDecision:
    gate_name: str
    skip: bool
    reasoning: str
    confidence: float                 # 0.0-1.0
    # Default sentinel keeps v1.0 cache entries loadable: when a legacy
    # JSON payload omits `risk_level`, `GateDecision(**payload)` succeeds
    # and rule #6 still vetoes (sentinel > RISK_CEILING → skip=False).
    risk_level: float = MISSING_RISK_LEVEL_SENTINEL  # 0.0-10.0, lower_is_better
    raw_score: dict = dataclasses.field(default_factory=dict)
    # A09 audit trail. Distinguishes the seven paths that can land
    # `risk_level` on the fail-closed sentinel:
    #   - "ok"                    — LLM returned a value in [0.0, 10.0].
    #   - "missing_key"           — LLM response omitted `risk_level`.
    #                               Sentinel applied at parse time.
    #   - "coerced_response"      — combined-score OR near-max-skip
    #                               sanity check fired; sentinel applied
    #                               as the fail-closed reaction.
    #   - "legacy_cache"          — `load_decision` clamped an out-of-
    #                               range cached value, OR an empty
    #                               `raw_score` paired with a skip-range
    #                               `risk_level` (cache-poisoning
    #                               signature), to the sentinel.
    #   - "coerced_response_cache" — cache-load combined-score OR near-
    #                               max-skip check fired; sentinel
    #                               applied at load time.
    #   - "llm_unavailable"       — LLM seam unreachable; emitted by
    #                               `_no_skip_decision`'s default when
    #                               `invoke_judge` returns empty.
    #   - "exception_fail_closed" — exception in `select_gates`; sentinel
    #                               applied by the top-level wrapper.
    # Empty string is treated as "ok" (backward compat with v1.1
    # audit JSON that did not record the field).
    audit_reason: str = "ok"


@dataclasses.dataclass(frozen=True)
class GateSkipDecision:
    head_sha: str
    decisions: tuple                  # tuple[GateDecision, ...]
    llm_raw: dict
    gates_hash: str                  # SHA-256 of gates.json at decision time
    decided_at_iso: str


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------

def is_gate_in_scope(gate_name: str, gate_entry: dict, diff_files: list) -> bool:
    """True iff any diff_file matches one of the gate's scope_globs.

    Pure helper — no I/O. Mirrors `fnmatch.fnmatch` semantics (not full
    gitignore). The `gates.<name>.scope_globs` field is `list[str]` per
    `lib/gates_state.DYNAMIC_FIELDS_DEFAULT`.
    """
    globs = gate_entry.get("scope_globs") or []
    if not globs or not diff_files:
        return False
    for path in diff_files:
        for glob in globs:
            if fnmatch.fnmatch(path, glob):
                return True
    return False


def hash_gates_state(root: Optional[Path] = None) -> str:
    """SHA-256 hex of `.dev-kit/gates.json`. Empty string if missing.

    Used as the cache-invalidation tag in `GateSkipDecision.gates_hash`.
    When the operator edits the file between iterations, the hash
    changes and the cached decision is treated as stale.
    """
    path = (root or Path(".")) / gates_state.STATE_REL_PATH
    if not path.exists():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def apply_hard_rules(
    context: GateContext,
    llm_decisions: list,
) -> list:
    """Pure: apply 6 bypass rules on top of LLM output.

    Returns a new list with `skip=False` overrides where hard rules fire.
    Hard rules (in order of precedence):
      1. iteration == 1 → all skip=False (first-push-deterministic)
      2. `forced_run: true` on the gate → skip=False (operator override)
      3. gate_name in {review, security} AND scope matches → skip=False
      4. confidence < CONFIDENCE_FLOOR → skip=False (low-confidence veto)
      5. `dynamic_eligible: false` (default) → skip=False (LLM-seam closed)
      6. risk_level > RISK_CEILING → skip=False (high-risk veto; lower_is_better)
    """
    out = []
    for dec in llm_decisions:
        gate_entry = (
            (context.gate_catalog or {}).get("gates", {}).get(dec.gate_name, {})
        )
        new_skip = dec.skip
        # Rule 1 — first-push-deterministic.
        if context.iteration <= 1:
            new_skip = False
        # Rule 2 — operator override.
        if gate_entry.get("forced_run"):
            new_skip = False
        # Rule 3 — critical gate in scope.
        # For the IN_SCOPE_GATES (review, security), an empty `scope_globs`
        # would defeat Rule #3 against the default config — an attacker who
        # reaches iteration ≥ 2 with a prompt-injection payload could skip
        # the critical gates because no operator-configured glob matches.
        # Treat empty scope for critical gates as "match everything" so
        # Rule #3 always fires for them.
        scope_globs = gate_entry.get("scope_globs") or []
        if not scope_globs and dec.gate_name in IN_SCOPE_GATES:
            scope_globs = ["**"]
        if (
            dec.gate_name in IN_SCOPE_GATES
            and is_gate_in_scope(dec.gate_name, {**gate_entry, "scope_globs": scope_globs}, _diff_files_from_stat(context))
        ):
            new_skip = False
        # Rule 4 — low-confidence veto.
        if dec.confidence < CONFIDENCE_FLOOR:
            new_skip = False
        # Rule 5 — dynamic_eligible opt-in. The default is False, meaning
        # "do NOT let the LLM judge touch this gate". An operator who
        # explicitly sets `dynamic_eligible=True` opts the gate in; an
        # operator who leaves it at the default expects the gate to be
        # immune to LLM-driven skips.
        if not gate_entry.get("dynamic_eligible", False):
            new_skip = False
        # Rule 6 — high-risk veto. risk_level is lower_is_better (0=safe,
        # 10=dangerous); a gate with risk above the ceiling is never
        # skipped regardless of how good the other scores look.
        if dec.risk_level > RISK_CEILING:
            new_skip = False
        if new_skip != dec.skip:
            out.append(dataclasses.replace(dec, skip=False))
        else:
            out.append(dec)
    return out


def _diff_files_from_stat(context: GateContext) -> list:
    """Best-effort: extract file paths from `git diff --stat` output.

    Used by `apply_hard_rules` rule #3. The full diff list lives in
    the parent PR (babysit-pr step 1 already fetches it via
    `gh pr view --json files`); this helper exists so the rule works
    without that data. Parsing `git diff --stat` line-by-line keeps
    the helper testable and pure.
    """
    files = []
    for line in (context.diff_stat or "").splitlines():
        # `git diff --stat` lines look like: " path/to/file.py | 12 ++--"
        if " | " not in line:
            continue
        path = line.split(" | ", 1)[0].strip()
        # Skip the leading space and the summary line (`3 files changed, ...`)
        if path and not path.endswith("changed,") and "|" in line:
            files.append(path)
    return files


# ----------------------------------------------------------------------------
# I/O — decision cache at .dev-kit/gate-dynamic/<sha>.json
# ----------------------------------------------------------------------------

def _audit_path(root: Path, head_sha: str) -> Path:
    """Resolve the audit-JSON path for a given head SHA.

    Defensive: a `--head-sha` value containing path-traversal sequences
    (e.g. `../../etc/foo`) would otherwise escape `.dev-kit/gate-dynamic/`
    and let `save_decision` write anywhere the operator's CLI can reach.
    Pin the resolved path under the audit dir.
    """
    audit_dir = (root / DYNAMIC_AUDIT_DIR).resolve()
    candidate = (audit_dir / f"{head_sha}.json").resolve()
    try:
        # Python 3.9+: Path.is_relative_to
        if not candidate.is_relative_to(audit_dir):
            raise ValueError(
                f"_audit_path: head_sha escapes audit dir "
                f"(audit_dir={audit_dir}, resolved={candidate})"
            )
    except AttributeError:
        # Defensive fallback for <3.9 — should never run on supported
        # versions (the project pins 3.12+), but assert the prefix
        # check manually rather than failing open.
        audit_dir_str = str(audit_dir)
        candidate_str = str(candidate)
        if not (candidate_str == audit_dir_str
                or candidate_str.startswith(audit_dir_str + "/")):
            raise ValueError(
                f"_audit_path: head_sha escapes audit dir "
                f"(audit_dir={audit_dir}, resolved={candidate})"
            )
    return candidate


def save_decision(decision: GateSkipDecision, root: Optional[Path] = None) -> Path:
    """Atomic-write the audit JSON. Returns the path written."""
    root = root or Path(".")
    payload = dataclasses.asdict(decision)
    # GateDecision tuples → list for JSON compat
    payload["decisions"] = list(payload["decisions"])
    path = _audit_path(root, decision.head_sha)
    atomic_write_json(path, payload)
    return path


def _clamp_risk_level_for_load(d: dict) -> dict:
    """S-1 + S-2 fix: validate + clamp `risk_level` on cache load.

    S-1 (range validation): a poisoned cache file with an
    out-of-range value (`risk_level=-1`, `risk_level=99`,
    `risk_level="high"`) would otherwise survive `GateDecision(**d)`
    and bypass rule #6: rule #6 checks `risk > RISK_CEILING`, so a
    negative value does not trigger the veto, and the cached
    `skip=True` survives the cache-hit re-application. Clamp any
    value outside `[0.0, 10.0]` (or non-numeric) to
    `MISSING_RISK_LEVEL_SENTINEL` so rule #6 fires and the gate
    fails closed. Tag the audit reason so operators can distinguish
    the legacy_cache path from missing_key / coerced_response /
    coerced_response_cache.

    S-2 (combined-score re-application): a poisoned cache entry with
    in-range but coerced values (`skip=True, gate_skippable=9,
    risk_level=0.5`) bypasses rule #6 but should have been caught
    by the LLM-parse-time combined-score check. An attacker who
    hand-crafts the cache JSON can skip that check entirely.
    Re-apply the combined-score check at load time using
    `raw_score["gate_skippable"]` + `risk_level`; if the combined-
    score trips, upgrade `risk_level` to the sentinel and tag
    `audit_reason="coerced_response_cache"` so operators can
    distinguish this path.
    """
    out = dict(d)  # do not mutate caller's dict
    rl_raw = out.get("risk_level")
    rl = None
    try:
        rl = float(rl_raw) if rl_raw is not None else MISSING_RISK_LEVEL_SENTINEL
        if not (0.0 <= rl <= 10.0):
            out["risk_level"] = MISSING_RISK_LEVEL_SENTINEL
            out["audit_reason"] = "legacy_cache"
        else:
            out["risk_level"] = rl
    except (TypeError, ValueError):
        out["risk_level"] = MISSING_RISK_LEVEL_SENTINEL
        out["audit_reason"] = "legacy_cache"

    # S-2: re-apply the combined-score coerced-response check at
    # load time. A legitimate cache entry where the original
    # LLM-parse-time check fired would have `risk_level ==
    # MISSING_RISK_LEVEL_SENTINEL`; if we see a cache entry with
    # in-range `risk_level` AND combined-score >= threshold,
    # the parse-time check was bypassed — clamp to sentinel.
    # Only re-apply when `raw_score["gate_skippable"]` is present
    # (legacy v1.1 cache entries may lack it).
    rs = out.get("raw_score") or {}
    try:
        gs = float(rs.get("gate_skippable", 0.0))
    except (TypeError, ValueError):
        gs = 0.0
    cur_rl = out.get("risk_level", MISSING_RISK_LEVEL_SENTINEL)
    if isinstance(cur_rl, (int, float)) and gs + (10.0 - cur_rl) >= COERCED_RESPONSE_COMBINED_FLOOR:
        out["risk_level"] = MISSING_RISK_LEVEL_SENTINEL
        out["audit_reason"] = "coerced_response_cache"
    # S-3 (v1.1.3 follow-up): re-apply the near-max-skip check at
    # load time. Mirrors the parse-time check above — closes the LLM01
    # high finding for cache-poisoned entries with `raw_score` empty
    # (which makes `gs=0.0` in the S-2 check above, so combined-score
    # never trips even when risk_level is at the borderline-low edge).
    # Only fires when gs is itself at the near-max threshold (defense-
    # in-depth: requires an attacker who both poisoned gs to >= 9 AND
    # chose a coerced risk_level).
    elif (
        isinstance(cur_rl, (int, float))
        and gs >= COERCED_RESPONSE_NEAR_MAX_SKIP
        and cur_rl >= RISK_CEILING
    ):
        out["risk_level"] = MISSING_RISK_LEVEL_SENTINEL
        out["audit_reason"] = "coerced_response_cache"
    # S-4 (v1.1.3 follow-up): empty raw_score + risk in skip range is
    # anomalous. A legitimate cache entry always writes
    # `raw_score={"gate_skippable": X, ...}`; an empty `raw_score`
    # paired with `risk_level <= RISK_CEILING` is the A06/A08 cache-
    # poisoning signature (attacker chose risk_level at the borderline-
    # low edge AND omitted `gate_skippable` to make the combined-score
    # check fall through with `gs=0.0`). Clamp to sentinel so rule #6
    # vetoes; legitimate v1.1 cache entries with `gate_skippable=7+
    # risk_level=3` always have a populated `raw_score` so they are
    # unaffected.
    elif (
        isinstance(cur_rl, (int, float))
        and not rs
        and cur_rl <= RISK_CEILING
    ):
        out["risk_level"] = MISSING_RISK_LEVEL_SENTINEL
        out["audit_reason"] = "legacy_cache"
    return out


def load_decision(
    head_sha: str,
    root: Optional[Path] = None,
) -> Optional[GateSkipDecision]:
    """Read cached decision or None if missing / invalidated.

    Invalidation: if the on-disk `gates_hash` doesn't match the
    current `.dev-kit/gates.json` hash, return None so callers
    re-run `select_gates`.

    Out-of-range `risk_level` values are clamped to
    `MISSING_RISK_LEVEL_SENTINEL` (S-1 fix) so a poisoned cache file
    cannot bypass rule #6 on cache-hit re-application.
    """
    root = root or Path(".")
    path = _audit_path(root, head_sha)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("gates_hash") != hash_gates_state(root):
        return None
    decisions = tuple(
        GateDecision(**_clamp_risk_level_for_load(d))
        for d in payload.get("decisions", [])
    )
    return GateSkipDecision(
        head_sha=payload["head_sha"],
        decisions=decisions,
        llm_raw=payload.get("llm_raw", {}),
        gates_hash=payload.get("gates_hash", ""),
        decided_at_iso=payload.get("decided_at_iso", ""),
    )


def prune_stale(root: Optional[Path] = None, ttl_days: int = DYNAMIC_AUDIT_TTL_DAYS) -> int:
    """Remove audit JSON files older than `ttl_days`. Returns count removed.

    Called from `select_gates` on every invocation. Cheap (small
    per-worktree dir, low file count) — keeps the audit dir bounded
    for long-running branches outside worktrees.
    """
    root = root or Path(".")
    audit_dir = root / DYNAMIC_AUDIT_DIR
    if not audit_dir.is_dir():
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for entry in audit_dir.iterdir():
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ----------------------------------------------------------------------------
# LLM invocation
# ----------------------------------------------------------------------------

def build_user_prompt(context: GateContext) -> str:
    """Render the volatile body the LLM judge sees.

    System prompt is built by `lib/llm_judge.call_judge` from the
    per-dim axes. This helper renders the diff/PR/catalog body and
    is appended after the rubric template.
    """
    verdict_lines = []
    for gate_name, verdict in (context.previous_verdicts or {}).items():
        verdict_lines.append(f"  {gate_name}: {verdict}")
    verdicts_str = "\n".join(verdict_lines) if verdict_lines else "  (none)"

    catalog_str = json.dumps(context.gate_catalog, indent=2, sort_keys=True)

    parts = [
        f"PR #{context.parent_pr} — head_sha={context.head_sha} — iteration={context.iteration}",
        "",
        "PREVIOUS VERDICTS:",
        verdicts_str,
        "",
        "DIFF STAT:",
        (context.diff_stat or "").strip() or "(empty)",
        "",
        "DIFF SAMPLE:",
        (context.diff_sample or "").strip()[:DIFF_SAMPLE_MAX_BYTES] or "(empty)",
        "",
        "PR BODY:",
        (context.pr_body or "").strip() or "(empty)",
        "",
        "GATE CATALOG (.dev-kit/gates.json):",
        catalog_str,
        "",
        "For each gate, decide skip/no-skip. Respond ONLY with the JSON "
        "object described in the system prompt.",
    ]
    return "\n".join(parts)


def invoke_judge(context: GateContext, project_root: Path) -> Optional[dict]:
    """Call the LLM judge. Returns parsed scores dict or None on failure.

    Mirrors `lib/push_intent_judge.py:run` invocation pattern. Loads
    config via `lib/llm_judge.load_config`, substitutes the
    per-dim prompt template, calls `call_judge`.

    Returns None on:
      - missing API key (config error)
      - missing prompt template
      - HTTP / parse failure (graceful degradation)

    The caller (select_gates) treats None as "no decision" → no gates
    skipped.
    """
    cfg = llm_judge.load_config(project_root)
    if not cfg.get("api_key"):
        return None

    template = llm_judge.format_prompt(
        project_root, "judge-gate-dynamic.md", {},
    )
    if not template:
        return None

    user_body = build_user_prompt(context)
    full_prompt = f"{template}\n\n---\n\n{user_body}"

    try:
        result = llm_judge.call_judge(
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            prompt=full_prompt,
            axes=llm_judge.DIM_AXES["gate_dynamic"],
            dim="gate_dynamic",
            base_url=cfg.get("base_url", "https://api.minimax.io/anthropic"),
            # The judge rubric (eval/prompts/judge-gate-dynamic.md) and the
            # PR title both promise `temperature=0` for deterministic
            # decision stability. Call_judge defaults to 1.0; pin it here so
            # the cache invalidation story (same SHA + gates_hash -> same
            # decision) holds across operators.
            temperature=0.0,
        )
    except Exception:
        return None
    return result


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------

def select_gates(
    context: GateContext,
    project_root: Optional[Path] = None,
    *,
    dry_run: bool = False,
    local: bool = False,
) -> GateSkipDecision:
    """Main entry. Run LLM judge, apply hard rules, cache + return decision.

    Behavior:
      1. Prune stale audit files (TTL = 7 days).
      2. If a cached decision exists for `context.head_sha` AND its
         `gates_hash` matches the current gates.json, return it.
      3. Else invoke the LLM judge. If parse fails / LLM unavailable,
         return a "no skip" decision (all gates skip=False).
      4. Apply hard rules (`apply_hard_rules`).
      5. Build `GateSkipDecision` + save (unless `dry_run`).
      6. Return the decision.
    """
    root = project_root or Path(".")

    # 0. Local mode — skip the LLM judge entirely (no network). Used by
    # `bin/review-local.sh` for offline iterations; returns a "no skip"
    # decision (all gates skip=False), which the orchestrator then
    # collapses into "every gate runs" — the safe default.
    if local:
        from gates_state import VALID_GATE_KEYS  # local import to avoid cycle
        decisions = tuple(
            GateDecision(
                gate_name=g,
                skip=False,
                reasoning="local mode: judge skipped, no LLM call",
                confidence=0.0,
                risk_level=0.0,
                raw_score={},
            )
            for g in VALID_GATE_KEYS
        )
        return GateSkipDecision(
            head_sha=context.head_sha,
            decisions=decisions,
            llm_raw={"scores": {}, "raw": ""},
            gates_hash=hash_gates_state(root),
            decided_at_iso=_now_utc_iso(),
        )

    # 1. Prune stale audit files.
    prune_stale(root)

    # A10 fix: wrap the body in a top-level fail-closed barrier.
    # Any exception in prune_stale / load_decision / apply_hard_rules /
    # save_decision falls back to `_no_skip_decision` so a crash in
    # the gate-dynamic layer cannot crash the babysit-pr loop.
    try:
        # 2. Cache hit?
        cached = load_decision(context.head_sha, root)
        if cached is not None:
            # Re-apply hard rules on cached decisions. Without this, a
            # cached `skip=True` from a pre-rule-#6 entry would survive
            # a rule upgrade and bypass the new veto — the v1.0 cache
            # short-circuit was the A01/A06 attack path the security
            # judge flagged. The judge is NOT re-invoked (no network);
            # the hard rules are deterministic and pure.
            return GateSkipDecision(
                head_sha=cached.head_sha,
                decisions=tuple(apply_hard_rules(context, list(cached.decisions))),
                llm_raw=cached.llm_raw,
                gates_hash=cached.gates_hash,
                decided_at_iso=cached.decided_at_iso,
            )

        # 3. Invoke LLM.
        raw = invoke_judge(context, root)
        if not raw:
            # Graceful degradation — empty decision, no gates skipped.
            return _no_skip_decision(context, root)

        scores = raw.get("scores") or {}

        # Build per-gate LLM decisions. Each gate in VALID_GATE_KEYS gets a
        # decision; missing confidence defaults to 0.0 (fails the floor).
        from gates_state import VALID_GATE_KEYS  # local import to avoid cycle
        llm_decisions = []
        for gate_name in VALID_GATE_KEYS:
            # gate_skippable maps to skip; confidence is its own field.
            # Judge rubric (eval/prompts/judge-gate-dynamic.md): both axes are
            # raw 0-10. Normalize confidence to 0-1 so the CONFIDENCE_FLOOR
            # (= 0.7) comparison is on the same scale; otherwise the floor
            # is effectively unreachable and rule #4 (low-confidence veto)
            # never fires.
            skip_score = float(scores.get("gate_skippable", 0.0))
            confidence_raw = float(scores.get("confidence", 0.0))
            confidence = confidence_raw / 10.0
            # A missing `risk_level` key (partial LLM response) must
            # fail closed: defaulting to 0.0 would be the SAFEST possible
            # score and would incorrectly PASS the risk_level <=
            # RISK_CEILING check below, letting an incomplete judge
            # response bypass the risk veto. Use
            # MISSING_RISK_LEVEL_SENTINEL (11.0, above the judge's 0-10
            # scale) so a missing key always vetoes the skip via rule
            # #6, same fail-closed posture as the other two axes' floors.
            risk_level_raw = scores.get("risk_level")
            if risk_level_raw is None:
                risk_level = MISSING_RISK_LEVEL_SENTINEL
                audit_reason = "missing_key"
            else:
                risk_level = float(risk_level_raw)
                audit_reason = "ok"
            # LLM01 follow-up: combined-score coerced-response sanity
            # check. A genuine judge response on a real diff almost
            # never emits a high skip-score together with a low
            # risk-level — that pair is the signature of a PR-body
            # prompt-injected judge response. The v1.1 AND-only check
            # missed the sub-extreme `(skip=8.5, risk=1.5)` attack
            # triple; the v1.1.1 combined-score check requires BOTH
            # axes to be pushed toward extremes simultaneously. An
            # attacker must push `skip + (10 - risk)` above
            # `COERCED_RESPONSE_COMBINED_FLOOR`; lowering either axis
            # alone is insufficient. Force `risk_level` to the
            # sentinel so rule #6 vetoes the skip and tag the audit
            # reason so operators can distinguish the
            # coerced-response path from missing_key / legacy_cache.
            if (
                skip_score + (10.0 - risk_level)
                >= COERCED_RESPONSE_COMBINED_FLOOR
            ):
                risk_level = MISSING_RISK_LEVEL_SENTINEL
                audit_reason = "coerced_response"
            # v1.1.3 follow-up: near-max-skip coerced-response sanity
            # check. Closes the LLM01 high finding where the combined-
            # score check missed the `(skip=9.99, risk=3.0)` attack
            # triple. A genuine judge response on a clean diff lands
            # skip_score in the 7-8 range (the SKIP_THRESHOLD zone);
            # an attacker driving skip_score to >= 9.0 AND keeping
            # risk_level at the documented borderline-low edge is the
            # signature of a coerced response. Force risk_level to the
            # sentinel so rule #6 vetoes.
            elif (
                skip_score >= COERCED_RESPONSE_NEAR_MAX_SKIP
                and risk_level >= RISK_CEILING
            ):
                risk_level = MISSING_RISK_LEVEL_SENTINEL
                audit_reason = "coerced_response"
            # Skip iff all three: skip_score >= SKIP_THRESHOLD AND
            # confidence >= CONFIDENCE_FLOOR AND risk_level <= RISK_CEILING.
            skip = (
                skip_score >= SKIP_THRESHOLD
                and confidence >= CONFIDENCE_FLOOR
                and risk_level <= RISK_CEILING
            )
            llm_decisions.append(
                GateDecision(
                    gate_name=gate_name,
                    skip=skip,
                    reasoning=f"llm: gate_skippable={skip_score:.1f} confidence_raw={confidence_raw:.1f} (normalized={confidence:.2f}) risk_level={risk_level:.1f}",
                    confidence=confidence,
                    risk_level=risk_level,
                    raw_score=scores,
                    audit_reason=audit_reason,
                )
            )

        # 4. Apply hard rules.
        final = apply_hard_rules(context, llm_decisions)

        # 5. Build decision payload.
        decision = GateSkipDecision(
            head_sha=context.head_sha,
            decisions=tuple(final),
            llm_raw={"scores": scores, "raw": raw.get("raw", "")},
            gates_hash=hash_gates_state(root),
            decided_at_iso=_now_utc_iso(),
        )

        # 6. Save (unless dry-run).
        if not dry_run:
            save_decision(decision, root)

        return decision
    except Exception:
        # A10 fix + S-1 fix: any exception in the gate-dynamic body
        # must fail closed, not propagate. `select_gates` is called
        # from babysit-pr / bin/review-local.sh / interactive flows;
        # a crash here would crash the calling loop. The no-skip
        # decision preserves all six hard rules (every gate runs)
        # — the safest possible default. S-1 fix: log the exception
        # via `logger.exception(...)` so a silent fail-open is
        # debuggable from logs, and tag the decision's audit_reason
        # as `exception_fail_closed` so operators can distinguish
        # this path from a healthy llm-unavailable decision.
        logger.exception(
            "select_gates body raised; failing closed to no-skip decision"
        )
        return _no_skip_decision(
            context, root,
            reason="select_gates exception",
            audit_reason="exception_fail_closed",
        )


def _no_skip_decision(
    context: GateContext,
    root: Path,
    *,
    reason: str = "llm unavailable",
    audit_reason: str = "llm_unavailable",
) -> GateSkipDecision:
    """Build a deterministic no-skip decision.

    `reason` controls the per-decision reasoning string; `audit_reason`
    is the `GateDecision.audit_reason` value, distinct from the
    reasoning string. The default (`"llm unavailable"`) is the
    graceful-degradation path when the LLM is unreachable. The
    exception-fail-closed path passes `audit_reason="exception_fail_closed"`
    so operators can tell a crashed gate-dynamic call apart from a
    healthy no-skip path (S-1 fix).
    """
    from gates_state import VALID_GATE_KEYS
    decisions = tuple(
        GateDecision(
            gate_name=g,
            skip=False,
            reasoning=f"{reason}; defaulting to no-skip",
            confidence=0.0,
            risk_level=0.0,
            raw_score={},
            audit_reason=audit_reason,
        )
        for g in VALID_GATE_KEYS
    )
    return GateSkipDecision(
        head_sha=context.head_sha,
        decisions=decisions,
        llm_raw={"scores": {}, "raw": "", "note": audit_reason},
        gates_hash=hash_gates_state(root),
        decided_at_iso=_now_utc_iso(),
    )


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="dynamic gate-selection LLM judge (lib/gate_dynamic)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sel = sub.add_parser("select", help="run the judge for one head_sha")
    sel.add_argument("--head-sha", required=True)
    sel.add_argument("--root", default=None)
    sel.add_argument("--dry-run", action="store_true")
    sel.add_argument(
        "--local",
        action="store_true",
        help="skip the LLM judge (no network); return a no-skip decision. "
             "Used by bin/review-local.sh for offline iterations.",
    )

    apply_p = sub.add_parser(
        "apply",
        help="bake the judge's recommendation into gates.json "
             "(sets forced_run: true for gates the judge said should NOT be skipped)",
    )
    apply_p.add_argument("--head-sha", required=True)
    apply_p.add_argument("--root", default=None)
    apply_p.add_argument("--dry-run", action="store_true")

    load_p = sub.add_parser("load", help="read a cached decision")
    load_p.add_argument("head_sha")
    load_p.add_argument("--root", default=None)

    audit_p = sub.add_parser("audit", help="list recent decisions newest-first")
    audit_p.add_argument("--root", default=None)
    audit_p.add_argument("--tail", type=int, default=20)

    args = parser.parse_args(argv)
    if args.command == "select":
        return _cli_select(args)
    if args.command == "apply":
        return _cli_apply(args)
    if args.command == "load":
        return _cli_load(args)
    if args.command == "audit":
        return _cli_audit(args)
    return 1


def _cli_select(args) -> int:
    root = Path(args.root) if args.root else Path(".")
    ctx = GateContext(
        parent_pr=0,
        head_sha=args.head_sha,
        iteration=2,
        diff_stat="",
        diff_sample="",
        pr_body=None,
        previous_verdicts={},
        gate_catalog=_read_gate_catalog(root),
    )
    decision = select_gates(ctx, root, dry_run=args.dry_run, local=args.local)
    payload = {
        "head_sha": decision.head_sha,
        "decisions": [dataclasses.asdict(d) for d in decision.decisions],
        "gates_hash": decision.gates_hash,
        "decided_at_iso": decision.decided_at_iso,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cli_apply(args) -> int:
    """Bake the judge's recommendation into gates.json.

    For each gate the judge said should NOT be skipped
    (`skip=False`, `confidence >= CONFIDENCE_FLOOR`), set
    `forced_run: true` so future iterations honor the override.
    The decision must already exist (run `select` first) and the
    cache must be valid (gates.json unchanged since decision).

    Returns 1 + JSON error if the decision is missing/invalid.
    """
    root = Path(args.root) if args.root else Path(".")
    decision = load_decision(args.head_sha, root)
    if decision is None:
        print(json.dumps({
            "error": "no valid cached decision — run `select` first",
            "head_sha": args.head_sha,
        }))
        return 1

    # Read current state; write back with forced_run overrides.
    state = _read_gate_catalog(root)
    overrides = {}
    for d in decision.decisions:
        if not d.skip and d.confidence >= CONFIDENCE_FLOOR:
            overrides[d.gate_name] = True
    for gate_name in overrides:
        entry = state["gates"].get(gate_name, {})
        entry["forced_run"] = True
        state["gates"][gate_name] = entry

    if not args.dry_run:
        gates_state.write_state(state, root)

    print(json.dumps({
        "head_sha": args.head_sha,
        "applied_overrides": overrides,
        "gates_hash_after": hash_gates_state(root),
    }, indent=2, sort_keys=True))
    return 0


def _cli_load(args) -> int:
    root = Path(args.root) if args.root else Path(".")
    decision = load_decision(args.head_sha, root)
    if decision is None:
        print(json.dumps({"error": "no cached decision", "head_sha": args.head_sha}))
        return 1
    payload = {
        "head_sha": decision.head_sha,
        "decisions": [dataclasses.asdict(d) for d in decision.decisions],
        "gates_hash": decision.gates_hash,
        "decided_at_iso": decision.decided_at_iso,
        "llm_raw": decision.llm_raw,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cli_audit(args) -> int:
    root = Path(args.root) if args.root else Path(".")
    audit_dir = root / DYNAMIC_AUDIT_DIR
    if not audit_dir.is_dir():
        print(json.dumps({"decisions": []}))
        return 0
    files = sorted(
        (f for f in audit_dir.iterdir() if f.suffix == ".json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    files = files[: args.tail]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "head_sha": data.get("head_sha"),
            "decided_at_iso": data.get("decided_at_iso"),
            "path": str(f),
        })
    print(json.dumps({"decisions": out}, indent=2, sort_keys=True))
    return 0


def _read_gate_catalog(root: Path) -> dict:
    """Best-effort read of `.dev-kit/gates.json` for the CLI context."""
    try:
        return gates_state.read_state(root)
    except gates_state.ValidationError:
        return {"gates": {}}


if __name__ == "__main__":
    sys.exit(main())
