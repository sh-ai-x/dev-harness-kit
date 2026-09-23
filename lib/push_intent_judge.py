#!/usr/bin/env python3
"""push_intent_judge.py — Pre-push LLM-judge CLI for commit intent.

Used by `.githooks/pre-push` (opt-in via `DEV_KIT_PUSH_INTENT=1`). Reads
the commit message + `git diff --stat` summary of the tip commit and
asks the LLM judge whether the change has value/intent (the four
value/meaning axes VM-1..4 from `judge-code-sanity.md`).

Outputs a single ``VERDICT=<OK|DRIFT_WARNING|ROT> REASON="..."`` line
to stdout and an exit code suitable for the pre-push hook:

  exit 0  -> OK
  exit 1  -> DRIFT_WARNING or ROT (pre-push should block)
  exit 2  -> configuration error (missing API key, missing prompt
              template) — pre-push should also block and surface the
              error loudly to the operator.

The CLI is intentionally side-effect-free aside from stdout and exit
code: no file writes, no network beyond the single LLM call, no
dependency on the harness runner. Keep it that way so the pre-push
hook can call it from a minimal shell environment.

Why only the 4 value/meaning axes and not the full 20-checkbox
rubric? The full rubric is run by the dedicated CI maintenance gate
(see `eval/prompts/judge-maintenance.md` +
`.github/workflows/maintenance.yml`). The pre-push hook is the fast,
local, single-purpose commit-value signal — keep it small.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Dual-import so consumer installs that ship `lib/*.py` flat (no
# `__init__.py` in the consumer `lib/`) keep working.
try:
    from . import llm_judge  # type: ignore
except ImportError:
    import llm_judge  # type: ignore  # noqa: E402

PUSH_INTENT_AXES = llm_judge.DIM_AXES["push_intent"]  # 4-axis VM-1..4


def _format_user_prompt(
    *,
    commit_message: str,
    diff_stat: str,
    diff_sample: str,
) -> str:
    """Render the user-facing prompt body fed to the LLM judge.

    The system prompt is built by ``llm_judge.call_judge`` from the
    per-dim axes; this function only renders the body the judge sees.
    Volatile fields (commit message, diff stat) go to the tail per
    session-hygiene rule #3 — but in practice the entire body is
    volatile, so we keep it short.
    """
    parts = []
    if commit_message.strip():
        parts.append(f"COMMIT MESSAGE:\n```\n{commit_message.strip()}\n```")
    if diff_stat.strip():
        parts.append(f"DIFF STAT:\n```\n{diff_stat.strip()}\n```")
    if diff_sample.strip():
        # Trim to ~2 KB to keep the prompt bounded.
        sample = diff_sample.strip()
        if len(sample) > 2048:
            sample = sample[:2048] + "\n... (truncated)"
        parts.append(f"DIFF SAMPLE:\n```\n{sample}\n```")
    parts.append(
        "Score the commit against the 4 axes above. Respond ONLY with "
        "the JSON object described in the system prompt."
    )
    return "\n\n".join(parts)


def _worst_axis_reason(scores: dict) -> str:
    """Pick the worst-scoring axis and render a one-line REASON string.

    Returns "<axis>=<score>" (e.g. ``scope_discipline=2.0``) so the
    terminal output is unambiguous and under 100 chars.
    """
    if not scores:
        return "no scores returned"
    worst_axis = min(scores, key=lambda k: scores[k])
    return f"{worst_axis}={scores[worst_axis]:.1f}"


def _emit(verdict: str, reason: str) -> None:
    """Print the canonical stdout line: ``VERDICT=<...> REASON="<...>"``."""
    # Escape any double-quotes in the reason so the bash parser doesn't
    # choke on a stray `"` inside the value. Single quotes are safe.
    safe = reason.replace('"', "'")
    print(f"VERDICT={verdict} REASON=\"{safe}\"")


def run(
    *,
    project_root: Path,
    commit_message: str,
    diff_stat: str,
    diff_sample: str,
) -> int:
    """Run the judge and return an exit code.

    Args:
        project_root: repo root, used for `.env` lookup by llm_judge.
        commit_message: full commit body of the tip commit being pushed.
        diff_stat: `git diff --stat` output for the same commit.
        diff_sample: up to ~2 KB of unified diff hunk content.

    Returns:
        0  — OK (commit has value/intent, push allowed)
        1  — DRIFT_WARNING or ROT (push blocked, reason printed)
        2  — configuration error (no api_key, prompt template missing)
    """
    # Load judge config early so a missing api_key produces exit 2
    # (loud config error) rather than exit 1 (looks like a content
    # failure). The pre-push hook can then distinguish "you forgot to
    # set the env var" from "the judge didn't like your commit".
    cfg = llm_judge.load_config(project_root)
    if not cfg.get("api_key"):
        _emit("ROT", "api_key missing (set MINIMAX_API_KEY or ANTHROPIC_API_KEY)")
        return 2

    user_prompt = _format_user_prompt(
        commit_message=commit_message,
        diff_stat=diff_stat,
        diff_sample=diff_sample,
    )
    # Substitute the per-dim prompt template via format_prompt (loads
    # eval/prompts/judge-push-intent.md and injects ${...} slots). The
    # user-prompt body above is appended after the template so the
    # judge sees both rubric guidance and the actual commit inputs.
    template = llm_judge.format_prompt(
        project_root, "judge-push-intent.md", {},
    )
    if not template:
        _emit("ROT", "judge-push-intent.md template missing")
        return 2
    full_prompt = f"{template}\n\n---\n\n{user_prompt}"

    try:
        result = llm_judge.call_judge(
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            prompt=full_prompt,
            axes=PUSH_INTENT_AXES,
            dim="push_intent",
            base_url=cfg.get("base_url", "https://api.minimax.io/anthropic"),
        )
    except Exception as exc:
        # Any LLM call failure -> exit 2 so the operator knows it's a
        # config/network problem, not a content problem with their commit.
        _emit("ROT", f"judge call failed: {exc}")
        return 2

    scores = result.get("scores") or {}
    # Coerce each axis to float, dropping missing/non-numeric ones (the
    # shared _coerce_score helper from eval_runner isn't worth an
    # import dependency for one call site).
    coerced = {}
    for ax in PUSH_INTENT_AXES:
        try:
            coerced[ax] = float(scores.get(ax, 0.0))
        except (TypeError, ValueError):
            coerced[ax] = 0.0

    if not any(coerced.values()):
        # parse_scores_json returned {} — could not recover scores from
        # the model output. Treat as ROT so a malformed commit (or a
        # broken prompt) fails loud instead of silently passing.
        _emit("ROT", "no scores parseable from judge response")
        return 1

    score = llm_judge.score_aggregate(coerced)
    verdict = llm_judge.verdict_from_score(score)
    _emit(verdict, _worst_axis_reason(coerced))

    # Only OK is non-blocking. DRIFT_WARNING also blocks the push so a
    # borderline commit gets a manual second look.
    return 0 if verdict == "OK" else 1


def cli_main(argv: Optional[list] = None) -> int:
    """CLI entry point. Parses argv and delegates to :func:`run`."""
    parser = argparse.ArgumentParser(
        description="Judge the intent of a commit before push (opt-in).",
    )
    parser.add_argument(
        "--project-root", default=".",
        help="project root for .env lookup (default: cwd)",
    )
    parser.add_argument(
        "--commit-message", required=True,
        help="full commit body of the tip commit being pushed",
    )
    parser.add_argument(
        "--diff-stat", required=True,
        help="`git diff --stat` output for the same commit",
    )
    parser.add_argument(
        "--diff-sample", default="",
        help="up to ~2 KB of unified diff hunk content",
    )
    args = parser.parse_args(argv)

    return run(
        project_root=Path(args.project_root).resolve(),
        commit_message=args.commit_message,
        diff_stat=args.diff_stat,
        diff_sample=args.diff_sample,
    )


if __name__ == "__main__":
    sys.exit(cli_main())
