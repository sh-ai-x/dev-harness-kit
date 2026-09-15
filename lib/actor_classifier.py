#!/usr/bin/env python3
"""actor_classifier.py — deterministic actor classification for PR routing.

Reads only local state (``git remote get-url origin``,
``.claude-plugin/plugin.json:owner``, ``.dev-kit/team.json:maintainers``)
and returns one of five actor types and a recommended gate path. The
output is the SSOT that ``hooks/pr-create-route.sh`` writes to
``.dev-kit/.pr-route.json`` for the CI ``if:`` predicates to consume in
a follow-up composite-action dedup (deferred — see plan §6).

Five scenarios must classify cleanly (per design plan):
  1. dev-harness-kit itself, maintainer → ``maintainer_self``
  2. dev-harness-kit fork, maintainer → ``maintainer_fork``
  3. dev-harness-kit fork, non-maintainer → ``consumer_fork``
  4. Consumer repo (ci-setup installed), maintainer → ``maintainer_self``
  5. Consumer repo, fork PR from outside → ``consumer_fork``

NEVER calls ``gh``. Pure-Python + git CLI; safe to invoke inline from a
PreToolUse hook. The server-side ``pull_request.head.repo.full_name``
+ ``author_association`` check in ``review.yml`` is still the
authoritative gate — this client-side classifier only writes a
breadcrumb for the user-visible route summary.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import FrozenSet, Literal, Optional, Tuple

ActorType = Literal[
    "maintainer_self",
    "maintainer_fork",
    "consumer_self",
    "consumer_fork",
    "unknown",
]
RepoKind = Literal["dev_harness_kit", "consumer", "unknown"]
RecommendedGate = Literal[
    "standard_gates",
    "fork_pr_review_environment",
    "manual_review",
]

# Fallback canonical origins used when ``.claude-plugin/plugin.json:owner``
# is not yet declared. Update when the plugin's canonical repo moves or
# when this list becomes stale (the gate fails closed via ``unknown`` if
# the live origin isn't recognised).
KNOWN_DEV_HARNESS_KIT_ORIGINS: FrozenSet[str] = frozenset(
    {"sh-ai-x/dev-harness-kit"}
)

# Repo names that identify a checkout as a fork of the dev-harness-kit
# plugin — used when ``.claude-plugin/plugin.json:owner`` is absent
# (i.e. ``ci-setup`` was installed on a fork of dev-harness-kit itself).
# Adding a new canonical dev-harness-kit repo is a coordinate change
# here + ``KNOWN_DEV_HARNESS_KIT_ORIGINS`` + ``plugin.json:owner``.
KNOWN_DEV_HARNESS_KIT_REPO_NAMES: FrozenSet[str] = frozenset(
    {"dev-harness-kit"}
)

# The full set of author_association values GitHub returns for a
# contributor trusted enough to use ``pull_request_target`` + secrets in
# review.yml. Mirrors the YAML ``if:`` predicate at
# ``.github/workflows/review.yml:133-136`` (and its mirror in
# ``maintenance.yml`` + ``fork-pr-review.yml``).
TRUSTED_ASSOCIATIONS: FrozenSet[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

# Choices surfaced in --gh-author-association. The TRUSTED_ASSOCIATIONS
# triple + every other value the GitHub REST API returns for
# ``/repos/:o/:r/issues/:n`` author_association.
_ALL_ASSOCIATIONS: Tuple[str, ...] = (
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIMER",
    "FIRST_TIME_CONTRIBUTOR",
    "MANNEQUIN",
    "MEMBER",
    "NONE",
    "OWNER",
)


@dataclass(frozen=True)
class ActorClassification:
    actor_type: ActorType
    repo_kind: RepoKind
    recommended_gate: RecommendedGate
    reason: str
    head_branch: str
    base_branch: str
    remote_owner_repo: Optional[Tuple[str, str]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("remote_owner_repo") is not None:
            d["remote_owner_repo"] = list(self.remote_owner_repo)
        return d


def _run(cmd: list, cwd: Path, timeout: int = 5) -> tuple:
    """Best-effort subprocess wrapper. Returns (rc, stdout, stderr).
    Never raises — a missing git binary or a 5-second timeout degrades
    to ``(1, "", "<ExceptionType>: <msg>")`` so the caller can fail
    closed without try/except boilerplate at every call site.
    """
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip())
    except (
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
    ) as e:
        return (1, "", f"{type(e).__name__}: {e}")


def _detect_owner_repo(root: Path) -> Optional[Tuple[str, str]]:
    """Parse ``git remote get-url origin`` into ``(OWNER, REPO)``.

    Same regex as ``lib/gates_state.detect_owner_repo`` and
    ``lib/ci_setup.detect_owner_repo`` — the byte-equivalence assertion
    in ``tests/test_gates_state.py::TestDetectOwnerRepoBodyEquivalence``
    pins those three to a single shape.
    """
    rc, out, _ = _run(["git", "remote", "get-url", "origin"], root)
    if rc != 0 or not out:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", out)
    if not m:
        return None
    return m.group(1), m.group(2)


def _read_plugin_owner(root: Path) -> Optional[str]:
    """Read ``.claude-plugin/plugin.json:owner`` — the canonical owner
    signal added in this PR. ``None`` when the manifest is absent
    (consumer repo that installed dev-kit via ci-setup) or malformed.
    """
    path = root / ".claude-plugin" / "plugin.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    owner = data.get("owner")
    return str(owner).lower() if isinstance(owner, str) and owner else None


def _read_team_maintainers(root: Path) -> FrozenSet[str]:
    """Read ``.dev-kit/team.json:maintainers`` — GitHub logins (lowercased).

    Missing file → empty frozenset (no error). Corrupt JSON → empty
    frozenset (defensive; mirrors the ``read_state`` fail-closed pattern
    in ``lib/guard_mode_state.py``). Non-list ``maintainers`` → empty.
    """
    path = root / ".dev-kit" / "team.json"
    if not path.exists():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    maintainers = data.get("maintainers")
    if not isinstance(maintainers, list):
        return frozenset()
    return frozenset(str(m).lower() for m in maintainers if isinstance(m, str))


def _resolve_head_branch(root: Path) -> str:
    rc, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    return out if rc == 0 else ""


def classify_actor(
    root: Path,
    *,
    head_branch: Optional[str] = None,
    base_branch: Optional[str] = None,
    team_maintainers: Optional[FrozenSet[str]] = None,
    gh_author_association: Optional[str] = None,
) -> ActorClassification:
    """Deterministic classification of the local repo + HEAD branch.

    All inputs are local; ``gh_author_association`` is the only network-
    adjacent field and is injected by the caller (the classifier itself
    never shells out to ``gh``).

    Returns an ``ActorClassification`` whose ``recommended_gate`` is the
    single label the hook writes to ``.dev-kit/.pr-route.json`` and the
    ``show`` sub-command prints for the human. ``unknown`` always maps
    to ``manual_review`` (fail-closed).
    """
    hb = head_branch if head_branch else _resolve_head_branch(root)
    bb = base_branch if base_branch else "main"

    owner_repo = _detect_owner_repo(root)

    if owner_repo is None:
        return ActorClassification(
            actor_type="unknown",
            repo_kind="unknown",
            recommended_gate="manual_review",
            reason=(
                "no GitHub origin detected — cannot classify without remote URL; "
                "fail-closed to manual_review"
            ),
            head_branch=hb,
            base_branch=bb,
            remote_owner_repo=None,
        )

    owner = owner_repo[0]
    plugin_owner = _read_plugin_owner(root)
    owner_lower = owner.lower()

    if plugin_owner is not None:
        # Running inside the dev-harness-kit plugin source checkout.
        repo_kind = "dev_harness_kit"
        same_repo = owner_lower == plugin_owner
    elif (
        f"{owner_lower}/{owner_repo[1].lower()}" in KNOWN_DEV_HARNESS_KIT_ORIGINS
        or owner_repo[1].lower() in KNOWN_DEV_HARNESS_KIT_REPO_NAMES
    ):
        # Origin matches the canonical dev-harness-kit repo OR the
        # repo name is a known dev-harness-kit name but the plugin
        # manifest is absent → consumer who installed ci-setup on a
        # fork of dev-harness-kit itself.
        repo_kind = "consumer"
        same_repo = False
    else:
        # Plain consumer repo (ci-setup installed on a normal org/app).
        repo_kind = "consumer"
        same_repo = True

    team = (
        team_maintainers
        if team_maintainers is not None
        else _read_team_maintainers(root)
    )
    trusted = (
        gh_author_association in TRUSTED_ASSOCIATIONS
        if gh_author_association
        else False
    )

    if same_repo:
        if trusted or team:
            return ActorClassification(
                actor_type="maintainer_self",
                repo_kind=repo_kind,
                recommended_gate="standard_gates",
                reason=(
                    "same-repo PR with maintainer signal (gh author_association "
                    "in TRUSTED_ASSOCIATIONS or .dev-kit/team.json populated)"
                ),
                head_branch=hb,
                base_branch=bb,
                remote_owner_repo=owner_repo,
            )
        return ActorClassification(
            actor_type="consumer_self",
            repo_kind=repo_kind,
            recommended_gate="standard_gates",
            reason=(
                "same-repo PR — consumer path; server-side review.yml "
                "trusted-author filter still applies"
            ),
            head_branch=hb,
            base_branch=bb,
            remote_owner_repo=owner_repo,
        )

    # different owner → fork
    if trusted or team:
        return ActorClassification(
            actor_type="maintainer_fork",
            repo_kind=repo_kind,
            recommended_gate="standard_gates",
            reason=(
                "fork PR with maintainer signal — server-side "
                "pull_request_target + author_association check still gates secrets"
            ),
            head_branch=hb,
            base_branch=bb,
            remote_owner_repo=owner_repo,
        )
    return ActorClassification(
        actor_type="consumer_fork",
        repo_kind=repo_kind,
        recommended_gate="fork_pr_review_environment",
        reason=(
            "fork PR without maintainer signal — route via "
            "fork-pr-review.yml Environment gate (manual approval required)"
        ),
        head_branch=hb,
        base_branch=bb,
        remote_owner_repo=owner_repo,
    )


def write_breadcrumb(
    classification: ActorClassification, root: Path
) -> Optional[Path]:
    """Persist the classification to ``.dev-kit/.pr-route.json``.

    Best-effort: a read-only filesystem or a missing ``.dev-kit/``
    directory returns ``None`` instead of raising so the hook can call
    this without a try/except. The classifier itself never depends on
    the breadcrumb being writable — the route is still printed to
    stderr by the hook.
    """
    path = root / ".dev-kit" / ".pr-route.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(classification.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return None
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "actor classifier — deterministic PR routing SSOT. "
            "Reads git remote + .claude-plugin/plugin.json:owner + "
            ".dev-kit/team.json; never calls gh."
        )
    )
    parser.add_argument("--root", default=None, help="project root (default: cwd)")
    parser.add_argument(
        "--head-branch", default=None, help="override HEAD branch (default: git rev-parse)"
    )
    parser.add_argument(
        "--base-branch",
        default=None,
        help="override base branch (default: 'main')",
    )
    parser.add_argument(
        "--gh-author-association",
        default=None,
        choices=_ALL_ASSOCIATIONS,
        help="inject caller-verified GitHub author_association",
    )
    parser.add_argument(
        "--write-breadcrumb",
        action="store_true",
        help="also write .dev-kit/.pr-route.json",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON (default: human)"
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else Path.cwd()
    cls = classify_actor(
        root,
        head_branch=args.head_branch,
        base_branch=args.base_branch,
        gh_author_association=args.gh_author_association,
    )
    if args.write_breadcrumb:
        write_breadcrumb(cls, root)
    if args.json:
        print(json.dumps(cls.to_dict(), sort_keys=True))
    else:
        d = cls.to_dict()
        print(f"actor_type:        {d['actor_type']}")
        print(f"repo_kind:         {d['repo_kind']}")
        print(f"recommended_gate:  {d['recommended_gate']}")
        print(f"head_branch:       {d['head_branch']}")
        print(f"base_branch:       {d['base_branch']}")
        if d.get("remote_owner_repo"):
            o, r = d["remote_owner_repo"]
            print(f"remote:            {o}/{r}")
        print(f"reason:            {d['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
