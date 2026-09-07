#!/usr/bin/env python3
"""test_slop_detector.py — regression for the v2 hook.

Verifies hooks/slop-detector.sh across tiers:

    - clean input -> exit 0, empty stderr
    - HIGH EN phrase cluster -> HIGH bucket
    - HIGH KO structure -> HIGH bucket
    - T2 structure catch (binary contrast) -> at least MEDIUM
    - T2 structure catch (Wh-starter) -> LOW bucket on a single finding
    - lockfile path skip -> exit 0, no scan
    - missing bank files -> falls back to v1 inline bank, prints WARN to stderr
    - sample-with-slop.md (regression fixture) -> HIGH
    - sample-clean.md (regression fixture)   -> 0 findings

We drive the script as a black box, the same way Claude Code does:
    stdin  : PostToolUse payload JSON
    stdout : empty (advisory)
    stderr : advisory text

No mocks. jq must be available on $PATH (same constraint as the hook itself).
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
PHRASES_BANK = REPO_ROOT / "hooks" / "references" / "slop" / "phrases.md"
STRUCTURES_BANK = REPO_ROOT / "hooks" / "references" / "slop" / "structures.md"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "slop"


def _require_jq() -> None:
    if shutil.which("jq") is None:
        raise unittest.SkipTest("jq is required on $PATH for slop-detector tests")


def _payload(file_path: str, content: str) -> str:
    return json.dumps({"tool_input": {"file_path": file_path, "content": content}})


def run_hook(content: str, *, file_path: str = "test.md", env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the hook with a PostToolUse payload and capture output.

    Pin `DEV_KIT_STAGE=build` so the test is hermetic w.r.t. whatever
    `.dev-kit/.active-hooks.json` the developer has on disk. Without the
    pin, a bootstrapped checkout (the typical post-`/dev-kit:bootstrap`
    state) makes the stage gate resolve the stage to `bootstrap`, where
    `slop-detector` is off, so the hook exits 0 silently and the tests
    assert against a hook that deliberately did nothing.
    """
    _require_jq()
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env.setdefault("DEV_KIT_STAGE", "build")
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


class CleanBaseline(unittest.TestCase):
    def test_clean_md_passes(self) -> None:
        proc = run_hook(
            "We shipped HMAC signing for webhooks. "
            "Secret lives in env, SDK wraps it, verify endpoint rejects in 2 ms. "
            "PR is up; review by EOD."
        )
        self.assertEqual(proc.returncode, 0, msg=f"exit={proc.returncode} stderr={proc.stderr}")
        self.assertEqual(proc.stdout, "")
        self.assertEqual(severity_of(proc.stderr), "OK")


class HighEnglishCluster(unittest.TestCase):
    def test_phrase_flood_triggers_high(self) -> None:
        content = (
            "In today's fast-paced landscape, we need to lean into uncertainty "
            "and navigate these complexities with a holistic approach. "
            "Its worth noting that robust and comprehensive changes are seamless "
            "and empower stakeholders."
        )
        proc = run_hook(content)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(severity_of(proc.stderr), "HIGH")
        # unique T1 phrase markers we expect to surface
        for marker in ("lean into", "robust", "comprehensive", "empower", "landscape"):
            self.assertIn(marker, proc.stderr.lower())


class HighKoreanStructure(unittest.TestCase):
    def test_ko_cluster_triggers_high(self) -> None:
        content = (
            "오늘날의 빠르게 변하는 시대에 우리는 종합적인 분석을 통해 강력한 기능을 도입했습니다. "
            "다양한 이점들이 있습니다. 핵심적으로 성능이 향상되었습니다."
        )
        proc = run_hook(content)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(severity_of(proc.stderr), "HIGH")
        # KO markers present
        for marker in ("오늘날의", "종합적인", "강력한"):
            self.assertIn(marker, proc.stderr)

    def test_ko_structure_alone_triggers_high(self) -> None:
        """A sentence that only carries KO structural crutches (no KO T1 phrases)
        must still escalate to HIGH via the KO-T2 branch. Plain ASCII characters
        would NOT trigger this path; the KO structure is the whole signal."""
        content = "이것 때문에 우리는 잘못된 결정을 내렸습니다. 반드시 기억하시기 바랍니다."
        proc = run_hook(content)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(
            severity_of(proc.stderr), "HIGH",
            msg=f"KO structure alone should be HIGH; stderr={proc.stderr}",
        )


