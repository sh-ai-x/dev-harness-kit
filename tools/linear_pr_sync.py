#!/usr/bin/env python3
"""tools/linear_pr_sync.py — Sync Linear issue state based on GitHub PR events.

Reads PR events (open, ready_for_review, closed, merged, etc.) and updates
the corresponding Linear issue's workflow state. Triggered by
`.github/workflows/linear-pr-sync.yml`.

The mapping is:

  PR opened (draft=false)         → "In Progress"
  PR opened (draft=true)          → no-op (drafts are not synced until ready)
  PR ready_for_review             → "In Review"
  PR reopened                     → "In Review"
  PR synchronize (new commits)   → "In Review"
  PR closed (merged=true)         → "Done"
  PR closed (merged=false)        → "Canceled"

Subcommands:
  sync --branch X --event Y [--merged B] [--pr-number N] [--pr-title T] [--pr-draft D]
      Update the Linear issue mapped to branch X based on event Y.
      --pr-draft: "true" / "false". When event="opened" and --pr-draft="true",
      the script is a no-op (drafts do not move Linear state).
  find --branch X
      Print the Linear issue identifier mapped to branch X (or empty).
  bulk-update --state STATE
      Update ALL open issues in the project to STATE (used for initial bulk
      transitions like "Backlog → In Review").

Failure modes: event-driven sync transport errors are reported without blocking the
PR workflow; the explicit smoke command fails on configuration drift.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _repo_name import repo_name as _repo_name

LINEAR_API_URL = "https://api.linear.app/graphql"


def _resolved_project_name() -> str:
    """Return the Linear project name for this repository.

    Precedence:
      1. ``LINEAR_PROJECT_NAME`` env var (explicit operator override).
      2. The main-checkout directory name via ``git rev-parse
         --git-common-dir`` — the canonical repository name per #539
         ("A repository whose Linear project name differs from its
         canonical repository name gets a project named exactly after
         the repository").
      3. The literal fallback ``"repository"`` only when both env is
         missing AND git is unavailable / not a git repo. Mirrors
         ``tools/linear_sync.py::_repo_name``'s terminal fallback so
         the two scripts agree on what to call a projectless repo.
    """
    env = os.environ.get("LINEAR_PROJECT_NAME", "").strip()
    if env:
        return env
    try:
        return _repo_name(Path.cwd())
    except Exception:
        return "repository"


# Backward-compat: keep the module-level constant so any external caller
# (or test) that reads ``linear_pr_sync.PROJECT_NAME`` still gets a value.
# Computed at import time from env-or-git, NOT from a hardcoded literal.
PROJECT_NAME = _resolved_project_name()

REQUIRED_STATE_NAMES = ("Backlog", "Todo", "In Progress", "In Review", "Done", "Canceled")

# Module-level caches — populated lazily on first call, live for the
# process lifetime. Avoids re-resolving the same project / team / state
# ids on every Edit|Write or auto-sync round.
_project_id_cache: str | None = None
_team_id_cache: str | None = None
_state_id_cache: dict[str, str] | None = None  # name (lowered) -> id

# Rate-limit state. Linear returns HTTP 429 when the hourly quota is
# exhausted. When we observe a 429, set _paused_until to a monotonic
# timestamp; subsequent calls in this process return None immediately
# until the window elapses. Each new process gets a fresh window.
# (Updated: rollup now reflects 4 caches + 429 backoff after c28ad36;
# see PR #672 for the full audit.)
_paused_until: float = 0.0
_RATE_LIMIT_BACKOFF_SECONDS = 60  # skip window after a single 429

EVENT_STATE_MAP = {
    "opened": "In Progress",
    "ready_for_review": "In Review",
    "reopened": "In Review",
    "synchronize": "In Review",
    "edited": "In Review",
    "closed": "Done",  # refined by --merged flag
}


def _api_key() -> str:
    return os.environ.get("LINEAR_API_KEY", "").strip()


def _has_api_key() -> bool:
    """Return True when LINEAR_API_KEY is present (non-empty)."""
    if not _api_key():
        print("LINEAR_API_KEY not set", file=sys.stderr)
        return False
    return True


# Backward-compat alias. Earlier callers/tests used the misleading name
# `_required`; the function actually returns True when the key IS set.
# Keep both names so existing tests and external mocks keep working.
_required = _has_api_key


def _request(query: str, variables: dict | None = None) -> dict | None:
    global _paused_until
    if not _has_api_key():
        return None
    if _paused_until > time.monotonic():
        # Linear rate limit hit earlier in this process — skip the
        # round-trip until the cooldown elapses. Each fresh process
        # starts with a clean window.
        return None
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=payload,
        headers={
            "Authorization": _api_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            # Hourly quota exhausted. Pause all subsequent calls in
            # this process for the backoff window — calling again
            # during the window wastes time and triggers 429s.
            _paused_until = time.monotonic() + _RATE_LIMIT_BACKOFF_SECONDS
            print(
                f"HTTP 429 rate limit; pausing Linear calls for "
                f"{_RATE_LIMIT_BACKOFF_SECONDS}s",
                file=sys.stderr,
            )
        else:
            print(f"HTTP {e.code} {e.reason}: {body[:200]}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"transport error: {e}", file=sys.stderr)
        return None


def _project_id() -> str | None:
    """Resolve the project ID for the canonical project name. Cached."""
    global _project_id_cache
    if _project_id_cache is not None:
        return _project_id_cache
    query = """
    query($name: String!) {
      projects(filter: { name: { eq: $name } }, first: 1) {
        nodes { id name }
      }
    }
    """
    r = _request(query, {"name": _resolved_project_name()})
    if not r:
        return None
    nodes = r.get("data", {}).get("projects", {}).get("nodes", [])
    _project_id_cache = nodes[0]["id"] if nodes else None
    return _project_id_cache


def _state_id(state_name: str) -> str | None:
    """Resolve a workflow state ID within the project's team. Cached.

    First call fetches ALL workflow states for the team in a single
    request and caches the name -> id map. Subsequent calls are dict
    lookups. Drops the smoke-test call count from `1 + N` to `1`.
    """
    global _state_id_cache
    if _state_id_cache is not None:
        return _state_id_cache.get(state_name.lower())
    team_id = _team_id()
    if not team_id:
        return None
    query = """
    query($teamId: ID!) {
      workflowStates(filter: { team: { id: { eq: $teamId } } }, first: 50) {
        nodes { id name type }
      }
    }
    """
    r = _request(query, {"teamId": team_id})
    if not r:
        return None
    mapping: dict[str, str] = {}
    for state in r.get("data", {}).get("workflowStates", {}).get("nodes", []):
        mapping[state["name"].lower()] = state["id"]
    _state_id_cache = mapping
    return mapping.get(state_name.lower())


def _team_id() -> str | None:
    """Resolve the Linear team ID for the canonical project. Cached."""
    global _team_id_cache
    if _team_id_cache is not None:
        return _team_id_cache
    query = """
    query($name: String!) {
      projects(filter: { name: { eq: $name } }, first: 1) {
        nodes { id teams { nodes { id } } }
      }
    }
    """
    r = _request(query, {"name": _resolved_project_name()})
    if not r:
        return None
    nodes = r.get("data", {}).get("projects", {}).get("nodes", [])
    if not nodes:
        return None
    teams = nodes[0].get("teams", {}).get("nodes", [])
    if not teams:
        return None
    _team_id_cache = teams[0]["id"]
    return _team_id_cache


def _issue_by_branch(branch: str, project_id: str | None) -> dict | None:
    """Find the project issue whose scope marker names branch.

    Matches by `<!-- scope:{branch}::` prefix rather than the full
    literal `{branch}::auto-sync` marker: the client-side session
    hook (tools/linear_sync.py::_scope_key) writes
    `{branch}::{first 12 words of the prompt}` as the scope suffix,
    not the literal `auto-sync` this script itself writes when it
    creates an issue. A prefix match unifies both writers onto the
    same issue; the trailing `::` still anchors on the full branch
    name so `feat/x` cannot match a `feat/x-extra` marker.
    """
    prefix = f"<!-- scope:{branch}::"
    for issue in _iter_issues(project_id, only_open=False):
        description = issue.get("description") or ""
        for line in description.splitlines():
            if line.startswith(prefix):
                return issue
    return None


def _iter_issues(project_id: str | None, only_open: bool = True) -> list[dict]:
    """Return every matching project issue, following Linear pagination."""
    if not project_id:
        return []
    state_filter = 'state: { type: { nin: ["completed", "canceled"] } },' if only_open else ""
    query = f"""
    query($projectId: ID!, $cursor: String) {{
      issues(
        filter: {{ project: {{ id: {{ eq: $projectId }} }}, {state_filter} }},
        first: 100,
        after: $cursor
      ) {{
        nodes {{ id identifier title description state {{ id name type }} url }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    issues: list[dict] = []
    cursor = None
    while True:
        response = _request(query, {"projectId": project_id, "cursor": cursor})
        if not response:
            return issues
        page = response.get("data", {}).get("issues", {})
        issues.extend(page.get("nodes", []))
        page_info = page.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            return issues
        cursor = page_info.get("endCursor")
        if not cursor:
            print("Linear pagination missing endCursor", file=sys.stderr)
            return issues


def _create_issue(branch: str, project_id: str, state_id: str, title: str = "") -> dict | None:
    """Create a Linear issue with the branch linked. Returns the issue or None."""
    team_id = _team_id()
    if not team_id:
        print("cannot create issue: team_id not resolved", file=sys.stderr)
        return None
    issue_title = title or f"PR #{branch}"
    desc = f"<!-- scope:{branch}::auto-sync -->\n\n{issue_title}"
    query = """
    mutation($projectId: String!, $teamId: String!, $title: String!, $stateId: String!, $desc: String!) {
      issueCreate(input: {
        projectId: $projectId
        teamId: $teamId
        title: $title
        stateId: $stateId
        description: $desc
      }) {
        success
        issue { id identifier title state { name } }
      }
    }
    """
    r = _request(
        query,
        {
            "projectId": project_id,
            "teamId": team_id,
            "title": issue_title,
            "stateId": state_id,
            "desc": desc,
        },
    )
    if not r:
        return None
    payload = r.get("data", {}).get("issueCreate")
    if not payload or not payload.get("success"):
        return None
    return payload.get("issue")


def _update_state(issue_id: str, state_id: str) -> bool:
    query = """
    mutation($issueId: String!, $stateId: String!) {
      issueUpdate(id: $issueId, input: { stateId: $stateId }) {
        success
        issue { id identifier state { name } }
      }
    }
    """
    r = _request(query, {"issueId": issue_id, "stateId": state_id})
    if not r:
        return False
    return bool(r.get("data", {}).get("issueUpdate", {}).get("success"))


def _all_open_issues(project_id: str | None) -> list[dict]:
    return _iter_issues(project_id, only_open=True)


def cmd_sync(args: argparse.Namespace) -> int:
    if not _has_api_key():
        return 0
    # Draft PRs are not synced until they leave draft state — the
    # `ready_for_review` event handles the actual transition.
    if args.event == "opened" and args.pr_draft == "true":
        print(f"draft PR opened — no-op (branch={args.branch})")
        return 0
    project_id = _project_id()
    target_state = EVENT_STATE_MAP.get(args.event)
    if args.event == "closed":
        target_state = "Done" if args.merged == "true" else "Canceled"
    if not target_state:
        print(f"unknown event: {args.event}", file=sys.stderr)
        return 0

    issue = _issue_by_branch(args.branch, project_id)
    state_id = _state_id(target_state)
    if not state_id:
        print(f"state not found: {target_state}", file=sys.stderr)
        return 0

    if not issue:
        # Create the issue if missing
        if args.pr_number and args.pr_title:
            title = f"PR #{args.pr_number}: {args.pr_title}"
        elif args.pr_number:
            title = f"PR #{args.pr_number}"
        else:
            title = args.pr_title or f"PR on branch {args.branch}"
        issue = _create_issue(args.branch, project_id, state_id, title)
        if not issue:
            print(f"could not create issue for branch={args.branch}", file=sys.stderr)
            return 1
        print(f"created {issue['identifier']} → {target_state} (branch={args.branch})")
        return 0

    if issue["state"]["name"].lower() == target_state.lower():
        print(f"already {target_state}: {issue['identifier']}")
        return 0

    if _update_state(issue["id"], state_id):
        print(f"{issue['identifier']} → {target_state} (branch={args.branch})")
    else:
        print(f"update failed for {issue['identifier']}", file=sys.stderr)
        return 1
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    if not _has_api_key():
        return 0
    project_id = _project_id()
    issue = _issue_by_branch(args.branch, project_id)
    if issue:
        print(issue["identifier"])
    return 0


def cmd_bulk_update(args: argparse.Namespace) -> int:
    if not _has_api_key():
        return 0
    project_id = _project_id()
    state_id = _state_id(args.state)
    if not state_id:
        print(f"state not found: {args.state}", file=sys.stderr)
        return 1
    issues = _all_open_issues(project_id)
    if not issues:
        print("no open issues to update", file=sys.stderr)
        return 0
    moved = 0
    for i in issues:
        if i["state"]["name"].lower() == args.state.lower():
            continue
        if _update_state(i["id"], state_id):
            moved += 1
            # extract branch from description scope marker
            desc = i.get("description") or ""
            branch = "?"
            if "<!-- scope:" in desc:
                branch = desc.split("<!-- scope:", 1)[1].split("::", 1)[0]
            print(f"{i['identifier']} → {args.state} (branch={branch})")
    print(f"\nMoved {moved}/{len(issues)} issues to {args.state}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """Run the bandwidth check: API key, project, state IDs.

    Cache-aware: re-uses the in-process caches populated by previous
    sync rounds or the auto-sync hook, so a smoke run after a long
    editing session makes 0 API calls (project + team + states are
    all already known). On a cold process the smoke is still 8 calls
    but with the rate-limit pause it bails after the first 429
    instead of retrying until the quota fully drains.

    Graceful degrade (per smoke-test minimum-change requirement):
    missing workflow states are reported as warnings, not failures.
    The gate fails only when the project itself cannot be resolved
    (wrong API key, wrong project name, or Linear paused for rate
    limit). Operators with a custom workflow (e.g. only Backlog + Done)
    are no longer blocked by missing states.
    """
    if not _has_api_key():
        return 1
    project_id = _project_id()
    if not project_id:
        print("project not found", file=sys.stderr)
        return 1
    print(f"project: {_resolved_project_name()} (id={project_id})")
    missing = []
    for name in REQUIRED_STATE_NAMES:
        sid = _state_id(name)
        print(f"state {name}: {'id=' + sid if sid else 'NOT FOUND'}")
        if not sid:
            missing.append(name)
    if missing:
        print(
            f"warning: {len(missing)} state(s) not configured in your "
            f"Linear workflow: {', '.join(missing)} (smoke still OK)",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="update state for a PR event")
    p_sync.add_argument("--branch", required=True)
    p_sync.add_argument("--event", required=True)
    p_sync.add_argument("--merged", default="false")
    p_sync.add_argument("--pr-number")
    p_sync.add_argument("--pr-title")
    p_sync.add_argument("--pr-draft", default="false",
                        help='GitHub PR draft flag ("true"/"false"). When event="opened" and --pr-draft="true", the script is a no-op.')
    p_sync.set_defaults(func=cmd_sync)

    p_find = sub.add_parser("find", help="print identifier for a branch")
    p_find.add_argument("--branch", required=True)
    p_find.set_defaults(func=cmd_find)

    p_bulk = sub.add_parser("bulk-update", help="move all open issues to STATE")
    p_bulk.add_argument("--state", required=True)
    p_bulk.set_defaults(func=cmd_bulk_update)

    p_smoke = sub.add_parser("smoke", help="verify API key + project + state IDs")
    p_smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
