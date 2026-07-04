#!/usr/bin/env python3
"""eval_runner.py — Asset freshness evaluator (MUST-32~34).

Scans repository for assets (CLAUDE.md / skills / hooks / iron laws),
runs LLM-as-judge per asset (4 axes), writes .dev-kit/eval-report.md.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
import llm_judge  # type: ignore

KST = timezone(timedelta(hours=9))
GOLDEN_SCHEMA_VERSION = "1.0.0"
ASSET_KINDS = ("claude_md", "skill", "hook", "iron_law", "methodology")


def now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z")


def discover_assets(project_root: Path) -> List[Dict]:
    """Find all evaluable assets in the project. Returns list of {path, kind, content}.

    Discovers:
    - CLAUDE.md (kind=claude_md)
    - skills/<name>/SKILL.md (kind=skill)
    - .claude-plugin/plugin/hooks/*.sh (kind=hook)
    - lib/write_claude_md.py IRON_LAWS (kind=iron_law)
    - lib/methodology/*.py excluding abc.py (kind=methodology)
    """
    assets = []
    claude_md = project_root / "CLAUDE.md"
    if claude_md.exists():
        assets.append({
            "path": "CLAUDE.md",
            "kind": "claude_md",
            "content": claude_md.read_text(encoding="utf-8"),
        })
    skills_dir = project_root / "skills"
    if skills_dir.exists():
        for skill_md in sorted(skills_dir.rglob("SKILL.md")):
            rel = skill_md.relative_to(project_root)
            assets.append({
                "path": str(rel),
                "kind": "skill",
                "content": skill_md.read_text(encoding="utf-8"),
            })
    hooks_dir = project_root / ".claude-plugin" / "plugin" / "hooks"
    if hooks_dir.exists():
        for hook in sorted(hooks_dir.glob("*.sh")):
            rel = hook.relative_to(project_root)
            assets.append({
                "path": str(rel),
                "kind": "hook",
                "content": hook.read_text(encoding="utf-8"),
            })
    iron_law_src = project_root / "lib" / "write_claude_md.py"
    if iron_law_src.exists():
        body = iron_law_src.read_text(encoding="utf-8")
        for line in body.splitlines():
            if line.startswith("L1_") or line.startswith("L2_") or line.startswith("L3_") or line.startswith("L4_") or line.startswith("L5_"):
                assets.append({
                    "path": f"lib/write_claude_md.py: {line.strip()[:40]}",
                    "kind": "iron_law",
                    "content": line.strip(),
                })
                break  # one representative entry
    method_dir = project_root / "lib" / "methodology"
    if method_dir.exists():
        for m in sorted(method_dir.glob("*.py")):
            if m.stem not in ("__init__", "abc"):
                rel = m.relative_to(project_root)
                assets.append({
                    "path": str(rel),
                    "kind": "methodology",
                    "content": m.read_text(encoding="utf-8"),
                })
    return assets


def load_golden(path: Path) -> Dict:
    """Load golden baseline JSON or return default placeholder."""
    if not path.exists():
        return {
            "asset": "",
            "schema_version": GOLDEN_SCHEMA_VERSION,
            "captured_at": "",
            "summary": "(no golden baseline captured yet)",
            "expected_behavior": "",
            "iron_law_refs": [],
            "code_refs": [],
            "status": "pending",
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_golden(path: Path, data: Dict) -> None:
    """Save golden baseline. Atomic write."""
    import os, tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _judge_asset(project_root: Path, asset: Dict, config: Dict) -> Dict:
    """Run LLM-as-judge on asset. Returns {scores, tokens_in, tokens_out, raw, verdict, score}."""
    prompt_key = {
        "claude_md": "judge-claude-md",
        "skill": "judge-skill",
        "hook": "judge-hook",
        "iron_law": "judge-claude-md",
        "methodology": "judge-skill",
    }.get(asset["kind"], "judge-skill")
    prompt = llm_judge.format_prompt(project_root, prompt_key + ".md", {
        "ASSET_NAME": asset["path"],
        "ASSET_KIND": asset["kind"],
        "ASSET_CONTENT": asset["content"][:4000],  # truncate
    })
    if not prompt:
        # fallback inline judge
        prompt = (
            f"You are a code review judge. Evaluate this {asset['kind']} asset.\n\n"
            f"Path: {asset['path']}\n\n"
            f"Content:\n{asset['content'][:4000]}\n\n"
            "Respond ONLY with a JSON object containing 4 axis scores (semantic_drift, "
            "completeness, correctness, consistency), each 0-10."
        )
    raw = llm_judge.call_judge(
        provider=config["provider"],
        api_key=config["api_key"],
        model=config["model"],
        prompt=prompt,
        base_url=config.get("base_url", "https://api.minimax.io/anthropic"),
    )
    scores = raw["scores"]
    score = llm_judge.score_aggregate(scores) if scores else 0.0
    verdict = llm_judge.verdict_from_score(score)
    return {
        "scores": scores,
        "tokens_in": raw["tokens_in"],
        "tokens_out": raw["tokens_out"],
        "raw": raw["raw"][:500],
        "verdict": verdict,
        "score": score,
    }


def score_asset(project_root: Path, asset: Dict, config: Optional[Dict] = None) -> Dict:
    """Score a single asset. Config defaults from llm_judge.load_config()."""
    if config is None:
        config = llm_judge.load_config(project_root)
    return _judge_asset(project_root, asset, config)


def cross_check_agree(results: List[Dict], tolerance: float = 0.5) -> bool:
    """MUST-NOT-23: 2-judge cross-check. Agree if all scores within tolerance."""
    if not results:
        return True
    base = results[0].get("score", 0)
    return all(abs(r.get("score", 0) - base) <= tolerance for r in results)


def write_report(project_root: Path, results: List[Dict], config: Optional[Dict] = None) -> Path:
    """Write .dev-kit/eval-report.md human-readable summary."""
    path = project_root / ".dev-kit" / "eval-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Eval Report — dev-harness-kit",
        f"> Generated: {now_iso()}",
        f"> Provider: {config.get('provider', 'minimax') if config else 'minimax'}",
        f"> Model: {config.get('model', 'MiniMax-M3[1m]') if config else 'MiniMax-M3[1m]'}",
        "",
        "## Summary",
    ]
    by_verdict = {"OK": 0, "DRIFT_WARNING": 0, "ROT": 0}
    for r in results:
        by_verdict[r.get("verdict", "OK")] += 1
    lines.append(f"- Total assets: {len(results)}")
    lines.append(f"- OK: {by_verdict['OK']}")
    lines.append(f"- DRIFT_WARNING: {by_verdict['DRIFT_WARNING']}")
    lines.append(f"- ROT: {by_verdict['ROT']}")
    lines.append("")
    lines.append("## Per-Asset Scores")
    for r in results:
        verdict = r.get("verdict", "?")
        score = r.get("score", 0)
        path_str = r.get("path", "?")
        s = r.get("scores", {})
        axis_str = ", ".join(f"{k}={s.get(k, '-')}" for k in llm_judge.JUDGE_AXES)
        lines.append(f"- **{verdict}** `{path_str}` score={score} ({axis_str})")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_eval(project_root: Path, config: Optional[Dict] = None, *, dry_run: bool = False) -> Dict:
    """Run full eval cycle. Returns report dict.

    Args:
        project_root: project root.
        config: llm_judge config (defaults to load_config()).
        dry_run: when True, skip LLM calls (return mock verdict per asset).
    """
    if config is None:
        config = llm_judge.load_config(project_root)
    if dry_run or not config.get("api_key"):
        # dry run / no api key → skip real LLM calls
        def _mock(asset):
            return {
                "path": asset["path"],
                "kind": asset["kind"],
                "scores": {ax: 7.0 for ax in llm_judge.JUDGE_AXES},
                "tokens_in": 0, "tokens_out": 0, "raw": "DRY_RUN",
                "verdict": "DRIFT_WARNING", "score": 7.0,
            }
        results = [_mock(a) for a in discover_assets(project_root)]
    else:
        results = []
        for asset in discover_assets(project_root):
            try:
                result = score_asset(project_root, asset, config)
                result["path"] = asset["path"]
                result["kind"] = asset["kind"]
                results.append(result)
            except Exception as e:
                results.append({
                    "path": asset["path"],
                    "kind": asset["kind"],
                    "scores": {ax: 0 for ax in llm_judge.JUDGE_AXES},
                    "tokens_in": 0, "tokens_out": 0, "raw": str(e),
                    "verdict": "ROT",
                    "score": 0.0,
                    "error": str(e),
                })
    write_report(project_root, results, config)
    return {
        "results": results,
        "config": {k: v for k, v in config.items() if k != "api_key"},
        "summary": {k: results.count(v) for k in ["OK", "DRIFT_WARNING", "ROT"] for v in []},
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run asset freshness eval")
    parser.add_argument("--project-root", default=".", help="project root")
    parser.add_argument("--dry-run", action="store_true", help="skip LLM calls")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    report = run_eval(root, dry_run=args.dry_run)
    summary = {}
    for r in report["results"]:
        v = r.get("verdict", "?")
        summary[v] = summary.get(v, 0) + 1
    print(json.dumps(summary, indent=2))
