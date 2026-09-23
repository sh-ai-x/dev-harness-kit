#!/usr/bin/env python3
"""tools/issue_sync.py — Parse GitHub-issue references from a PR body.

Used by ``.github/workflows/issue-sync.yml`` to enforce the one-time
sync gate:

  * If a PR references a GitHub issue (``#N`` or ``owner/repo#N``,
    optionally preceded by a close-keyword like ``Fixes`` / ``Closes``
    / ``Resolves``), every referenced issue MUST be ``open`` at the
    moment the gate runs. The contract: a PR cannot "sync" against an
    already-closed issue, because the work the PR delivers is no
    longer owed.
  * If a PR references no GitHub issue, the gate is skipped — sync is
    not owed and there is nothing to check.

Subcommands
-----------

``parse --body <text> [--title <text>] [--json] [--quiet]``
    Extract every ``#N`` / ``owner/repo#N`` reference from the PR body
    (and optionally the title). One JSON object per reference on stdout:

        {"ref": "#12",            "owner": null,       "repo": null,  "number": 12, "keyword": "fixes",   "source": "body"}
        {"ref": "owner/repo#7",   "owner": "owner",    "repo": "repo","number": 7,  "keyword": null,      "source": "title"}

    The reference itself is ``ref`` (the verbatim token). ``owner`` /
    ``repo`` are populated only for cross-repo (``owner/repo#N``)
    references; ``number`` is the integer. ``keyword`` is the
    normalized lowercase keyword that immediately preceded the
    reference (``fixes`` / ``closes`` / ``resolves`` / ``ref`` /
    ``refs`` / ``references`` / ``see`` / ``part of`` / ``related to``
    / ``tracking``) or ``null`` for bare ``#N`` mentions. ``source``
    is ``"title"`` or ``"body"``.

    ``--quiet`` exits 1 if no references were found (handy for shell
    gating). ``--json`` is implied when stdout is not a TTY.

``--version``
    Print the version banner and exit.

Design notes
------------

* The regex intentionally ignores Markdown link wrappers like
  ``[closes #12](https://...)`` — GitHub's own reference parser does
  the same, so the contract stays aligned with how the web UI counts
  references.
* Comments (``<!-- ... -->``) and fenced code blocks (```` ``` ````
  / ```` ~~~ ````) are stripped before scanning so an issue number
  discussed in a code example doesn't trip the gate. PR templates that
  embed ``#NN`` example rows fall in this bucket.
* Bare ``#N`` mentions (no keyword) ARE counted. A contributor who
  writes ``Tracking #123`` is still linking the PR to the issue, so
  the sync contract applies. If we ever want to narrow this to
  close-keywords only, drop the second alternation from
  ``_REF_PATTERN``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Iterable

__version__ = "1.1.0"

# GitHub's own closing-keywords (case-insensitive). See:
#   https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_CLOSE_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)

# Reference-only keywords. These do NOT auto-close the issue on merge,
# but the user explicitly chose to surface a link to it, so the sync
# contract still applies.
#
# v1.1.0 expansion: `issue` is added (case-insensitive) per issue #833
# and the recovered PR #832 proposal. The case-insensitive regex already
# matches `Issue`/`ISSUE`/`issue` regardless, so adding the lowercased
# canonical form is purely additive. PR #829's body used `Issue #N` (not
# `Closes #N`/`Related to #N`) for closed refs preserved for audit
# trail; the previous keyword set silently missed them, so the gate
# fired HARD FAIL even when the developer intended a reference. With
# `issue` in the set, the lenient branch can WARN on those refs without
# breaking the strict-by-default contract.
_REF_KEYWORDS = (
    "issue",
    "ref",
    "refs",
    "reference",
    "references",
    "see",
    "part of",
    "related to",
    "tracking",
)

_ALL_KEYWORDS = _CLOSE_KEYWORDS + _REF_KEYWORDS

# The full reference pattern. Two halves:
#   1. An optional leading keyword (with trailing whitespace) from
#      _ALL_KEYWORDS, captured as `kw` so we can recover it from
#      `match.group("kw")` rather than scanning the substring to the
#      left of the match. Scanning the substring would miss keywords
#      that appear at the start of the text — the keyword is part of
#      the overall match span, so `match.start()` lands at 0 and the
#      look-back window is empty.
#   2. The reference itself: `owner/repo#N` (cross-repo) or `#N`.
# `\b` at the end of `(?P<ref>...)` keeps `foo#bar` slugs from matching.
_REF_PATTERN = (
    r"(?:(?P<kw>"
    + "|".join(re.escape(k) for k in _ALL_KEYWORDS)
    + r")\s+)?"
    r"(?P<ref>(?:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}))?#(?P<number>[0-9]+))\b"
)
_REFERENCE_RE = re.compile(_REF_PATTERN, re.IGNORECASE | re.MULTILINE)


def _strip_code_and_comments(text: str) -> str:
    """Remove fenced code blocks + HTML comments before scanning.

    PR templates and design docs routinely embed ``#NN`` inside ````
    code fences or ``<!-- -->`` HTML comments as examples / template
    filler. Without stripping these, every template-shaped PR
    would falsely trip the gate. The strip is intentionally crude —
    it is a pre-filter, not a parser — so a fence spanning many lines
    or a multi-line HTML comment is fine.
    """
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def _iter_references(text: str, source: str) -> Iterable[dict]:
    """Yield one record per unique ``(ref, keyword)`` reference."""
    if not text:
        return
    seen: set[tuple[str, str | None]] = set()
    for match in _REFERENCE_RE.finditer(text):
        ref = match.group("ref")
        owner = match.group("owner")
        # Keyword is captured inside the regex span itself (named
        # group `kw`). `None` means a bare `#N` mention with no
        # leading keyword.
        keyword_raw = match.group("kw")
        keyword = keyword_raw.lower() if keyword_raw else None
        key = (ref, keyword)
        if key in seen:
            continue
        seen.add(key)
        yield {
            "ref": ref,
            "owner": owner,
            "repo": match.group("repo") if owner else None,
            "number": int(match.group("number")),
            "keyword": keyword,
            "source": source,
        }


def parse_references(body: str, title: str = "") -> list[dict]:
    """Return the list of reference records for a PR (title + body).

    Order: title refs first (in body order), then body refs (in body
    order). Duplicates are dropped by ``(ref, keyword)``.
    """
    refs: list[dict] = []
    refs.extend(_iter_references(_strip_code_and_comments(title or ""), "title"))
    refs.extend(_iter_references(_strip_code_and_comments(body or ""), "body"))
    return refs


# Keyword classification used by the ``pre-push`` subcommand to decide
# hard-fail vs warn under ``--lenient``. Mirrors the `_CLOSE_KEYWORDS` /
# `_REF_KEYWORDS` split above; rebuilt from those tuples to keep the
# table the SSOT and avoid drift.
def _keyword_class(keyword: str | None) -> str:
    """Return ``"close"``, ``"ref"``, or ``"bare"`` for a keyword string."""
    if not keyword:
        return "bare"
    k = keyword.lower()
    if k in {x.lower() for x in _CLOSE_KEYWORDS}:
        return "close"
    return "ref"


# Issue-state lookup keys used by the ``pre-push`` subcommand.
_STATE_OPEN = "open"
_STATE_CLOSED = "closed"

# Exit codes used by ``pre-push``. Distinguishing exit-1 (sync gate
# failed) from exit-0 (passed) lets the workflow's ``if: failure()``
# step surface the right verdict without parsing JSON.
_EXIT_OK = 0
_EXIT_FAIL = 1


def _check_ref_states(
    refs: list[dict],
    *,
    lenient: bool,
    offline: bool,
    api_token: str | None = None,
    repo: str | None = None,
) -> dict:
    """Run the issue-state check loop against ``refs``.

    Returns a dict shaped as the contract for the ``pre-push``
    subcommand JSON output::

        {
            "errors":   [ {"ref": "...", "message": "..."}, ... ],
            "warnings": [ {"ref": "...", "message": "..."}, ... ],
            "refs":     [ {parsed ref dict}, ... ],
            "skipped":  bool,   # true iff --offline or zero refs
            "lenient":  bool,
        }

    In strict mode, every closed ref becomes an ``errors`` entry. In
    lenient mode, only close-keyword refs (``closes``/``fixes``/
    ``resolves`` and friends) and bare ``#N`` mentions become errors;
    reference-only keywords (``issue``/``ref``/``refs``/``see``/``part
    of``/``related to``/``tracking``) become warnings. Bare ``#N`` is
    treated as ambiguous under lenient — without an explicit keyword,
    the developer is presumed to mean ``closes``, so a closed ref
    still fails.

    Network: when ``offline`` is true, every ref is reported as
    ``state=unknown`` (no ``gh api`` call). When ``offline`` is false
    and ``gh api`` fails for any ref (404 missing issue, 403 token
    scope, transport error), the ref becomes an error — a stale ref
    must surface, not silently pass.
    """
    out: dict = {
        "errors": [],
        "warnings": [],
        "refs": [dict(r) for r in refs],
        "skipped": False,
        "lenient": lenient,
    }

    if not refs:
        out["skipped"] = True
        return out

    if offline:
        # Skip ``gh api`` entirely. The local hook runs in offline
        # mode when ``gh auth status`` fails so a developer without
        # auth still gets the parse-time signal (e.g. "3 refs found,
        # state NOT verified"). Re-run after ``gh auth login`` for
        # state-verified results.
        out["skipped"] = True
        for r in refs:
            out["warnings"].append(
                {
                    "ref": r["ref"],
                    "message": (
                        f"{r['ref']} state NOT verified (offline mode); "
                        f"run with auth for full sync gate."
                    ),
                }
            )
        return out

    import os
    import shutil
    import subprocess

    gh = shutil.which("gh")
    if not gh:
        # ``gh`` missing — degrade to offline (matches the rationale
        # above: a developer without ``gh`` still gets the parse
        # signal; the remote GH Actions workflow is the source of
        # truth for state verification).
        out["skipped"] = True
        for r in refs:
            out["warnings"].append(
                {
                    "ref": r["ref"],
                    "message": (
                        f"{r['ref']} state NOT verified (gh not on PATH); "
                        f"install gh CLI for full sync gate."
                    ),
                }
            )
        return out

    env = os.environ.copy()
    if api_token:
        env["GH_TOKEN"] = api_token

    for r in refs:
        ref = r["ref"]
        number = r["number"]
        if r.get("owner"):
            api_path = f"repos/{r['owner']}/{r['repo']}/issues/{number}"
        else:
            api_path = f"repos/{repo}/issues/{number}" if repo else None
            if not api_path:
                out["errors"].append(
                    {
                        "ref": ref,
                        "message": (
                            f"{ref} has no owner/repo and --repo was not "
                            f"supplied; skipping."
                        ),
                    }
                )
                continue

        try:
            cp = subprocess.run(
                [gh, "api", api_path, "--jq", ".state"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            out["errors"].append(
                {"ref": ref, "message": f"{ref} gh api timed out after 10s."}
            )
            continue

        state = (cp.stdout or "").strip()
        if cp.returncode != 0 or not state:
            out["errors"].append(
                {
                    "ref": ref,
                    "message": (
                        f"{ref} state fetch failed (gh rc={cp.returncode}); "
                        f"reference may be stale or token scope insufficient."
                    ),
                }
            )
            continue

        if state == _STATE_OPEN:
            continue  # ok — no entry needed

        if state != _STATE_CLOSED:
            out["errors"].append(
                {
                    "ref": ref,
                    "message": (
                        f"{ref} returned unexpected state '{state}' "
                        f"(expected open|closed)."
                    ),
                }
            )
            continue

        # state == closed
        kw_class = _keyword_class(r.get("keyword"))
        is_error = (not lenient) or kw_class in ("close", "bare")
        record = {
            "ref": ref,
            "message": (
                f"{ref} is closed ({r.get('keyword') or 'bare'} reference); "
                f"{'gate failed' if is_error else 'warn only (lenient mode)'}."
            ),
        }
        if is_error:
            out["errors"].append(record)
        else:
            out["warnings"].append(record)

    return out


def _run_pre_push(args) -> int:
    """Dispatch for the ``pre-push`` subcommand.

    Composes the input source (``--from-log`` vs explicit
    ``--pr-title``/``--pr-body``), runs parse, then runs the
    issue-state loop, and prints the structured JSON output.
    """
    title = args.pr_title or ""
    body = args.pr_body or ""

    if args.from_log:
        # Read commit subjects + bodies between ``--base`` and HEAD
        # on ``--remote`` so the local pre-push hook catches stale
        # refs in commit messages without a network call. ``%B`` is
        # the full commit message (subject + body). When ``--base``
        # is empty, default to ``<remote>/main`` so a feature branch
        # off main is the assumed base.
        import subprocess

        remote = args.remote or "origin"
        base = args.base or f"{remote}/main"
        try:
            cp = subprocess.run(
                [
                    "git",
                    "log",
                    f"{base}..HEAD",
                    "--format=%H%n%B%n--END--",
                    "-z",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            sys.stderr.write(
                f"pre-push: failed to read git log {base}..HEAD: {exc}\n"
            )
            return _EXIT_FAIL

        # Concatenate every commit body. ``--format=%B`` already
        # separates commits by NUL with ``-z``; the ``--END--``
        # marker is belt-and-suspenders for older git versions.
        messages = [
            m.strip()
            for m in cp.stdout.replace("--END--", "").split("\x00")
            if m.strip()
        ]
        # The first commit message (chronologically newest on push)
        # is the one that mattered for the recent PR description; we
        # concatenate all to be safe (multi-commit PRs).
        body = "\n\n".join(messages)

    refs = parse_references(body, title)
    result = _check_ref_states(
        refs,
        lenient=args.lenient,
        offline=args.offline,
        api_token=args.gh_token,
        repo=args.repo,
    )

    if args.json or not sys.stdout.isatty():
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")

    if result["errors"]:
        return _EXIT_FAIL
    return _EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue_sync",
        description="Parse GitHub-issue references from a PR body.",
    )
    # Two-pass parse: top-level args first so `--version` works without
    # a subcommand. `add_subparsers(required=False)` (instead of True)
    # keeps the parser from rejecting `--version` before the version
    # flag is read. `parse_known_args()` lets us consume `--version`
    # without argparse complaining about a missing subcommand.
    pre_args, _ = p.parse_known_args()
    if getattr(pre_args, "version", False):
        print(f"issue_sync {__version__}")
        sys.exit(0)
    sub = p.add_subparsers(dest="cmd", required=False)

    p_parse = sub.add_parser("parse", help="Parse references and print as JSON.")
    p_parse.add_argument("--body", default="", help="PR body text (Markdown).")
    p_parse.add_argument("--title", default="", help="PR title (optional).")
    p_parse.add_argument(
        "--quiet",
        action="store_true",
        help="Exit 1 if no references were found (shell-gating mode).",
    )
    p_parse.add_argument(
        "--json",
        action="store_true",
        help="Force JSON output even on a TTY (default: auto-detect).",
    )

    # `pre-push` — combined parse + gh-api loop. Single source of
    # truth for both the GH Actions gate (which passes the PR title +
    # body explicitly) and the local pre-push hook (which reads the
    # commit log between base and HEAD).
    p_pre = sub.add_parser(
        "pre-push",
        help="Parse + gh api loop (single source of truth for the gate).",
    )
    p_pre.add_argument("--pr-title", default="", help="PR title (workflow mode).")
    p_pre.add_argument("--pr-body", default="", help="PR body (workflow mode).")
    p_pre.add_argument(
        "--from-log",
        action="store_true",
        help="Read commits via `git log <base>..HEAD` instead of --pr-body.",
    )
    p_pre.add_argument("--remote", default="origin", help="Git remote (for --from-log).")
    p_pre.add_argument(
        "--base",
        default="",
        help="Base ref for --from-log (default: <remote>/main).",
    )
    p_pre.add_argument(
        "--repo",
        default="",
        help="owner/repo for same-repo refs (default: read from env GITHUB_REPOSITORY).",
    )
    p_pre.add_argument(
        "--lenient",
        action="store_true",
        help="WARN on closed ref-only keywords; HARD FAIL only on close-keyword + bare refs.",
    )
    p_pre.add_argument(
        "--strict",
        action="store_true",
        help="HARD FAIL on any closed ref (default for the local hook).",
    )
    p_pre.add_argument(
        "--offline",
        action="store_true",
        help="Skip gh api; parser-only mode with a per-ref warning.",
    )
    p_pre.add_argument(
        "--gh-token",
        default="",
        help="Override GH_TOKEN (defaults to env $GH_TOKEN).",
    )
    p_pre.add_argument(
        "--json",
        action="store_true",
        help="Force JSON output even on a TTY (default: auto-detect).",
    )

    p.add_argument("--version", action="store_true", help="Print version and exit.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.version:
        print(f"issue_sync {__version__}")
        return 0
    if args.cmd == "parse":
        refs = parse_references(args.body, args.title)
        if args.json or not sys.stdout.isatty():
            json.dump(refs, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            for r in refs:
                print(f"{r['source']}\t{r['ref']}\t{r['keyword'] or '-'}")

        if args.quiet and not refs:
            return 1
        return 0
    if args.cmd == "pre-push":
        # --lenient / --strict are mutually exclusive; default strict.
        # argparse can't enforce mutual exclusion on store_true flags
        # without a custom check, so resolve here.
        if args.lenient and args.strict:
            sys.stderr.write("pre-push: --lenient and --strict are mutually exclusive.\n")
            return 2
        if not args.lenient and not args.strict:
            args.lenient = False
            args.strict = True  # default
        elif args.strict:
            args.lenient = False
        # Resolve GH_TOKEN / GITHUB_REPOSITORY for the API loop.
        if not args.gh_token:
            args.gh_token = os.environ.get("GH_TOKEN", "")
        if not args.repo:
            args.repo = os.environ.get("GITHUB_REPOSITORY", "")
        return _run_pre_push(args)
    return 2  # pragma: no cover — argparse requires a subcommand


if __name__ == "__main__":
    raise SystemExit(main())