class StructureShapes(unittest.TestCase):
    def test_binary_contrast_is_at_least_medium(self) -> None:
        content = "The answer is not A. It's B. It feels like X. It's actually Y."
        proc = run_hook(content)
        self.assertEqual(proc.returncode, 0)
        sev = severity_of(proc.stderr)
        self.assertIn(sev, {"MEDIUM", "HIGH"}, msg=f"got {sev}; stderr={proc.stderr}")

    def test_wh_starter_yields_low_or_higher(self) -> None:
        content = "What makes this hard is the constraint. When in doubt, we ship."
        proc = run_hook(content)
        self.assertEqual(proc.returncode, 0)
        # Wh-starter at sentence start is a T1 hit -> at least LOW
        sev = severity_of(proc.stderr)
        self.assertIn(sev, {"LOW", "MEDIUM", "HIGH"})


class Scoping(unittest.TestCase):
    def test_lockfile_path_is_skipped(self) -> None:
        proc = run_hook("any content", file_path="package-lock.json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertEqual(proc.stdout.strip(), "")

    def test_minified_path_is_skipped(self) -> None:
        proc = run_hook("any content", file_path="app.min.js")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.strip(), "")


class StrictMode(unittest.TestCase):
    def test_high_with_slop_strict_exits_2(self) -> None:
        content = (
            "In today's fast-paced landscape, we need to lean into uncertainty "
            "and navigate these complexities with a holistic approach. "
            "Its worth noting that robust and comprehensive changes are seamless "
            "and empower stakeholders."
        )
        proc = run_hook(content, env_extra={"SLOP_STRICT": "1"})
        # Strict mode exits 2 on HIGH
        self.assertEqual(proc.returncode, 2, msg=f"exit={proc.returncode} stderr={proc.stderr}")
        self.assertEqual(severity_of(proc.stderr), "HIGH")


