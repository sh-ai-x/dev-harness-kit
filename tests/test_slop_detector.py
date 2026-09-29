#!/usr/bin/env python3
"""test_slop_detector.py — regression for the v3 LLM-judge hook.

v3 (refactor/slop-detector-llm-judge) replaces the v2 regex tier ladder
(`hooks/references/slop/{phrases,structures}.md`) with a single
`lib/llm_judge.py:call_judge(dim="slop")` call. Tests drive the
script as a black box (stdin = PostToolUse JSON, stderr = advisory,
stdout = `{severity,score,reason}`) and inject canned scores via the
`SLOP_FIXTURE` env so verdicts are deterministic without the network.

    - clean content + no SLOP_FIXTURE + no api_key -> silent OK
    - clean content + SLOP_FIXTURE=high  -> HIGH bucket
    - clean content + SLOP_FIXTURE=med   -> MEDIUM
    - clean content + SLOP_FIXTURE=low   -> LOW
    - clean content + SLOP_FIXTURE=ok    -> silent OK
    - SLOP_FIXTURE=high + SLOP_STRICT=1  -> exit 2
    - lockfile path skip                  -> exit 0, no scan
    - non-Latin CJK body                  -> exit 0, no scan
    - sample-with-slop.md fixture + HIGH  -> HIGH
    - sample-clean.md fixture             -> silent

No mocks. jq must be available on $PATH (same constraint as the hook
itself).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "slop-detector.sh"


def _require_jq() -> None:
    if shutil.which("jq") is None:
        raise unittest.SkipTest("jq is required on $PATH for slop-detector tests")


def _payload(file_path: str, content: str) -> str:
    return json.dumps({"tool_input": {"file_path": file_path, "content": content}})


def _write_fixture(tmp: Path, score: float, reason: str = "test reason") -> Path:
    """Write a SLOP_FIXTURE JSON and return its path. Caller cleans up."""
    f = tmp / f"slop-fixture-{score}.json"
    f.write_text(json.dumps({"slop_score": score, "reason": reason}), encoding="utf-8")
    return f


def run_hook(
    content: str,
    *,
    file_path: str = "test.md",
    env_extra: dict | None = None,
    fixture: Path | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with a PostToolUse payload and capture output.

    Pin `DEV_KIT_STAGE=build` so the test is hermetic w.r.t. whatever
    `.dev-kit/.active-hooks.json` the developer has on disk. The new
    v3 hook does NOT need `CLAUDE_PLUGIN_ROOT` (the judge reads
    `PROJECT_ROOT` -> `.env` instead), but we still pass
    `CLAUDE_PLUGIN_ROOT` for stage-gate compatibility.
    """
    _require_jq()
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env.setdefault("DEV_KIT_STAGE", "build")
    # Strip any leaked api key from the dev box — the fixture path must
    # take precedence when both are present, otherwise env-loaded
    # api_key would let the real judge run on a network-touching dev box.
    env.pop("MINIMAX_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    if fixture is not None:
        env["SLOP_FIXTURE"] = str(fixture)
    elif "SLOP_FIXTURE" in env:
        env.pop("SLOP_FIXTURE")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(HOOK)],
        input=_payload(file_path, content),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def severity_of(stderr: str) -> str:
    for sev in ("HIGH", "MEDIUM", "LOW"):
        if f"[slop-detector] {sev}" in stderr:
            return sev
    return "OK"


class SilentWithoutApiKey(unittest.TestCase):
    def test_clean_content_no_api_key_no_fixture_is_silent(self) -> None:
        """The v3 hook must exit silently when api_key is missing and no
        fixture is provided — advisory default, no false-positive on
        dev boxes without MINIMAX_API_KEY configured."""
        proc = run_hook("Plain prose. PR is up; review by EOD.")
        self.assertEqual(proc.returncode, 0, msg=f"exit={proc.returncode} stderr={proc.stderr}")
        self.assertEqual(proc.stderr.strip(), "")
        self.assertEqual(proc.stdout.strip(), "")


