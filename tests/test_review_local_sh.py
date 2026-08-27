"""test_review_local_sh.py — shell-level tests for bin/review-local.sh.

The script is the local equivalent of `.github/workflows/review.yml`
(provider switch + verdict extraction + combined gate + L3-evidence
gate + optional auto-approve). These tests are deliberately hermetic:
they shell out to the script with `--help` and the bare-minimum argv
that fails BEFORE any `gh` / `claude` / network call. End-to-end
smoke tests against a real PR are run manually (the script's
interactive loop calls `claude -p` which isn't exercisable in CI).

Coverage:
  - bash -n syntax check (catches shell parse errors).
  - --help prints the documented usage block.
  - Missing --pr exits non-zero with an error.
  - Non-numeric --pr exits non-zero with an error.
  - Unknown flag exits non-zero with --help hint.
  - The script is executable (mode includes the +x bit).
  - Stub-binary behavioural coverage:
      * Bump-PR title skip exits 0 with `source=bin_review_local
        (bump-PR skip)` audit marker; no `claude` call.
      * --auto-approve refuses on combined verdict != Approve
        (does NOT call `gh pr review --approve`).
      * --auto-approve refuses on L3-evidence gate failure.
      * --auto-approve refuses on empty judge output.
      * --no-touch-probe treats every PR as production-touching AND
        still runs the L3 regex on the PR body.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = PROJECT_ROOT / "bin" / "review-local.sh"


def _run(*args: str, check: bool = False, env: dict | None = None, path: str | None = None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    if path:
        e["PATH"] = path
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=check,
        env=e,
    )


class TestReviewLocalShell(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.exists(), f"missing script: {SCRIPT}")

    def test_script_is_executable(self) -> None:
        mode = SCRIPT.stat().st_mode
        self.assertTrue(
            mode & 0o111,
            f"bin/review-local.sh must be executable (mode={oct(mode)})",
        )

    def test_bash_n_syntax_check(self) -> None:
        """`bash -n` parses the script without executing anything. Catches
        shell grammar errors that would otherwise surface only at the
        first real invocation.
        """
        r = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_prints_usage(self) -> None:
        r = _run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        # Header must name the script.
        self.assertIn("review-local.sh", r.stdout)
        # Documented flags must appear in the help block.
        for flag in (
            "--pr",
            "--provider",
            "--auto-approve",
            "--review-only",
            "--security-only",
            "--maintenance-only",
            "--dry-run",
            "--no-touch-probe",
        ):
            self.assertIn(flag, r.stdout, f"--help missing flag {flag}")

    def test_help_short_flag_also_works(self) -> None:
        r = _run("-h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("review-local.sh", r.stdout)

    def test_missing_pr_exits_nonzero(self) -> None:
        """Bare invocation -- no --pr -- must fail fast with a clear
        error pointing at --pr.
        """
        r = _run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--pr", r.stderr)

    def test_non_numeric_pr_exits_nonzero(self) -> None:
        r = _run("--pr", "abc")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("numeric", r.stderr)

    def test_unknown_flag_exits_nonzero(self) -> None:
        r = _run("--pr", "1", "--no-such-flag")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--help", r.stderr)

    def test_short_help_does_not_invoke_gh(self) -> None:
        """Sanity: --help must exit before any gh / python call. The
        script reaches the `usage` block at argparse-time, so no
        subprocess is spawned. This is a contract test: a regression
        that defers --help past the provider-resolution block would
        fail (or hang) in CI where gh is unauthenticated.
        """
        r = _run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        # No "gh" or "python" in stderr — argv-only path.
        self.assertNotIn("gh ", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_plugin_dir_passed_to_spawned_claude(self) -> None:
        """Regression: bin/review-local.sh must pass --plugin-dir to
        every spawned `claude -p` so /dev-kit:* slash commands resolve.
        Without it, every judge exits in <1s with "Unknown command"
        and the wrapper silently defaults verdicts to Approve.

        This was originally issue #727 (closed PR #728), then
        re-surfaced as the "HTML viewer shows nothing useful" bug
        during PR #731's 2026-08-23 babysit -- the SSE pipe was
        alive but every line was `Unknown command: /dev-kit:*`.
        """
        src = SCRIPT.read_text(encoding="utf-8")
        # PR #749 hardened the invocation with `--bare` (skips the
        # dev-kit plugin's SessionStart/UserPromptSubmit hook chain,
        # which otherwise hangs the spawned `claude -p` CLI) and a
        # `run_with_timeout 600` wrapper; `--plugin-dir` is still the
        # load-bearing flag this regression guards, so match the
        # current argv shape rather than the pre-hardening literal.
        self.assertIn(
            'claude --bare --plugin-dir "$PLUGIN_SRC" -p "$prompt"',
            src,
            'review-local.sh must call `claude --bare --plugin-dir "$PLUGIN_SRC" -p "$prompt"`',
        )
        self.assertNotIn(
            'claude -p "$prompt" 2>&1',
            src,
            "review-local.sh still contains a bare `claude -p \"$prompt\" 2>&1` site",
        )
        self.assertIn('PLUGIN_SRC="$REPO_ROOT"', src)
        self.assertIn(".claude-plugin/plugin.json", src)

    def test_provider_inferred_from_process_env(self) -> None:
        """Regression: when the operator's interactive shell has
        `MINIMAX_API_KEY` exported (typical of `bin/set-provider.sh
        minimax` running in their login shell) but NO
        `CI_REVIEW_PROVIDER` flag/env/.env, bin/review-local.sh
        should still infer `minimax` and inject the
        ANTHROPIC_BASE_URL / MODEL block. Otherwise the script
        silently falls back to "local claude CLI auth" and the
        spawned `claude -p` either fails (no auth) or uses a
        different endpoint than the operator's interactive Claude
        Code session.

        Source-text contract: the MINIMAX_API_KEY check must come
        BEFORE the .env-readback fallback in the provider resolution
        block. Without the inference, the HTML viewer shows the
        "falling back to local claude CLI auth" line and emits
        "Unknown command: /dev-kit:*" (because the spawned
        `claude -p` has no `--plugin-dir` style context loaded).
        """
        src = SCRIPT.read_text(encoding="utf-8")
        # The MINIMAX_API_KEY inference must be present and ordered
        # before the lib.ci_setup.read_provider() fallback.
        self.assertIn(
            'elif [ -n "${MINIMAX_API_KEY:-}" ]; then',
            src,
            "review-local.sh must check MINIMAX_API_KEY env to infer minimax provider",
        )
        # Same for anthropic + deepseek.
        self.assertIn(
            'elif [ -n "${DEEPSEEK_API_KEY:-}" ]; then',
            src,
            "review-local.sh must check DEEPSEEK_API_KEY env to infer deepseek provider",
        )
        # The check ordering must put process env BEFORE the
        # lib/ci_setup.read_provider() python fallback.
        minimax_pos = src.find('elif [ -n "${MINIMAX_API_KEY:-}" ]')
        fallback_pos = src.find("from ci_setup import read_provider")
        self.assertGreater(
            minimax_pos, 0,
            "MINIMAX_API_KEY inference block missing",
        )
        self.assertGreater(
            fallback_pos, minimax_pos,
            "MINIMAX_API_KEY inference must come BEFORE the lib.ci_setup.read_provider fallback",
        )


# ---------------------------------------------------------------------------
# Stub-binary behavioural coverage.
#
# Tests in TestStubBinBehaviours install a tmpdir on PATH with stub
# `gh`, `claude`, and `python3` binaries that return controlled outputs.
# The script under test is then invoked as if it were talking to a real
# provider; the assertions inspect what was called and what was posted.
#
# Each stub logs its invocations to a marker file so the test can assert
# "gh pr review --approve was NOT called" / "claude was called N times"
# without parsing the script's stdout.
# ---------------------------------------------------------------------------
class TestStubBinBehaviours(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bindir = Path(self._tmp.name) / "bin"
        self.bindir.mkdir()
        self.call_log = self.bindir / "calls.log"
        # Prepend the stub dir to PATH so the script picks up our `gh`,
        # `claude`, and `python3` instead of the real ones. The script
        # also shells out to `python3 -m lib.maintenance_gate`; we
        # route that to the real interpreter by symlinking it AFTER
        # the stubs, so `python3 lib.*` still works. The stubs only
        # intercept the specific patterns the script uses.
        self._real_path = os.environ.get("PATH", "")
        self.new_path = f"{self.bindir}{os.pathsep}{self._real_path}"

    def _write_stub(self, name: str, body: str) -> Path:
        p = self.bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def _stub_gh(
        self,
        *,
        pr_state: str = "OPEN",
        pr_title: str = "feat(ci): anything",
        pr_body: str = "",
        pr_files: list[str] | None = None,
        repo_full: str = "owner/repo",
    ) -> None:
        file_list = pr_files if pr_files is not None else ["lib/x.py"]
        # The script's `read_pr_field` python helper expects `files`
        # to be a list of strings (the GH-Actions workflow jq filters
        # down to `.files[].path`). The stub returns the post-jq shape
        # so the test matches the production contract.
        pr_json = json.dumps({
            "state": pr_state,
            "title": pr_title,
            "body": pr_body,
            "reviewDecision": "",
            "files": list(file_list),
        })
        self._write_stub("gh", f"""#!/usr/bin/env bash
