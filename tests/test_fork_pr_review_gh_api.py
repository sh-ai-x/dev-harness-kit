#!/usr/bin/env python3
"""test_fork_pr_review_gh_api.py — regression for fork-pr-review.yml on pull_request_target.

Background:
  fork-pr-review.yml runs on `pull_request_target` and intentionally does NOT
  check out the fork's code (security policy — fork code is never executed).
  Result: the runner's working directory has no `.git` directory.

  The workflow posts commit-status updates via `gh api "repos/${{ github.repository }}/statuses/$SHA" -X POST`.
  The URL is relative (no `https://api.github.com/` prefix) and no `--repo` flag is passed.
  When `gh` resolves a relative URL, it walks a discovery chain:
    1. `--repo` flag — not set
    2. `GH_REPO` env var — not set
    3. `git rev-parse` against the current `.git` directory — fails because none exists
  Symptom: `failed to determine base repo: failed to run git: fatal: not a git
  repository (or any of the parent directories): .git` (observed on PR #665,
  run 32245678201, step "Post final AI-judge status to PR commit").

  The ONLY working fix is the absolute URL form:
    `https://api.github.com/repos/${{ github.repository }}/statuses/$SHA`
  Earlier PR #673 tried `--repo "${{ github.repository }}"` instead. `gh api`
  does NOT accept `--repo`, so the run fails with `unknown flag: --repo`
  (observed on PR #665, run 32255230444). This test rejects that form too.

This test pins that contract so the workflow cannot drift back to relying on
.git context discovery or to the rejected `--repo` flag.

Pin tests:
  T1: workflow file exists and parses as YAML.
  T2: every `gh api` invocation in any `run:` block uses an absolute URL
      (`https://api.github.com/...`) AND does NOT pass `--repo`.
  T3: specifically, the four commits-status POST calls (Mark PR commit,
      Post per-judge, Post final) all meet T2.
  T4: workflow still triggers on `pull_request_target` (intentional — that's
      the security boundary; if someone changes it to `pull_request`, the
      fork-trust model breaks).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

WORKFLOW_PATH = (
    Path(__file__).parent.parent / ".github" / "workflows" / "fork-pr-review.yml"
)


def _yaml_doc() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _all_run_blocks(doc: dict) -> list[tuple[str, str]]:
    """Yield (step_name, run_text) for every step that has a `run:` field."""
    out: list[tuple[str, str]] = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            name = step.get("name", "<unnamed>")
            run = step.get("run", "")
            if run:
                out.append((name, run))
    return out


def _check_gh_api_call(step_name: str, run_text: str) -> list[str]:
    """Return a list of failure messages for any `gh api` call in the run
    block that does NOT use an absolute URL.

    The only acceptable form is the absolute URL — `gh api` does NOT accept
    `--repo`, and a relative `repos/${{ github.repository }}/...` URL fails
    on `pull_request_target` because the runner has no `.git` directory for
    git-context discovery (observed on PR #665, runs 32245678201 / 32255230444).

    Earlier commits (#673) added `--repo "${{ github.repository }}"` to every
    `gh api` call. That looked plausible (the comment even explained the
    `.git`-less rationale), but `gh api` rejects the flag with
    `unknown flag: --repo`, so the gate stayed red. This test now pins the
    only working form: an absolute URL of the form
    `https://api.github.com/repos/${{ github.repository }}/...`.
    """
    # Collapse line-continuation backslashes so a multi-line `gh api ...\` block
    # becomes one virtual command whose args we can inspect.
    # Strip ANSI color codes (the workflow file is plain; this is defensive).
    plain = re.sub(r"\x1b\[[0-9;]*m", "", run_text)
    virtual_lines: list[str] = []
    buf: list[str] = []
    for raw in plain.splitlines():
        # Strip trailing `\` (line continuation) and accumulate.
        if raw.rstrip().endswith("\\"):
            buf.append(raw.rstrip()[:-1])
            continue
        buf.append(raw)
        virtual_lines.append(" ".join(buf))
        buf = []
    if buf:
        virtual_lines.append(" ".join(buf))

    failures: list[str] = []
    for virt in virtual_lines:
        # Find every `gh api` invocation start in the (joined) line.
        for m in re.finditer(r"\bgh\s+api\b", virt):
            # Extract the args starting after the `gh api` token.
            tail = virt[m.end():]
            # Bail if the very next non-space token is `|`, `&&`, `||`, `;`,
            # `<`, `>`, `(`, `)` — could be a piped call (rare but possible).
            # For our regression we only care about direct invocations.
            stripped = tail.lstrip()
            if not stripped or stripped[0] in "|;&<>()`":
                continue
            # Tokenize the args (whitespace split is sufficient for our
            # simple call shapes; quoted strings preserve the token).
            args = _tokenize(stripped)
            if not args:
                continue
            url = args[0]
            # Hard requirement: the URL must be absolute. A relative URL
            # like `repos/${{ github.repository }}/...` will fail because
            # `pull_request_target` has no `.git` directory for
            # git-context discovery.
            if not (url.startswith("https://") or url.startswith("http://")):
                failures.append(
                    f"step {step_name!r}: `gh api` call uses relative URL "
                    f"{url!r} — on `pull_request_target` the runner has no "
                    f".git directory, so git-context discovery fails with "
                    f"'failed to determine base repo'. Use the absolute "
                    f"`https://api.github.com/repos/${{ github.repository }}/...` "
                    f"form instead."
                )
                continue
            # Reject `--repo` on `gh api` explicitly. Earlier PR #673 added
            # `--repo "${{ github.repository }}"` to every `gh api` call,
            # but `gh api` rejects the flag with `unknown flag: --repo`,
            # so the gate stayed red. Pin the contract: never use --repo
            # on gh api.
            if _has_repo_flag(args[1:]):
                failures.append(
                    f"step {step_name!r}: `gh api` call uses `--repo` flag — "
                    f"`gh api` does NOT accept `--repo` and exits with "
                    f"`unknown flag: --repo`. The absolute URL form is "
                    f"self-resolving; --repo is unnecessary AND rejected "
                    f"(observed on PR #665, run 32255230444)."
                )
    return failures


def _tokenize(s: str) -> list[str]:
    """Minimal shell-style tokenizer: splits on whitespace, preserves
    double-quoted segments as a single token, drops the quoted quotes.
    Good enough for the `gh api ... -f key=value` shape we care about."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            # Closing-quoted segment.
            j = s.find('"', i + 1)
            if j == -1:
                # Unterminated quote — bail out, treat the rest as one token.
                out.append(s[i + 1:])
                return out
            out.append(s[i + 1:j])
            i = j + 1
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j == -1:
                out.append(s[i + 1:])
                return out
            out.append(s[i + 1:j])
            i = j + 1
            continue
        # Bare token: read until whitespace or quote.
        j = i
        while j < n and not s[j].isspace() and s[j] not in "\"'":
            j += 1
        out.append(s[i:j])
        i = j
    return out


def _has_repo_flag(args: list[str]) -> bool:
    """Return True iff `--repo <value>` appears in args (separate tokens)."""
    for i, a in enumerate(args):
        if a == "--repo" and i + 1 < len(args):
            return True
        # Tolerate the `--repo=...` form as well.
        if a.startswith("--repo="):
            return True
    return False


class TestForkPrReviewGhApi(unittest.TestCase):

    def test_01_workflow_file_exists(self):
        self.assertTrue(WORKFLOW_PATH.exists(),
                        f"missing workflow: {WORKFLOW_PATH}")

    def test_02_workflow_parses_as_yaml(self):
        doc = _yaml_doc()
        self.assertEqual(doc["name"], "Fork PR Review Gate")
        self.assertIn("jobs", doc)
        self.assertEqual(len(doc["jobs"]), 1)

    def test_03_pull_request_target_trigger(self):
        """The workflow must trigger on pull_request_target (not pull_request).
        That's the security boundary: a fork cannot exploit the workflow's
        write permissions because the workflow file is read from the base
        branch, not the fork's branch."""
        on_dict = doc_true_or_str(doc=_yaml_doc(), key="on")
        self.assertIn("pull_request_target", on_dict,
                      "fork-pr-review.yml must trigger on pull_request_target "
                      "so the trusted workflow file is used, not the fork's")
        self.assertNotIn("pull_request", on_dict,
                         "fork-pr-review.yml must NOT trigger on pull_request — "
                         "that would let the fork's workflow file run with "
                         "write permissions, defeating the trust boundary")

    def test_04_no_checkout_step(self):
        """The workflow must NOT check out the fork's code. That is the
        whole point of pull_request_target — we want the runner to never
        execute fork code. If anyone adds an `actions/checkout` step here,
        the security model breaks."""
        doc = _yaml_doc()
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                self.assertFalse(uses.startswith("actions/checkout"),
                                 f"step {step.get('name', '?')!r} uses "
                                 f"actions/checkout — would execute fork code")
        # Done implicitly — no actions/checkout step exists.

    def test_05_gh_api_calls_use_absolute_url_no_repo_flag(self):
        """Every `gh api` call in any `run:` block must use an absolute
        `https://api.github.com/...` URL and must NOT pass `--repo`
        (which `gh api` rejects with `unknown flag: --repo`).

        Relative URLs fail because `pull_request_target` has no `.git`
        directory for git-context discovery (PR #665 / run 32245678201).
        `--repo` was added by PR #673 as a workaround but does not work
        with `gh api` (PR #665 / run 32255230444). The absolute URL is
        the only form that works."""
        doc = _yaml_doc()
        all_failures: list[str] = []
        for name, run_text in _all_run_blocks(doc):
            all_failures.extend(_check_gh_api_call(name, run_text))
        self.assertEqual(all_failures, [],
                         "\n".join(all_failures) if all_failures else "")

    def test_06_specifically_the_three_commit_status_calls(self):
        """Pin the three calls that post commit statuses to the PR commit
        (the ones that fail on PR #665). Each must use an absolute URL and
        not pass --repo."""
        doc = _yaml_doc()
        names_to_pin = [
            "Mark PR commit as AI-review-pending",
            "Post per-judge commit statuses",
            "Post final AI-judge status to PR commit",
        ]
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if step.get("name") in names_to_pin:
                    failures = _check_gh_api_call(
                        step["name"], step.get("run", "")
                    )
                    self.assertEqual(
                        failures, [],
                        f"step {step['name']!r}: "
                        + "\n".join(failures))


def doc_true_or_str(doc: dict, key: str) -> dict:
    """PyYAML >=1.1 coerces bare `on:` to the boolean True. Tolerate both."""
    if isinstance(doc.get(True), dict):
        return doc[True]
    if isinstance(doc.get(key), dict):
        return doc[key]
    raise AssertionError(
        f"workflow has no `{key}:` triggers; doc keys = {list(doc)}"
    )


if __name__ == "__main__":
    unittest.main()
