#!/usr/bin/env python3
"""Publish one idempotent babysit phase transition to GitHub and Linear.

The local state file remains authoritative. This adapter is deliberately
best-effort: a tracker outage must not lose the phase, and the transition key
in the comment body prevents duplicate external events after a restart.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from linear_pr_sync import _request


def transition_marker(key: str) -> str:
    return f"<!-- babysit-transition:{key} -->"


def format_transition(
    *,
    key: str,
    pr_number: int,
    phase: str,
    strategy: str,
    head_sha: str,
    context_epoch: int,
    review_verdict: str,
    checks_summary: str,
) -> str:
    return "\n".join(
        [
            transition_marker(key),
            f"### babysit-pr phase: `{phase}`",
            "",
            f"- PR: #{pr_number}",
            f"- Strategy: `{strategy}`",
            f"- Head: `{head_sha}`",
            f"- Context epoch: `{context_epoch}`",
            f"- Review: `{review_verdict or 'REVIEW_REQUIRED'}`",
            f"- Checks: {checks_summary}",
            "- Resume: re-run `/dev-kit:babysit-pr`; local state remains authoritative.",
        ]
    )


def _gh_output(args: list[str]) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    return result.stdout


def publish_github(repo: str, issue: int, body: str) -> bool:
    marker = body.splitlines()[0]
    comments = _gh_output(
        ["gh", "api", f"repos/{repo}/issues/{issue}/comments", "--paginate"]
    )
    if marker in comments:
        return False
    result = subprocess.run(
        ["gh", "issue", "comment", str(issue), "--repo", repo, "--body", body],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def publish_linear(issue: str, body: str) -> bool:
    if not issue:
        return False
    query = """
    query($id: String!) {
      issue(id: $id) { comments(first: 100) { nodes { body } } }
    }
    """
    result = _request(query, {"id": issue})
    if not result:
        return False
    nodes = result.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])
    marker = body.splitlines()[0]
    if any(marker in str(node.get("body", "")) for node in nodes):
        return False
    mutation = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) { success }
    }
    """
    result = _request(mutation, {"issueId": issue, "body": body})
    return bool(result and result.get("data", {}).get("commentCreate", {}).get("success"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--github-issue", type=int, required=True)
    parser.add_argument("--linear-issue", default="")
    parser.add_argument("--key", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--context-epoch", type=int, required=True)
    parser.add_argument("--review", default="REVIEW_REQUIRED")
    parser.add_argument("--checks", required=True)
    args = parser.parse_args(argv)
    body = format_transition(
        key=args.key,
        pr_number=args.pr,
        phase=args.phase,
        strategy=args.strategy,
        head_sha=args.head_sha,
        context_epoch=args.context_epoch,
        review_verdict=args.review,
        checks_summary=args.checks,
    )
    published = [
        publish_github(args.repo, args.github_issue, body),
        publish_linear(args.linear_issue, body),
    ]
    print(f"github={published[0]} linear={published[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
