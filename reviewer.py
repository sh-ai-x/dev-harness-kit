#!/usr/bin/env python3
"""reviewer.py — MiniMax-powered PR reviewer (GitHub Action).

Invoked from .github/workflows/dev-kit-review.yml on pull_request event.
Reads PR diff via gh CLI, calls MiniMax (Anthropic-compatible API),
posts review comment via `gh pr comment`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_judge  # type: ignore  # noqa: E402


REVIEW_PROMPT = """You are reviewing a pull request for the `dev-harness-kit` plugin.

PR title: {title}
PR body: {body}
Files changed: {files}
Diff (truncated to first 8K chars):
```
{diff}
```

Score the PR on 4 axes 0-10 each:
1. **architecture**: modular / IRON-LAWS / SSOT / single-source-of-truth respected
2. **correctness**: tests cover behavior, no obvious bugs, edge cases handled
3. **convention**: naming per ADR-0010, comments meaningful, no slop/phrases
4. **safety**: hook behavior (advisory default, --strict opt-in), no destructive ops

Respond ONLY with a JSON object:
{{"architecture":N,"correctness":N,"convention":N,"safety":N,"summary":"<1-line>"}}
"""


def get_pr_diff(project_root: Path, diff_limit_chars: int = 8000) -> dict:
    """Get PR diff via gh CLI. Returns {title, body, files, diff}."""
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", env.get("GITHUB_TOKEN", ""))
    try:
        title = subprocess.run(
            ["gh", "pr", "view", "--json", "title", "--jq", ".title"],
            capture_output=True, text=True, cwd=str(project_root), env=env,
        ).stdout.strip()
    except Exception:
        title = ""
    try:
        body = subprocess.run(
            ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
            capture_output=True, text=True, cwd=str(project_root), env=env,
        ).stdout.strip()
    except Exception:
        body = ""
    try:
        files = subprocess.run(
            ["gh", "pr", "diff", "--name-only"],
            capture_output=True, text=True, cwd=str(project_root), env=env,
        ).stdout.strip()
    except Exception:
        files = ""
    try:
        diff = subprocess.run(
            ["gh", "pr", "diff"],
            capture_output=True, text=True, cwd=str(project_root), env=env,
        ).stdout
    except Exception:
        diff = ""
    return {
        "title": title,
        "body": body[:2000],
        "files": files[:2000],
        "diff": diff[:diff_limit_chars],
    }


def render_review(scores: dict, summary: str) -> str:
    """Format PR comment markdown with verdict + 4-axis table."""
    vals = [scores.get(k, 0) for k in ("architecture", "correctness", "convention", "safety")]
    verdict = "✅ APPROVE" if all(v >= 8 for v in vals) else (
        "🔴 CHANGES REQUESTED" if any(v < 5 for v in vals) else "🟡 COMMENT"
    )
    return (
        f"## {verdict}\n\n"
        f"### 4-Axis Scores\n"
        f"| Axis | Score |\n|---|---|\n"
        f"| architecture | {scores.get('architecture', '?')} |\n"
        f"| correctness | {scores.get('correctness', '?')} |\n"
        f"| convention | {scores.get('convention', '?')} |\n"
        f"| safety | {scores.get('safety', '?')} |\n\n"
        f"**Summary**: {summary}\n"
    )


def main() -> int:
    project_root = Path(os.environ.get("PROJECT_ROOT", "."))
    config = llm_judge.load_config(project_root)

    api_key = config.get("api_key") or config.get("minimax_api_key", "")
    if not api_key:
        print("::warning::No MiniMax API key configured — skipping LLM review")
        subprocess.run(
            ["gh", "pr", "comment", "--body",
             "## LLM Review (skipped)\n\n"
             "No `MINMAX_API_KEY` secret configured. "
             "Set it in repo Settings → Secrets."],
            cwd=str(project_root),
        )
        return 0

    pr_meta = get_pr_diff(project_root)
    prompt = REVIEW_PROMPT.format(**pr_meta)

    try:
        result = llm_judge.call_judge(
            provider=config["provider"],
            api_key=api_key,
            model=config["model"],
            prompt=prompt,
            base_url=config.get("base_url", "https://api.minimax.io/anthropic"),
        )
    except Exception as e:
        print(f"::error::MiniMax call failed: {e}")
        subprocess.run(
            ["gh", "pr", "comment", "--body",
             f"## LLM Review (failed)\n\n`{e}`"],
            cwd=str(project_root),
        )
        return 0

    scores = result["scores"]
    summary_match = ""
    raw = result.get("raw", "")
    try:
        m = re.search(r'"summary"\s*:\s*"([^"]+)"', raw)
        if m:
            summary_match = m.group(1)
    except Exception:
        pass

    body = render_review(scores, summary_match or "(no summary)")
    subprocess.run(["gh", "pr", "comment", "--body", body], cwd=str(project_root))

    print(f"tokens_in={result['tokens_in']} tokens_out={result['tokens_out']}")
    print(f"scores={scores}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