echo "GH_CALLED: $*" >> '{self.call_log}'
case "$1" in
  pr)
    case "$2" in
      view)
        printf '%s\\n' '{pr_json}'
        exit 0
        ;;
      comment)
        # gh pr comment <N> --body <body> -- shift past 4 args to land
        # on the body text.
        shift; shift; shift; shift  # pr comment <N> --body
        BODY="$1"
        echo "GH_PR_COMMENT: $BODY" >> '{self.call_log}'
        exit 0
        ;;
      review)
        echo "GH_PR_REVIEW: $*" >> '{self.call_log}'
        exit 0
        ;;
    esac
    ;;
  repo)
    echo '{repo_full}'
    exit 0
    ;;
  api)
    echo '[]'
    exit 0
    ;;
esac
exit 0
""")

    def _stub_claude(self, body: str = "**Verdict:** Approve\nSome prose.") -> None:
        self._write_stub("claude", f"""#!/usr/bin/env bash
echo "CLAUDE_CALLED: $*" >> '{self.call_log}'
printf '%s\\n' '{body}'
exit 0
""")

    def _stub_real_python3(self) -> None:
        """Symlink the real python3 so `python3 -m lib.maintenance_gate`
        still works. The stub bin/ dir comes first on PATH so its
        `gh` and `claude` win, but `python3` resolves here.
        """
        # We don't actually need to symlink -- the test runner's python3
        # is already on the unstubbed PATH. The script's calls go to
        # `python3 -m lib.maintenance_gate ...`, which uses the real
        # interpreter. No action needed.
        return

    def _run_with_stubs(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        """Run `bin/review-local.sh` with the stub binaries on PATH and
        CI_REVIEW_PROVIDER + ANTHROPIC_API_KEY pre-set in env so the
        script's provider/key resolution doesn't die before reaching
        the gate under test.
        """
        env_overrides = {
            "CI_REVIEW_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-fake-for-test",
        }
        if env_extra:
            env_overrides.update(env_extra)
        return _run(*args, path=self.new_path, env=env_overrides)

    def _calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def test_bump_pr_skips_llm_and_emits_audit_marker(self) -> None:
        """A bump-PR title short-circuits BEFORE any claude call.
        The audit marker must show `source=bin_review_local
        (bump-PR skip)` so downstream audits can grep for it.
        """
        self._stub_gh(pr_title="chore(release): bump dev-kit to v0.3.239")
        self._stub_claude()
        self._stub_real_python3()

        r = self._run_with_stubs("--pr", "605")
        self.assertEqual(r.returncode, 0, r.stderr)

        calls = self._calls()
        # No claude call -- the bump-PR path skips the LLM judge.
        self.assertFalse(
            any(c.startswith("CLAUDE_CALLED") for c in calls),
            "bump-PR must not invoke claude",
        )
        # Audit comment is posted with the bump-PR skip marker. The
        # multi-line body (parseable quartet + human table + trailing
        # marker) lands in the log as one entry with embedded newlines;
        # splitlines() in `_calls()` turns it into many entries, so
        # check across the whole log, not just the first line.
        comments = [c for c in calls if c.startswith("GH_PR_COMMENT")]
        self.assertEqual(len(comments), 1, f"expected one comment, got: {comments}")
        joined = "\n".join(calls)
        self.assertIn("bump-PR skip", joined)

    def test_auto_approve_refuses_on_non_approve_verdict(self) -> None:
        """Security-judge finding: a Changes Requested verdict + --auto-approve
        must REFUSE -- it must NOT call `gh pr review --approve`.
        """
        self._stub_gh(pr_files=["lib/babysit_pr_cli.py"])
        # The maintenance judge returns Blocked; combined verdict != Approve.
        # Use --maintenance-only so review + security don't override.
        self._stub_claude(body="**Verdict:** Changes Requested\nMore findings.")
        self._stub_real_python3()

        r = self._run_with_stubs(
            "--pr", "605",
            "--maintenance-only",
            "--auto-approve",
        )
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("auto-approve refused", r.stderr)

        calls = self._calls()
        self.assertFalse(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"--auto-approve must not call `gh pr review --approve`; got: {calls}",
        )

    def test_auto_approve_refuses_on_l3_evidence_failure(self) -> None:
        """Security-judge finding: PR body lacks pytest tail line AND
        touches production code AND --auto-approve is set -- the gate
        must REFUSE without calling gh pr review --approve.
        """
        self._stub_gh(
            pr_title="feat(ci): local CI",
            pr_body="",  # no pytest tail line
            pr_files=["bin/review-local.sh"],
        )
        self._stub_claude(body="**Verdict:** Approve\nAll good.")
        self._stub_real_python3()

        r = self._run_with_stubs(
            "--pr", "605",
            "--review-only",
            "--auto-approve",
        )
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("L3-evidence", r.stderr)

        calls = self._calls()
        self.assertFalse(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"L3 failure must not allow auto-approve; got: {calls}",
        )

    def test_auto_approve_refuses_on_empty_judge_output(self) -> None:
        """Maintenance-judge finding: empty judge output must REFUSE
        --auto-approve. A gate that approves when its input is missing
        is worse than no gate.
        """
        # `claude` returns only whitespace -- no `**Verdict:**` line.
        # The extract step yields "" -- not "Approve".
        self._stub_gh(pr_files=["bin/review-local.sh"], pr_body="47 passed in 1.23s")
        self._stub_claude(body="\n\n")
        self._stub_real_python3()

        r = self._run_with_stubs(
            "--pr", "605",
            "--review-only",
            "--auto-approve",
        )
        self.assertNotEqual(r.returncode, 0, r.stderr)
        # Either "empty judge output" or "auto-approve refused" surfaces.
        self.assertTrue(
            "auto-approve refused" in r.stderr or "empty judge" in r.stderr,
            f"expected auto-approve refusal; stderr={r.stderr!r}",
        )

        calls = self._calls()
        self.assertFalse(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"empty verdict must not allow auto-approve; got: {calls}",
        )

    def test_no_touch_probe_runs_l3_strictly(self) -> None:
        """Review-judge nit: --no-touch-probe must STILL run the L3
        regex (force treats every PR as production-touching), not
        disable the gate entirely.

        Setup: PR body has NO pytest tail line; PR files are docs-only
        (which would normally skip L3 because touches_prod=false).
        --no-touch-probe forces L3 to run; the regex sees no tail
        line, so the combined path returns exit 1, and --auto-approve
        refuses with L3-evidence reason.
        """
        self._stub_gh(
            pr_title="fix(docs): small doc edit",
            pr_body="no pytest output here",
            pr_files=["docs/local-ci.md"],
        )
        self._stub_claude(body="**Verdict:** Approve\nFine.")
        self._stub_real_python3()

        r = self._run_with_stubs(
            "--pr", "605",
            "--review-only",
            "--no-touch-probe",
            "--auto-approve",
        )
        self.assertNotEqual(r.returncode, 0, r.stderr)
        # The L3 check fired (not bypassed), and auto-approve refused.
        self.assertIn("L3-evidence", r.stderr)

        calls = self._calls()
        self.assertFalse(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"--no-touch-probe must not disable the L3 gate; got: {calls}",
        )

    def test_provider_flag_applied_before_api_key_resolution(self) -> None:
        """Maintenance-judge finding: --provider must override .env
        BEFORE the API key is resolved. We verify by setting
        CI_REVIEW_PROVIDER=minimax in env (without MINIMAX_API_KEY)
        and passing --provider anthropic with env:ANTHROPIC_API_KEY.
        The script must NOT die on `no API key` -- it must pick up
        the anthropic provider from the flag and the matching key
        from env. (Previously the script resolved minimax from .env,
        then swapped to anthropic after the key was already loaded,
        silently sending the wrong key to the wrong endpoint.)
        """
        self._stub_gh(pr_files=["lib/x.py"], pr_body="47 passed in 1.23s")
        self._stub_claude(body="**Verdict:** Approve\nFine.")
        self._stub_real_python3()

        env_extra = {
            "CI_REVIEW_PROVIDER": "minimax",  # would-be fallback (no MINIMAX_API_KEY in env)
            "ANTHROPIC_API_KEY": "sk-ant-fake-for-test",
        }
        r = self._run_with_stubs(
            "--pr", "605",
            "--review-only",
            "--provider", "anthropic",
            env_extra=env_extra,
        )
        # Exit 0 on clean verdict (no --auto-approve).
        self.assertEqual(r.returncode, 0, r.stderr)
        # Belt-and-braces: no "no API key" die message.
        self.assertNotIn("no API key", r.stderr)

    # -------------------------------------------------------------------
    # read_pr_fields() NUL-delimiter regression (discovered live against
    # a real PR): the line-counting parser capped PR_FIELDS at exactly
    # 5 array elements total across ALL FIVE fields (state, title,
    # reviewDecision, body, files-join). Any PR body longer than ~1
    # line -- i.e. virtually every real PR -- pushed PR_BODY and
    # PR_FILES to read garbage/truncated content, because bash's
    # `read -r line` treats every embedded newline in `body` as its
    # own array slot, consuming the budget meant for the trailing
    # files-join field. The observed real-world effect: PR_FILES ended
    # up empty, so the production-file touch-probe silently failed
    # open -- a real code PR was misclassified as "docs/infra-only",
    # downgrading the L3-evidence gate to advisory-only and letting
    # --auto-approve pass WITHOUT the required pytest-tail evidence.
    # -------------------------------------------------------------------
    def test_multiline_body_prod_file_missing_evidence_refuses_auto_approve(self) -> None:
        """Security-relevant regression: a multi-line PR body with NO
        pytest tail line, touching a real production file
        (bin/tool.sh, not the first entry in the files list), MUST
        still refuse --auto-approve. Under the old line-counting
        parser, the multi-line body silently truncated PR_FILES to
        empty, the touch-probe fell through to 'docs/infra-only;
        advisory only', and --auto-approve incorrectly SUCCEEDED
        despite the missing evidence -- a false-positive approval.
        """
        long_body_no_tail = "\n".join([
            "## Why",
            "",
            "This change does something.",
            "",
            "## What",
            "",
            "Some prose here across several lines, no test evidence below.",
            "",
            "## Notes",
            "",
            "Nothing quantitative here.",
        ])
        self._stub_gh(
            pr_body=long_body_no_tail,
            pr_files=["docs/readme.md", "bin/tool.sh"],
        )
        self._stub_claude(body="**Verdict:** Approve\nFine.")
        self._stub_real_python3()

        r = self._run_with_stubs("--pr", "605", "--review-only", "--auto-approve")

        self.assertNotEqual(
            r.returncode, 0,
            f"auto-approve must REFUSE (production file touched + missing "
            f"L3 evidence in a multi-line body); "
            f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        self.assertIn("L3-evidence", r.stderr)
        calls = self._calls()
        self.assertFalse(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"must NOT auto-approve a production-file PR with missing "
            f"L3 evidence; calls={calls}",
        )

    def test_multiline_body_late_tail_line_detected(self) -> None:
        """Positive-path counterpart: the pytest tail line sits at the
        END of a long multi-line body (line 11 of an 11-line body).
        Correct parsing MUST find it (proving PR_BODY carries the
        FULL body text, not a single truncated line) and the
        production file (2nd of 2 in the files list, proving PR_FILES
        is the real list, not truncated/misaligned garbage).
        """
        long_body_with_tail = "\n".join([
            "## Why",
            "",
            "This change does something.",
            "",
            "## What",
            "",
            "Some prose here across several lines.",
            "",
            "## Verification",
            "",
            "47 passed in 1.23s",
        ])
        self._stub_gh(
            pr_body=long_body_with_tail,
            pr_files=["docs/readme.md", "bin/tool.sh"],
        )
        self._stub_claude(body="**Verdict:** Approve\nFine.")
        self._stub_real_python3()

        r = self._run_with_stubs("--pr", "605", "--review-only", "--auto-approve")

        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        calls = self._calls()
        self.assertTrue(
            any(c.startswith("GH_PR_REVIEW") for c in calls),
            f"auto-approve should succeed: L3 evidence IS present (late "
            f"in the body) and touches_prod correctly detects "
            f"'bin/tool.sh'; calls={calls}",
        )


# ---------------------------------------------------------------------------
# No-explicit-provider local-auth fallback (operator finding, 2026-08-11):
# an interactive local session almost always already has an authenticated
# `claude` CLI (claude.ai login or a keychain-stored key) -- the CI-style
# explicit ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL injection exists so a
# GH-Actions runner (which has no interactive login) can authenticate.
# Forcing the same requirement onto local operators who never asked for a
# specific provider (no --provider flag, no CI_REVIEW_PROVIDER env, no
# real .env — `.env.example`'s CI_REVIEW_PROVIDER=minimax is a committed
# template default, not operator intent) breaks the "just works" case.
# ---------------------------------------------------------------------------
class TestLocalAuthFallback(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bindir = Path(self._tmp.name) / "bin"
        self.bindir.mkdir()
        self.call_log = self.bindir / "calls.log"
        self._real_path = os.environ.get("PATH", "")
        self.new_path = f"{self.bindir}{os.pathsep}{self._real_path}"

    def _write_stub(self, name: str, body: str) -> Path:
        p = self.bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def _stub_gh(self, pr_body: str = "") -> None:
        pr_json = json.dumps({
            "state": "OPEN",
            "title": "feat(ci): anything",
            "body": pr_body,
            "reviewDecision": "",
            "files": ["lib/x.py"],
        })
        self._write_stub("gh", f"""#!/usr/bin/env bash