class BankFallback(unittest.TestCase):
    def test_inline_fallback_when_banks_missing(self) -> None:
        # Defensive: if the references/ bank is ever removed, hook must
        # still fire on legacy v1 phrases (e.g. "Certainly!").
        #
        # The hook sources hooks/lib/stage-gate.sh, which reads
        # `.dev-kit/.active-hooks.json` from `git rev-parse
        # --show-toplevel`. To make the gate fail-open at line 17 (no
        # matrix file present), the test must run the hook from a
        # directory whose git toplevel has NO `.dev-kit/`. Creating a
        # worktree inside the dev-harness-kit checkout is not enough
        # because git resolves the worktree's toplevel to the parent
        # worktree (via commondir), which DOES have `.dev-kit/`. We
        # therefore use a temp worktree created from a git context
        # OUTSIDE the dev-harness-kit checkout (a temp dir containing
        # only a fresh `git init`), which makes the toplevel that
        # git sees exactly equal to the temp dir — no `.dev-kit/`,
        # stage gate fail-opens, inline fallback fires.
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outer = tmp_path / "outer"
            wt = tmp_path / "wt"
            outer.mkdir()
            # Fresh git context, NOT inside dev-harness-kit.
            sp.run(["git", "init", "-q", str(outer)], check=True)
            (outer / ".gitkeep").write_text("placeholder\n")
            sp.run(["git", "add", ".gitkeep"], cwd=str(outer), check=True)
            sp.run(["git", "-c", "user.email=x@x", "-c", "user.name=x",
                    "commit", "-q", "-m", "init"], cwd=str(outer), check=True)
            sp.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(wt), "HEAD"],
                cwd=str(outer), check=True,
            )
            # Mirror just the hook + its lib into the worktree.
            sp.run(["mkdir", "-p", str(wt / "hooks")], check=True)
            shutil.copy(str(HOOK), str(wt / "hooks" / "slop-detector.sh"))
            shutil.copytree(
                REPO_ROOT / "hooks" / "lib",
                wt / "hooks" / "lib",
                symlinks=True,
            )
            # The stage gate does `cd ${BASH_SOURCE[0]%/*}/../..` and
            # then `sys.path.insert(0, <plugin_root>/lib)` before
            # importing `active_hooks_codec`. That module is part of
            # dev-kit's lib/ package and depends on `lib/atomic.py` —
            # the gate does NOT source them on a separate path, so the
            # entire `lib/` directory must be reachable at the
            # worktree root.
            shutil.copytree(
                REPO_ROOT / "lib",
                wt / "lib",
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (wt / "hooks" / "references" / "slop" / "phrases.md").unlink(
                missing_ok=True,
            )
            (wt / "hooks" / "references" / "slop" / "structures.md").unlink(
                missing_ok=True,
            )
            content = "Certainly! This is a robust and comprehensive solution."
            payload = json.dumps({"tool_input": {"file_path": "test.md", "content": content}})
            proc = sp.run(
                [str(wt / "hooks" / "slop-detector.sh")],
                input=payload, capture_output=True, text=True,
                cwd=str(wt),
                env={
                    **os.environ,
                    "CLAUDE_PLUGIN_ROOT": str(wt),
                    "CLAUDE_PROJECT_DIR": str(wt),
                    "PATH": os.environ["PATH"],
                },
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("WARN", proc.stderr)
            self.assertIn("[slop-detector]", proc.stderr)
            # v1 inline bank still catches "robust" / "comprehensive"
            self.assertTrue(
                any(tok in proc.stderr for tok in ("robust", "comprehensive")),
                msg=f"fallback did not flag legacy phrases: {proc.stderr}",
            )


class RegressionFixtures(unittest.TestCase):
    """The fixtures referenced by skills/inspect/SKILL.md (--slop) and the bank README."""

    def test_sample_with_slop_is_high(self) -> None:
        path = FIXTURE_DIR / "sample-with-slop.md"
        self.assertTrue(path.exists(), f"missing fixture: {path}")
        text = path.read_text(encoding="utf-8")
        proc = run_hook(text, file_path=str(path.name))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(severity_of(proc.stderr), "HIGH")

    def test_sample_clean_is_silent(self) -> None:
        path = FIXTURE_DIR / "sample-clean.md"
        self.assertTrue(path.exists(), f"missing fixture: {path}")
        text = path.read_text(encoding="utf-8")
        proc = run_hook(text, file_path=str(path.name))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr.strip(), "")


class BankFileInvariants(unittest.TestCase):
    """The bank files are loaded by `grep -oE -f <(... filter ...)`. They must:
       - be readable,
       - contain at least 20 non-comment, non-blank lines,
       - contain no POSIX-unfriendly escape (\\b, \\m, \\s, \\d, \\w)
         because BSD grep / ugrep on macOS reject these in ERE mode.
    """

    def _assert_bank_loadable(self, path: Path, *, min_lines: int) -> None:
        self.assertTrue(path.exists(), f"missing bank file: {path}")
        text = path.read_text(encoding="utf-8")
        loadable_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreaterEqual(
            len(loadable_lines), min_lines,
            msg=f"{path.name}: only {len(loadable_lines)} loadable lines (>= {min_lines} required)",
        )

        # Portable ERE: only allow POSIX character classes & standard escapes.
        bad = []
        for line in loadable_lines:
            # Look for bare \\X escapes that aren't POSIX classes or \\./\\- etc.
            for tok in line.split():
                if tok.startswith("\\") and len(tok) > 1 and tok[1].isalpha():
                    if tok[1] not in (":", ):
                        bad.append((tok, line))
        self.assertEqual(bad, [], msg=f"non-portable escapes in {path.name}: {bad}")

    def test_phrases_bank(self) -> None:
        # Floor at 80% of the current loadable line count so accidental
        # halves of the bank during edits get caught.
        self._assert_bank_loadable(PHRASES_BANK, min_lines=50)

    def test_structures_bank(self) -> None:
        self._assert_bank_loadable(STRUCTURES_BANK, min_lines=30)


if __name__ == "__main__":
    unittest.main()