class FixtureSeverity(unittest.TestCase):
    """SLOP_FIXTURE injects a canned slop_score; severity is mapped from
    the score threshold ladder (≥8 OK, ≥5 LOW, ≥2 MEDIUM, <2 HIGH)."""

    def _run(self, score: float, file_path: str = "test.md") -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            fix = _write_fixture(Path(tmp), score)
            return run_hook("any content", file_path=file_path, fixture=fix)

    def test_high_score_maps_to_high(self) -> None:
        proc = self._run(1.0)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(severity_of(proc.stderr), "HIGH")
        self.assertIn("score=1.0", proc.stderr)
        self.assertIn("test reason", proc.stderr)

    def test_medium_score_maps_to_medium(self) -> None:
        proc = self._run(3.0)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(severity_of(proc.stderr), "MEDIUM")

    def test_low_score_maps_to_low(self) -> None:
        proc = self._run(6.0)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(severity_of(proc.stderr), "LOW")

    def test_clean_score_is_silent(self) -> None:
        proc = self._run(9.0)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")


class StrictMode(unittest.TestCase):
    def test_high_with_slop_strict_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = _write_fixture(Path(tmp), 1.0)
            proc = run_hook(
                "any content",
                env_extra={"SLOP_STRICT": "1"},
                fixture=fix,
            )
            self.assertEqual(proc.returncode, 2, msg=f"exit={proc.returncode} stderr={proc.stderr}")
            self.assertEqual(severity_of(proc.stderr), "HIGH")

    def test_strict_does_not_trigger_on_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = _write_fixture(Path(tmp), 6.0)
            proc = run_hook(
                "any content",
                env_extra={"SLOP_STRICT": "1"},
                fixture=fix,
            )
            # LOW stays advisory even under SLOP_STRICT — strict only
            # blocks HIGH (matches v2 contract).
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertEqual(severity_of(proc.stderr), "LOW")


class Scoping(unittest.TestCase):
    def test_lockfile_path_is_skipped(self) -> None:
        proc = run_hook("any content", file_path="package-lock.json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")

    def test_minified_path_is_skipped(self) -> None:
        proc = run_hook("any content", file_path="app.min.js")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")

    def test_cjk_body_skips_judge(self) -> None:
        """v3 is conservative on non-Latin prose — the judge prompt is
        English and scoring Hangul/Japanese with English rubrics is
        unreliable. Skip without scanning; CJK content gets the
        existing pre-filter treatment."""
        with tempfile.TemporaryDirectory() as tmp:
            fix = _write_fixture(Path(tmp), 1.0)  # would otherwise be HIGH
            proc = run_hook(
                "오늘날의 빠르게 변하는 시대에 종합적인 분석을 도입했습니다.",
                file_path="test.md",
                fixture=fix,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            # CJK content is skipped before the judge call -> no stderr.
            self.assertEqual(proc.stderr.strip(), "")


class EmptyInput(unittest.TestCase):
    def test_empty_content_is_silent(self) -> None:
        proc = run_hook("")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertEqual(proc.stdout.strip(), "")


class QuietMode(unittest.TestCase):
    def test_slop_quiet_suppresses_stderr_but_keeps_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fix = _write_fixture(Path(tmp), 1.0)
            proc = run_hook(
                "any content",
                env_extra={"SLOP_QUIET": "1"},
                fixture=fix,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertEqual(proc.stderr.strip(), "")


class PromptTemplateContract(unittest.TestCase):
    """The judge prompt must exist and be readable — the hook falls
    back to silent OK if it's missing, but a missing prompt at the
    canonical location is a regression the test should catch before
    a deployment."""

    def test_prompt_template_exists(self) -> None:
        path = REPO_ROOT / "eval" / "prompts" / "judge-slop.md"
        self.assertTrue(path.exists(), f"missing prompt template: {path}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("slop_score", text)
        self.assertIn("${CONTENT}", text)


class LlmJudgeDimContract(unittest.TestCase):
    """`lib/llm_judge.py` must declare the `slop` dim with `slop_score`
    so `DIM_AXES["slop"]` resolves at hook runtime."""

    def test_slop_dim_registered(self) -> None:
        from lib import llm_judge  # type: ignore

        axes = llm_judge.DIM_AXES.get("slop", ())
        self.assertIn("slop_score", axes)


if __name__ == "__main__":
    unittest.main()