echo "GH_CALLED: $*" >> '{self.call_log}'
case "$1" in
  pr)
    case "$2" in
      view) printf '%s\\n' '{pr_json}'; exit 0 ;;
      comment) echo "GH_PR_COMMENT" >> '{self.call_log}'; exit 0 ;;
      review) echo "GH_PR_REVIEW: $*" >> '{self.call_log}'; exit 0 ;;
    esac
    ;;
  repo) echo 'owner/repo'; exit 0 ;;
  api)  echo '[]'; exit 0 ;;
esac
exit 0
""")

    def _stub_claude_capturing_env(self, body: str = "**Verdict:** Approve\nFine.") -> None:
        """Stub `claude` that ALSO records which ANTHROPIC_* vars were
        present in its own environment at call time -- this is how the
        test distinguishes "provider env injected" from "bare inherited
        env" (the fallback path under test)."""
        self._write_stub("claude", f"""#!/usr/bin/env bash
echo "CLAUDE_CALLED: $*" >> '{self.call_log}'
env | grep -E '^ANTHROPIC_' | sort | sed 's/^/CLAUDE_SAW_ENV: /' >> '{self.call_log}' || echo "CLAUDE_SAW_ENV: (none)" >> '{self.call_log}'
printf '%s\\n' '{body}'
exit 0
""")

    def _run_clean(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        """Run the script with EVERY provider-related var explicitly
        nulled out (empty string), regardless of what the host shell or
        `.env.example` (a committed template, not operator config)
        would otherwise supply -- guarantees hermetic "no signal" runs.
        """
        env = os.environ.copy()
        for k in (
            "CI_REVIEW_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
        ):
            # Delete (not blank) -- an env var present with an empty
            # value is still "present" to the child `claude` stub's
            # `env | grep ANTHROPIC_` capture, which would falsely
            # look like injection. A truly clean operator shell never
            # set these keys at all.
            env.pop(k, None)
        env["PATH"] = self.new_path
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )

    def _calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        return self.call_log.read_text(encoding="utf-8").splitlines()

    def test_no_signal_falls_back_to_local_auth_no_die(self) -> None:
        """No --provider flag, no CI_REVIEW_PROVIDER env, no real .env
        (only the repo's committed .env.example declares a template
        default) -- the script must NOT die on 'no API key' and must
        still invoke claude (using its own local/inherited auth)."""
        self._stub_gh(pr_body="47 passed in 1.23s")
        self._stub_claude_capturing_env()

        r = self._run_clean("--pr", "605", "--review-only")

        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("no API key", r.stderr)
        self.assertNotIn("bad substitution", r.stderr)
        calls = self._calls()
        self.assertTrue(any(c.startswith("CLAUDE_CALLED:") for c in calls), calls)

    def test_no_signal_skips_provider_env_injection(self) -> None:
        """When falling back to local auth, the script must NOT inject
        ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL
        into the `claude -p` call -- an empty/fake override would
        actively break a session that already has valid local auth.
        """
        self._stub_gh(pr_body="47 passed in 1.23s")
        self._stub_claude_capturing_env()

        r = self._run_clean("--pr", "605", "--review-only")

        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        calls = self._calls()
        env_lines = [c for c in calls if c.startswith("CLAUDE_SAW_ENV:")]
        # Either no ANTHROPIC_* vars at all, or explicitly "(none)".
        self.assertTrue(
            env_lines == ["CLAUDE_SAW_ENV: (none)"] or env_lines == [],
            f"expected no injected ANTHROPIC_* vars, got: {env_lines}",
        )

    def test_explicit_provider_flag_with_missing_key_still_dies(self) -> None:
        """An EXPLICIT --provider ask that has no matching key is a real
        misconfiguration -- unlike the no-signal case, this must still
        fail loudly (the operator asked for something specific that
        cannot be satisfied)."""
        self._stub_gh()
        self._stub_claude_capturing_env()

        r = self._run_clean("--pr", "605", "--review-only", "--provider", "anthropic")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no API key for provider 'anthropic'", r.stderr)
        self.assertNotIn("bad substitution", r.stderr)

    def test_explicit_env_provider_with_missing_key_still_dies(self) -> None:
        """Same as above but the explicit signal comes from the
        CI_REVIEW_PROVIDER process env instead of --provider."""
        self._stub_gh()
        self._stub_claude_capturing_env()

        r = self._run_clean(
            "--pr", "605", "--review-only",
            env_extra={"CI_REVIEW_PROVIDER": "minimax"},
        )

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no API key for provider 'minimax'", r.stderr)
        self.assertNotIn("bad substitution", r.stderr)

    def test_no_caret_caret_bashism_in_script(self) -> None:
        """Portability regression guard: `${VAR^^}` (bash 4+ uppercase
        expansion) is NOT valid on macOS's default bash (3.2, GPLv2
        license freeze -- ships as /bin/bash). Any occurrence silently
        breaks every code path that reaches it on a stock Mac. Assert
        the pattern is gone for good."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("^^}", text, "found a bash-4-only ${VAR^^} uppercase expansion")


# ---------------------------------------------------------------------------
# Cwd-independence regression (issue #619 D2). Previously
# bin/review-local.sh derived REPO_ROOT from BASH_SOURCE, which required
# the script to live at `<repo>/bin/review-local.sh`. The fix replaces
# the BASH_SOURCE dance with `git rev-parse --show-toplevel` so the
# script resolves REPO_ROOT from cwd's git toplevel.
#
# This test mirrors the install pattern: copy the bin/scripts + lib/
# helpers into a tmpdir consumer (exactly what ci-setup would do), then
# run the script from a directory OUTSIDE the consumer. `--help` is
# chosen because it short-circuits at L126 before any `gh pr view`
# call (a meaningful difference from `--pr 0 --dry-run`, where `gh pr
# view 0` returns non-zero in a throwaway repo).
# ---------------------------------------------------------------------------
class TestCwdIndependence(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.consumer = Path(self._tmp.name) / "consumer"
        self.consumer.mkdir()
        # Consumer must be a git repo so `git rev-parse --show-toplevel`
        # works when invoked from the consumer or from outside.
        subprocess.run(["git", "init", "-q"], cwd=self.consumer, check=True)
        # Mirror what ci-setup would install: bin/ + lib/ but nothing else.
        for filetype, rels in (
            ("bin", ("babysit-pr-local.sh", "review-local.sh")),
            ("lib", ("review_local_lib.sh", "maintenance_gate.py", "atomic.py", "__init__.py")),
        ):
            (self.consumer / filetype).mkdir()
            for name in rels:
                src = PROJECT_ROOT / filetype / name
                self.assertTrue(src.is_file(), f"plugin source missing: {src}")
                dst = self.consumer / filetype / name
                dst.write_bytes(src.read_bytes())
                # Match the +x bit that ci-setup would set.
                dst.chmod(dst.stat().st_mode | 0o111)

    def test_review_local_runs_from_arbitrary_cwd(self) -> None:
        """Issue #619 D2 regression: `bin/review-local.sh` must resolve
        REPO_ROOT from cwd's git toplevel, not from BASH_SOURCE. Mirror
        an install into a tmpdir consumer, then invoke the script from
        an unrelated directory (NOT inside the consumer). `--help` is
        the shortest code path that exits successfully before any `gh`
        call, so it proves cwd-independence without the noise of
        fake-PR / unauthenticated-gh failures.
        """
        # /tmp is guaranteed to exist on every Unix; we cd INTO it but
        # not into the consumer. The script must still locate the
        # consumer's git toplevel.
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        self.assertFalse(
            outside.resolve().is_relative_to(self.consumer.resolve()),
            "test setup wrong: outside should not be inside the consumer",
        )

        r = subprocess.run(
            ["bash", str(self.consumer / "bin" / "review-local.sh"), "--help"],
            cwd=outside,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            r.returncode, 0,
            f"expected exit 0 from cwd={outside}; got {r.returncode} "
            f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        # The help banner is the same as the in-repo test, so just
        # verify the script's own header text renders.
        self.assertIn("review-local.sh", r.stdout)
        # And NO "not in a git repo" error — that would mean the
        # BASH_SOURCE regression is back.
        self.assertNotIn("not in a git repo", r.stderr)


# ---------------------------------------------------------------------------
# Issue #727: regression for `claude -p` missing `--plugin-dir`. The
# GH-Actions sibling `bin/ci-claude-p.sh` correctly passes
# `--plugin-dir "$PLUGIN_SRC"` so /dev-kit:review, /dev-kit:security,
# /dev-kit:maintenance slash commands resolve. The local mirror
# previously called bare `claude -p "$prompt"`, so the slash commands
# resolved to "Unknown command" and the gate silently defaulted all
# three verdicts to Approve (a false positive).
#
# Tests lock the fix:
#   1. test_dry_run_argv_contains_plugin_dir: behavioral -- spawns the
#      script with stub `gh` + `claude`, runs --dry-run, asserts the
#      captured claude argv contains `--plugin-dir`.
#   2. test_plugin_src_script_source: static check on the script source
#      for the PLUGIN_SRC derivation + the manifest guard. Cheap;
#      catches accidental removal even if the stub infra regresses.
#   3. test_missing_manifest_dies / test_wrong_manifest_name_dies:
#      behavioral negative-path coverage for the two `die()` branches
#      that ARE the fix (review finding #1, PR #741) -- without these,
#      a `die` -> `log` warning swap re-introduces issue #727 silently,
#      since neither of the two tests above exercises the failure path.
# ---------------------------------------------------------------------------
class TestReviewLocalPluginDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stub_bin = Path(self._tmp.name) / "bin"
        self.stub_bin.mkdir()
        self.call_log = Path(self._tmp.name) / "calls.log"
        real_path = os.environ.get("PATH", "/usr/bin:/bin")
        self.new_path = f"{self.stub_bin}:{real_path}"

    def _write_stub(self, name: str, body: str) -> None:
        p = self.stub_bin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | 0o111)

    def _stub_gh_open_pr(self) -> None:
        pr_json = json.dumps({
            "state": "OPEN",
            "title": "feat: anything",
            "body": "",
            "reviewDecision": "",
            "files": ["lib/x.py"],
        })
        self._write_stub("gh", f"""#!/usr/bin/env bash
