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
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gates_state  # noqa: E402
import llm_judge  # noqa: E402
from atomic import atomic_write_json  # noqa: E402

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
# with risk_level > RISK_FLOOR is never skipped regardless of how high
# gate_skippable or confidence scores are. Default 3.0 means "low risk
# only" — a relaxed ceiling for a v1.1 feature (per OE-1 philosophy).
# This also prevents the 0.0 default (no LLM response) from accidentally
# bypassing the risk rule on the first legitimate call.
RISK_FLOOR = 3.0

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
    risk_level: float                # 0.0-10.0, lower_is_better
    raw_score: dict


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
      6. risk_level > RISK_FLOOR → skip=False (high-risk veto; lower_is_better)
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
        if dec.risk_level > RISK_FLOOR:
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


def load_decision(
    head_sha: str,
    root: Optional[Path] = None,
) -> Optional[GateSkipDecision]:
    """Read cached decision or None if missing / invalidated.

    Invalidation: if the on-disk `gates_hash` doesn't match the
    current `.dev-kit/gates.json` hash, return None so callers
    re-run `select_gates`.
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
        GateDecision(**d) for d in payload.get("decisions", [])
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

    # 2. Cache hit?
    cached = load_decision(context.head_sha, root)
    if cached is not None:
        return cached

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
        risk_level = float(scores.get("risk_level", 0.0))
        # Skip iff all three: skip_score >= SKIP_THRESHOLD AND
        # confidence >= CONFIDENCE_FLOOR AND risk_level <= RISK_FLOOR.
        skip = (
            skip_score >= SKIP_THRESHOLD
            and confidence >= CONFIDENCE_FLOOR
            and risk_level <= RISK_FLOOR
        )
        llm_decisions.append(
            GateDecision(
                gate_name=gate_name,
                skip=skip,
                reasoning=f"llm: gate_skippable={skip_score:.1f} confidence_raw={confidence_raw:.1f} (normalized={confidence:.2f}) risk_level={risk_level:.1f}",
                confidence=confidence,
                risk_level=risk_level,
                raw_score=scores,
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


def _no_skip_decision(context: GateContext, root: Path) -> GateSkipDecision:
    """Build a deterministic no-skip decision (LLM unavailable path)."""
    from gates_state import VALID_GATE_KEYS
    decisions = tuple(
        GateDecision(
            gate_name=g,
            skip=False,
            reasoning="llm unavailable; defaulting to no-skip",
            confidence=0.0,
            risk_level=0.0,
            raw_score={},
        )
        for g in VALID_GATE_KEYS
    )
    return GateSkipDecision(
        head_sha=context.head_sha,
        decisions=decisions,
        llm_raw={"scores": {}, "raw": "", "note": "llm_unavailable"},
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
