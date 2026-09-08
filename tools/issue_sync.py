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
import re
import sys
from typing import Iterable

__version__ = "1.0.0"

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
_REF_KEYWORDS = (
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

    p.add_argument("--version", action="store_true", help="Print version and exit.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.version:
        print(f"issue_sync {__version__}")
        return 0
    if args.cmd != "parse":
        return 2  # pragma: no cover — argparse requires a subcommand

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


if __name__ == "__main__":
    raise SystemExit(main())