echo "GH_CALLED: $*" >> '{self.call_log}'
case "$1" in
  pr)
    case "$2" in
      view) printf '%s\\n' '{pr_json}'; exit 0 ;;
      comment) exit 0 ;;
      review) exit 0 ;;
    esac ;;
  repo) echo 'owner/repo'; exit 0 ;;
  api) echo '[]'; exit 0 ;;
esac
exit 0
""")

    def _stub_claude(self) -> None:
        self._write_stub("claude", f"""#!/usr/bin/env bash
echo "CLAUDE_CALLED: $*" >> '{self.call_log}'
exit 0
""")

    def _run_with_stubs(self, *args: str) -> subprocess.CompletedProcess:
        return _run(
            *args,
            path=self.new_path,
            env={
                "CI_REVIEW_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "sk-ant-fake-for-test",
            },
        )

    def test_dry_run_argv_contains_plugin_dir(self) -> None:
        """Issue #727: every `claude -p` invocation MUST carry
        `--plugin-dir "$PLUGIN_SRC"` so the spawned process loads
        /dev-kit:* slash commands. Regression: bare `claude -p "$prompt"`
        made /dev-kit:review / /dev-kit:security / /dev-kit:maintenance
        resolve to "Unknown command" and the gate silently defaulted
        all three verdicts to Approve.

        We assert on the dry-run log (which mirrors the real argv shape
        per the script's contract) instead of capturing real `claude`
        argv, because the stub-binary path is fully hermetic and the
        dry-run print is the audit-visible record of what the gate
        *would* have done.
        """
        self._stub_gh_open_pr()
        self._stub_claude()
        r = self._run_with_stubs("--pr", "1", "--dry-run")
        # Each gate emits a `would run: env ... claude --plugin-dir ... -p
        # ...` line (review finding #5, PR #741: --plugin-dir precedes
        # -p to match bin/ci-claude-p.sh:198-203's canonical argv
        # order). Count them; 3 gates -> >=3 dry-run prints. Match on
        # "claude " (not "claude -p") so the assertion stays valid
        # regardless of exact flag ordering.
        would_run_lines = [
            line for line in r.stdout.splitlines()
            if line.strip().startswith("would run:") and "claude " in line
        ]
        self.assertGreaterEqual(
            len(would_run_lines), 3,
            f"expected >=3 'would run: claude ...' lines (one per gate); "
            f"got {len(would_run_lines)}. stdout={r.stdout!r} "
            f"stderr={r.stderr!r}",
        )
        for i, line in enumerate(would_run_lines):
            self.assertIn(
                "--plugin-dir", line,
                f"dry-run print #{i} missing --plugin-dir (issue #727): "
                f"{line!r}",
            )
            self.assertIn(
                " -p ", line,
                f"dry-run print #{i} missing -p flag: {line!r}",
            )

    def test_plugin_src_script_source(self) -> None:
        """Static check on the script source. Catches accidental removal
        of PLUGIN_SRC even if the integration stub infra regresses.
        """
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('PLUGIN_SRC="$REPO_ROOT"', text,
            "PLUGIN_SRC derivation missing -- issue #727 not fixed")
        self.assertIn(".claude-plugin/plugin.json", text,
            "PLUGIN_SRC guard (manifest check) missing")
        self.assertIn('--plugin-dir "$PLUGIN_SRC"', text,
            "claude -p call missing --plugin-dir -- issue #727 not fixed")

    def _install_manifestless_consumer(self, tmp_path: Path) -> Path:
        """Mirror TestCwdIndependence's install pattern: bin/ + lib/
        only, no .claude-plugin/. Returns the consumer directory.
        """
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=consumer, check=True)
        for filetype, rels in (
            ("bin", ("review-local.sh",)),
            ("lib", ("review_local_lib.sh", "maintenance_gate.py", "atomic.py", "__init__.py")),
        ):
            (consumer / filetype).mkdir()
            for name in rels:
                src = PROJECT_ROOT / filetype / name
                dst = consumer / filetype / name
                dst.write_bytes(src.read_bytes())
                dst.chmod(dst.stat().st_mode | 0o111)
        return consumer

    def test_missing_manifest_dies(self) -> None:
        """Review finding #1 (MAJOR, PR #741): the die() branch at the
        manifest-existence check is the fix for issue #727's silent-
        Approve regression. A swap of `die` back to a `log` warning
        would reproduce #727 undetected without this test.

        Security finding A10 (PR #741): duration assertion catches a
        regression that hangs ~15s on a stat() of a stalled network
        mount before subprocess.run's timeout kills it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            consumer = self._install_manifestless_consumer(Path(tmp))
            t0 = time.monotonic()
            r = subprocess.run(
                ["bash", str(consumer / "bin" / "review-local.sh"), "--pr", "1", "--dry-run"],
                cwd=consumer,
                capture_output=True,
                text=True,
                timeout=15,
            )
            elapsed = time.monotonic() - t0
            self.assertNotEqual(r.returncode, 0, f"expected non-zero exit; stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("plugin manifest not found", r.stderr,
                f"expected manifest-not-found die message; stderr={r.stderr!r}")
            self.assertLess(elapsed, 5.0,
                f"manifest-existence check should fail fast (<5s); took {elapsed:.2f}s -- likely hung stat() regression")

    def test_wrong_manifest_name_dies(self) -> None:
        """Review finding #1 (MAJOR, PR #741): the die() branch that
        rejects a manifest whose `name` != "dev-kit" is the F1-followup
        fix (local judge finding A08). Without this test, removing the
        name check re-opens the substituted-plugin-source gap silently.

        Security finding A10 (PR #741): duration assertion catches a
        regression that hangs ~15s in the python3 manifest parse heredoc
        before subprocess.run's timeout kills it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            consumer = self._install_manifestless_consumer(Path(tmp))
            (consumer / ".claude-plugin").mkdir()
            (consumer / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "not-dev-kit"}), encoding="utf-8",
            )
            t0 = time.monotonic()
            r = subprocess.run(
                ["bash", str(consumer / "bin" / "review-local.sh"), "--pr", "1", "--dry-run"],
                cwd=consumer,
                capture_output=True,
                text=True,
                timeout=15,
            )
            elapsed = time.monotonic() - t0
            self.assertNotEqual(r.returncode, 0, f"expected non-zero exit; stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn('does not declare name="dev-kit"', r.stderr,
                f"expected name-mismatch die message; stderr={r.stderr!r}")
            self.assertLess(elapsed, 5.0,
                f"manifest-parse + name-check should fail fast (<5s); took {elapsed:.2f}s -- likely hung python3 heredoc regression")

    def test_symlink_manifest_dies(self) -> None:
        """Security finding A06 (PR #741): `[ ! -f ... ]` accepts a
        symlink-to-regular-file, and json.load then slurps the link
        target unbounded. A symlink at .claude-plugin/plugin.json must
        be refused with a distinct die message so the operator sees
        "refusing to follow" instead of the misleading parse-error.
        """
        with tempfile.TemporaryDirectory() as tmp:
            consumer = self._install_manifestless_consumer(Path(tmp))
            (consumer / ".claude-plugin").mkdir()
            # Symlink to a benign regular file; the size/symlink guard
            # must catch it BEFORE the python3 parser runs.
            target = consumer / "benign.json"
            target.write_text(json.dumps({"name": "dev-kit"}), encoding="utf-8")
            (consumer / ".claude-plugin" / "plugin.json").symlink_to(target)
            r = subprocess.run(
                ["bash", str(consumer / "bin" / "review-local.sh"), "--pr", "1", "--dry-run"],
                cwd=consumer,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(r.returncode, 0, f"expected non-zero exit on symlink manifest; stdout={r.stdout!r} stderr={r.stderr!r}")
            self.assertIn("is a symlink", r.stderr,
                f"expected symlink-refused die message; stderr={r.stderr!r}")

    def test_repo_root_spoofing_dies(self) -> None:
        """Review finding #1 (PR #741): `manifest_guard_log` was
        previously called at the REPO_ROOT-spoofing branch BEFORE the
        function was textually defined. Under `set -euo pipefail` bash
        fails with "command not found" (exit 127) instead of the
        intended `die()` with the security warning. Reproduced live
        with a consumer whose cwd is in a different git toplevel than
        the script's own BASH_SOURCE checkout.

        The fix moves the function definition ABOVE the call site;
        this test guards against the ordering regression.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Two separate git repos: one for the script (via
            # PROJECT_ROOT/bin/review-local.sh), one for the cwd.
            other_repo = tmp_path / "other"
            other_repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
            # Mirror the bin/lib install into the OTHER repo's bin/ so
            # the script resolves and the script's BASH_SOURCE-anchored
            # realpath differs from cwd's git-toplevel. Include a valid
            # manifest so the spoofing branch (which fires AFTER the
            # existence/name check) is the one that trips.
            # To trigger the spoofing check, we need:
            #   - cwd's git-toplevel (REPO_ROOT via line 87) = other_repo
            #   - script's BASH_SOURCE-anchored realpath (SCRIPT_REPO_REAL) = PROJECT_ROOT
            # So the script must NOT be installed under other_repo; we
            # invoke the real PROJECT_ROOT/bin/review-local.sh directly.
            # That means PROJECT_ROOT needs its own .claude-plugin/
            # valid manifest (so the script's manifest guard passes
            # before the spoofing check fires at line ~177) -- and
            # PROJECT_ROOT already has one (the dev-kit plugin source).
            r = subprocess.run(
                ["bash", str(PROJECT_ROOT / "bin" / "review-local.sh"), "--pr", "1", "--dry-run"],
                cwd=other_repo,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(r.returncode, 0, f"expected non-zero exit on REPO_ROOT spoofing; stdout={r.stdout!r} stderr={r.stderr!r}")
            # The die() message names the actual attack: "refusing to
            # load dev-kit plugin from <other_repo> -- git toplevel
            # disagrees with the script's own checkout" -- plus the
            # `manifest_guard_log` "spoofing" line that fires right
            # before it. Either is a valid signal.
            self.assertTrue(
                "spoofing" in r.stderr.lower() or "refusing to load" in r.stderr.lower(),
                f"expected REPO_ROOT-spoofing die message; stderr={r.stderr!r}",
            )
            # Crucial: must NOT be a "command not found" error from
            # calling manifest_guard_log before its definition.
            self.assertNotIn("manifest_guard_log: command not found", r.stderr,
                f"manifest_guard_log must be defined before its call site; stderr={r.stderr!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
