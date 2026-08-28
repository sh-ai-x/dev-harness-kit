"""ci_setup.py — Install dev-kit's reusable CI templates into a target project.

Engine for the `/dev-kit:ci-setup` skill. Copies the canonical CI templates
(from `templates/ci/`) into a target repo, writes the marker file
`.dev-kit/ci-config.json`, and sets executable bits on shell scripts.

Mirrors `lib/install.sh`'s conventions (mkdir -p + copy + summary), but:
- Written in Python (cross-platform pathlib, no shell escaping on Windows).
- Idempotent: existing files are skipped unless `force=True`.
- Returns a structured `InstallReport` dataclass for the skill body.

Usage (from the skill body or directly):
    from lib.ci_setup import install_ci_config
    report = install_ci_config(Path("/path/to/target_repo"))
    print(report)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

# `yaml` is imported lazily inside `_lint_if_block_scalar_hashes()` so
# the rest of ci_setup (and consumers that only call install_ci_config /
# read_provider / plugin_version) can load without PyYAML installed.
# The `bin/install.sh` consumer ships yaml as a runtime dep, but
# partial-import consumers and CI lint step should not require it for
# the install path.

# Atomic write helper. Dual-import supports both shapes:
#   * source repo: `lib/__init__.py` makes `lib` a package, so intra-package
#     `from .atomic import` resolves.
#   * consumer repo: `lib/install.sh` copies `ci_setup.py` + `atomic.py` to
#     `<target>/lib/` without `__init__.py`, so the package form fails and
#     we fall back to a top-level `from atomic import` (works when
#     `<target>/` is on sys.path, which the consumer-side invocations
#     guarantee).
#
# ci_setup.py stays on the inline try/except (rather than the centralized
# helper in `lib/_dual_import.py`) because the consumer-install contract
# is "ship only ci_setup.py + atomic.py + read_env_key.py" — see
# `tests/test_ci_setup.py::test_import_succeeds_without_hooks_manifest`.
# Adding `lib/_dual_import.py` to the consumer bundle would change that
# contract; the helper is reserved for in-package callers (ci_doctor,
# ci_update). inspect 2026-08-27 dup-5.
try:
    from .atomic import atomic_write_json, read_json_or_default  # type: ignore
except ImportError:
    from atomic import atomic_write_json, read_json_or_default  # type: ignore

# Single-source-of-truth dotenv parser (issue #711). Dual-import mirrors
# the `atomic` shim above so the consumer-side install (no
# `lib/__init__.py` shipped) keeps working.
try:
    from .read_env_key import read_env_key as _read_env_key_helper  # type: ignore
except ImportError:
    from read_env_key import read_env_key as _read_env_key_helper  # type: ignore

# Centralized gh-CLI presence + auth probe (inspect 2026-08-27 dup-6)
# lives at `lib/_gh_cli.py`. ci_setup.py is shipped flat to consumer
# installs (alongside `atomic.py` + `read_env_key.py` only) so it cannot
# import from `lib.*`. `_read_ci_provider_via_gh()` keeps the inline
# try/except dance below; the helper exists for in-package callers
# (`ci_doctor.py`) that can import freely.
try:
    from lib._gh_cli import gh_available  # type: ignore
except ImportError:
    # Consumer-install flat layout: ship a local re-implementation so the
    # call site stays a one-liner. Mirrors the helper shape exactly.
    import shutil as _shutil  # type: ignore
    import subprocess as _subprocess  # type: ignore

    def gh_available(*, timeout: int = 10):  # type: ignore
        gh = _shutil.which("gh")
        if not gh:
            return None, "gh not on PATH"
        try:
            cp = _subprocess.run(
                [gh, "auth", "status"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (_subprocess.SubprocessError, _subprocess.TimeoutExpired, OSError) as e:
            return None, f"gh auth error: {type(e).__name__}"
        if cp.returncode != 0:
            return None, "gh not authenticated"
        return gh, ""

# Plugin root (resolved via __file__ so the module is location-independent).
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_ROOT = _PLUGIN_ROOT / "templates" / "ci"
_HOOKS_ROOT = _PLUGIN_ROOT / "hooks"  # single source of truth for hook files
_TOOLS_ROOT = _PLUGIN_ROOT / "tools"  # single source of truth for bundled CLI tools

# Consumer-specific files installed into the target repo. Hook files are
# appended from the canonical `hooks/` tree below; do not hand-maintain a
# second hook inventory here.
_CI_PATHS_BEFORE_HOOKS: tuple[str, ...] = (
    # CI workflows + scripts
    ".github/workflows/ci.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/review.yml",
    # Provider selection is env-based: locally `.env:CI_REVIEW_PROVIDER`
    # (managed via `bin/set-provider.sh <provider>`, gitignored, per-user),
    # in CI `vars.CI_REVIEW_PROVIDER` (per-repo, set via `gh variable set`).
    # There is intentionally NO tracked provider file — the same repo can
    # be used by different operators with different providers.
    ".githooks/pre-push",
    "scripts/validate.py",
    "scripts/test.sh",
    "scripts/branch-policy.sh",
    "scripts/ci-local.sh",
    # Verdict extractor (issue #244, boilerplate-web PR #17/#19): reads
    # anthropics/claude-code-action@v1's claude-execution-output.json so
    # the review/security post-steps don't grep PR comments (which would
    # resurrect stale "Verdict: Changes Requested" comments from prior
    # pushes and re-introduce deterministic gate flapping).
    "scripts/extract-verdict.py",
    # Comment-derived verdict fallback (issue #625): when the agent's
    # output file is missing/unparseable (provider=minimax returns a
    # wrapper-format envelope that the parser above can't read), this
    # helper recovers the verdict from the most recent claude-prefixed
    # PR comment, filtered by createdAt > cutoff to avoid resurrecting
    # stale verdicts from prior pushes. Lives next to review.yml so the
    # workflow can `python3 ${{ github.workspace }}/.github/workflows/
    # _verdict_from_comment.py` without an extra consumer-side install
    # step.
    ".github/workflows/_verdict_from_comment.py",
)

# Keep the hook payload between consumer files and the remaining canonical
# assets. Named groups make the ordering boundary explicit without a fragile
# numeric slice that could silently move when a template is added.
_CI_PATHS_AFTER_HOOKS: tuple[str, ...] = (
    ".claude/rules/git-workflow.md",
    "tests/test_worktree_guard.py",
    "tests/test_review_yml_isolation.py",
    "tests/test_extract_verdict.py",
    # Runtime-artifact gitignore fragment (issue #202). Installed via a
    # marked-block merge so consumer-owned lines outside the block are
    # preserved across --force refreshes.
    ".gitignore",
    # /dev-kit:skill-usage's CLI + its two helper modules. Commands shell
    # out to these by a bare relative path (`python3 tools/skill_usage.py`)
    # because ${CLAUDE_PLUGIN_ROOT} does not expand inside command markdown
    # bodies (anthropics/claude-code#9354). Without these in EXPECTED_PATHS,
    # any consumer that only ran ci-setup/bootstrap-full (never cloned
    # dev-harness-kit itself) got "No such file or directory" on
    # /dev-kit:skill-usage.
    "tools/skill_usage.py",
    "tools/skill_usage_normalize.py",
    "tools/skill_usage_render.py",
    # Read-only portability and long-running loop entrypoints. These are
    # shipped with CI setup so a consumer does not need the plugin checkout.
    "tools/portability_check.py",
    "tools/loop_engine.py",
    # /dev-kit:babysit-pr-local entrypoints (issue #619). The local
    # mirror invokes these by relative path from the consumer repo;
    # ci-setup previously installed neither bin/ nor lib/ so consumers
    # had to manually cp from the plugin cache.
    "bin/babysit-pr-local.sh",
    "bin/review-local.sh",
    "bin/set-provider.sh",
    # lib/ helpers actually imported by bin/review-local.sh. The rest of
    # lib/ is plugin-internal and intentionally not shipped.
    "lib/review_local_lib.sh",  # bash, sourced by bin/review-local.sh:77
    "lib/maintenance_gate.py",  # Python, invoked by bin/review-local.sh:96,439
    "lib/atomic.py",            # Python, dep of lib/maintenance_gate.py
    "lib/__init__.py",          # Python package marker (already exists at repo root)
    # Linear auto-registration entrypoints. Every Linear hook
    # (hooks/linear-*.sh, hooks/worktree-auto-cut.sh) guards on the
    # presence of tools/linear_sync.py; without these in EXPECTED_PATHS,
    # consumer repos after ci-setup would silently bail at that guard
    # and never sync — issues land in the wrong project (or never land).
    # linear_pr_sync.py is the GH-Actions-driven companion (workflow
    # picks it up via sparse-checkout). tools/_repo_name.py is the
    # shared helper both scripts `from _repo_name import ...` — without
    # it in EXPECTED_PATHS the consumer's first hook fire raises
    # ModuleNotFoundError. All three are invoked via `python3 <path>`
    # so they do NOT need +x (no entry in EXECUTABLE_PATHS).
    "tools/_repo_name.py",
    "tools/linear_sync.py",
    "tools/linear_pr_sync.py",
)


def _canonical_hook_paths() -> tuple[str, ...]:
    """Return the complete hook tree from the plugin's canonical source.

    `hooks/hooks.json` registers hook entrypoints while shared shell helpers
    are sourced indirectly. Installing both the manifest and every `.sh`
    file prevents a new hook/helper from being omitted from consumer repos.

    `hooks/references/**` is included alongside the `.sh` files because a
    hook can depend on non-code data it reads at runtime — e.g.
    `slop-detector.sh` reads `hooks/references/slop/{phrases,structures}.md`.
    Shipping `.sh` files without their data banks is the same "manifest ships
    without everything it needs" failure class as #273/#277/#310, one level
    down: the hook file itself is present but silently degrades (or crashes)
    because a file it reads was never installed.
    """
    manifest = _HOOKS_ROOT / "hooks.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"hook manifest missing: {manifest}")
    references_root = _HOOKS_ROOT / "references"
    reference_paths = (
        [f"hooks/{path.relative_to(_HOOKS_ROOT).as_posix()}"
         for path in sorted(references_root.rglob("*")) if path.is_file()]
        if references_root.is_dir() else []
    )
    return tuple(
        ["hooks/hooks.json"]
        + [f"hooks/{path.relative_to(_HOOKS_ROOT).as_posix()}"
           for path in sorted(_HOOKS_ROOT.rglob("*.sh"))]
        + reference_paths
    )


class _LazyTuple(Sequence):
    """Tuple-like object that materializes its contents on first access.

    Looks like a tuple (`for rel in EXPECTED_PATHS:`, `EXPECTED_PATHS[:5]`,
    `len(EXPECTED_PATHS)`, `EXPECTED_PATHS[0]`, set membership) but the
    storage is computed only when first accessed. After materialization the
    result is cached, so subsequent accesses are O(1). The point is to
    defer `_canonical_hook_paths()` past module import — the consumer-side
    `from ci_setup import install_ci_config` must not raise `FileNotFoundError`
    when `hooks/hooks.json` is absent, but the install path still needs the
    full inventory at call time.
    """
    __slots__ = ("_builder", "_cached")

    def __init__(self, builder):
        self._builder = builder
        self._cached: tuple[str, ...] | None = None

    def _materialize(self) -> tuple[str, ...]:
        if self._cached is None:
            self._cached = tuple(self._builder())
        return self._cached

    def __iter__(self):
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def __getitem__(self, key):
        return self._materialize()[key]

    def __contains__(self, item) -> bool:
        return item in self._materialize()

    def __eq__(self, other) -> bool:
        return self._materialize() == other

    def __hash__(self):
        return hash(self._materialize())

    def __repr__(self) -> str:
        return repr(self._materialize())

    def __add__(self, other):
        return self._materialize() + other

    def __radd__(self, other):
        return other + self._materialize()


# One inventory drives copying, idempotency, marker hashes, and verification.
EXPECTED_PATHS: _LazyTuple = _LazyTuple(
    lambda: _CI_PATHS_BEFORE_HOOKS + _canonical_hook_paths() + _CI_PATHS_AFTER_HOOKS
)

# Files that need the executable bit after install.
EXECUTABLE_PATHS: _LazyTuple = _LazyTuple(
    lambda: (
        ".githooks/pre-push",
        "scripts/test.sh",
        "scripts/branch-policy.sh",
        "scripts/ci-local.sh",
        "scripts/extract-verdict.py",
        "scripts/validate.py",
        "tools/skill_usage.py",
        "tools/portability_check.py",
        "tools/loop_engine.py",
        # /dev-kit:babysit-pr-local entrypoints (issue #619). These must be
        # +x so the consumer can invoke them by relative path from anywhere
        # (cwd-independent, per bin/review-local.sh's REPO_ROOT-from-git
        # derivation).
        "bin/babysit-pr-local.sh",
        "bin/review-local.sh",
        "bin/set-provider.sh",
        *[path for path in EXPECTED_PATHS
          if path.startswith("hooks/") and path.endswith(".sh")],
    )
)

MARKER_REL = ".dev-kit/ci-config.json"
# Marker schema is content-only (no per-field version gate). Content is the
# source of truth; _copy_template skips when bytes match.
MARKER_SCHEMA_VERSION = "1.0.0"
# Plugin release tag — there is NO constant here. The canonical plugin
# version is `.claude-plugin/plugin.json:version`, read at runtime via
# `plugin_version(_PLUGIN_ROOT)`. Hardcoding a release tag was the source
# of a version-drift bug (see PR #111): every plugin bump had to chase
# a Python constant and a template literal. Derive at runtime instead.

# Semver 2.0.0 format (X.Y.Z with optional `-prerelease`/`+build`). Used to
# validate `.claude-plugin/plugin.json:version` shape (see plugin_version()).
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Post-install checklist: rendered (opt-in via install_ci_config(print_checklist=True))
# AFTER the marker is written. Each tuple is (number, command-block with notes).
# Empty <OWNER>/<REPO> placeholder is filled at print time from
# `git remote get-url origin` if a remote is configured; otherwise the literal
# string is shown so the user can edit it.
POST_INSTALL_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("1", "Add DEV_KIT_GITHUB_TOKEN (PAT scoped to sh-ai-x/dev-harness-kit):\n"
          "       gh secret set DEV_KIT_GITHUB_TOKEN --repo <OWNER>/<REPO> --app actions\n"
          "       (omit if sh-ai-x/dev-harness-kit is public)"),
    ("2", "Add MINIMAX_API_KEY (or ANTHROPIC_API_KEY for opt-in provider):\n"
          "       gh secret set MINIMAX_API_KEY --repo <OWNER>/<REPO>"),
    ("3", "Install Ruff and enable Git hooks:\n"
          "       brew install ruff   # macOS\n"
          "       apt install ruff    # Debian/Ubuntu\n"
          "       git config core.hooksPath .githooks"),
    ("4", "Push a feature branch; open a PR that does NOT modify "
          ".github/workflows/*.\n"
          "       /dev-kit:review + /dev-kit:security should fire."),
    ("5", "The first PR that ADDS review.yml cannot have the action validated "
          "by the\n"
          "       severity gate until review.yml lands on the default branch. "
          "Merge that\n"
          "       bootstrap PR first; the gate works on every PR after."),
)


# Provider-aware required-secret catalog (issue #212-B1/B2). Each
# provider carries its own API-key secret name. `DEV_KIT_GITHUB_TOKEN`
# is the consumer-install precondition and is added regardless of provider.
# Keep keys lowercase + values human-readable so the skill body can render
# the checklist in plain English.
PROVIDER_SECRETS: dict[str, tuple[tuple[str, str], ...]] = {
    "minimax": (
        ("MINIMAX_API_KEY", "MiniMax provider API key"),
    ),
    "anthropic": (
        ("ANTHROPIC_API_KEY", "Anthropic API key (claude-code-action opt-in)"),
    ),
    "deepseek": (
        ("DEEPSEEK_API_KEY", "DeepSeek provider API key"),
    ),
}

# Consumer install always needs the dev-harness-kit PAT. The skill body
# resolves the provider via `read_provider()` (env + `.env`) and merges
# the matching provider secret above with this PAT.
DEV_KIT_CONSUMER_SECRET: tuple[str, str] = (
    "DEV_KIT_GITHUB_TOKEN",
    "Fine-grained PAT with `contents:read` on sh-ai-x/dev-harness-kit "
    "(required when this repo is NOT the dev-harness-kit source itself)",
)


def required_secrets_for_provider(provider: str) -> tuple[str, ...]:
    """Names of repo secrets required for the given review provider.

    Always includes `DEV_KIT_GITHUB_TOKEN` (consumer-install precondition).
    The provider's own API-key secret is appended. Unknown provider names
    fall back to `minimax` (matches the gate's default fallback).

    Returns:
        Tuple of secret names (e.g. `("DEV_KIT_GITHUB_TOKEN", "MINIMAX_API_KEY")`).
    """
    provider = (provider or "minimax").strip().lower()
    names = [DEV_KIT_CONSUMER_SECRET[0]]
    for secret_name, _ in PROVIDER_SECRETS.get(provider, PROVIDER_SECRETS["minimax"]):
        if secret_name not in names:
            names.append(secret_name)
    return tuple(names)


def gh_secret_set_command(repo: str, secret_name: str) -> str:
    """Render the exact `gh secret set` invocation for one secret.

    Issue #212-B3: makes the discover path "list secrets → paste commands"
    instead of "fail CI → read log → man-page `gh secret set`". The print
    path goes through `print_checklist=True` and the post-install recap.
    """
    return f"gh secret set {secret_name} --repo {repo}"


def read_env_key(path: Path, key: str) -> str:
    """Return the last `KEY=...` value from a dotenv-style file.

    Thin wrapper around `lib.read_env_key.read_env_key` so the bash
    (`bin/set-provider.sh`) and Python (`lib/ci_setup.read_provider`)
    sides share a single parser (issue #711) and cannot drift on
    quoting / `export` prefix / CRLF edge cases. Behavior matches the
    pre-refactor in-line implementation for every previously-pinned
    test case (see `tests/test_read_env_key.py`); the helper itself
    additionally handles `export KEY=...` and CRLF line endings that
    the previous parser silently dropped.

    See `lib/read_env_key.py` for the full rules.
    """
    return _read_env_key_helper(path, key)


# Private back-compat alias — same function object. New callers should
# use the public name `read_env_key` (issue #310 overarch: cross-module
# coupling on a private symbol). Removal of the private alias is a
# follow-up; pinning it here keeps the promotion zero-risk for existing
# callers (`lib/ci_doctor.py` already updated to the public name).
_read_env_key = read_env_key


# Provider values are free-form strings that operators put into `.env` and
# `vars.CI_REVIEW_PROVIDER`. They appear in the printed remediation message
# verbatim — operators may copy-paste it into a shell. Reject shell
# metacharacters so a typo or hostile value can't invite an `$(rm -rf ~)`
# copy-paste. Mirrors the spirit of `bin/set-provider.sh:234`'s allow-list
# (only writes a fixed set of values), without pulling the full script.
_SHELL_METACHARS_RE = re.compile(r"""[\s;&|`$()<>'"\\]""")


def _is_safe_provider_value(value: str) -> bool:
    """True iff `value` has no shell metacharacters and is non-empty.

    Allowlist: alnum + `._-` only. Empty values are treated as "unset"
    by the caller, not as a safety violation, so this returns True
    for the empty string — the caller decides whether to use it.
    """
    if not value:
        return True
    return not _SHELL_METACHARS_RE.search(value)


def _remediation_msg(local_val: str, ci_val: str) -> str:
    """Render the `gh variable set …` remediation tail for a drift WARN.

    Single source of truth so the three drift branches (local-only,
    ci-only, both-set-differ) don't drift from each other. The
    remediation always echoes the LOCAL value (the side the operator
    can change without `gh` admin rights) and references `bin/set-provider.sh`
    for the inverse direction.
    """
    if local_val and not ci_val:
        return (
            f"`gh variable set CI_REVIEW_PROVIDER --body {local_val}`"
        )
    if ci_val and not local_val:
        return (
            f"`gh variable set CI_REVIEW_PROVIDER --body {ci_val}` "
            f"(or `bin/set-provider.sh {ci_val}` to update local)"
        )
    return f"`gh variable set CI_REVIEW_PROVIDER --body {local_val}`"


def _read_ci_provider_via_gh() -> tuple[str, str]:
    """Read `gh variable get CI_REVIEW_PROVIDER`. Returns `(value, degraded_msg)`.

    `value` is the lowercased variable body, or `""` when the variable is
    unset on the repo (gh exits non-zero with a "not found" stderr — that
    is a valid "no CI value" state, not a degraded call).

    `degraded_msg` is non-empty when the call is SKIP-worthy: gh absent
    from PATH, gh not authenticated, subprocess errored, or the variable
    query failed for any reason OTHER than "not found". The caller emits
    SKIP in that case (consistent with `_check_gh_auth` / `_list_repo_secrets`
    in `lib/ci_doctor.py` — ci-doctor prefers "honest can't verify" over a
    false PASS/WARN when the tool to verify is unavailable).

    Never raises; subprocess.SubprocessError / OSError / TimeoutExpired
    are caught and surfaced as degraded messages using the exception
    *type* name only (the full repr can include fragments of argv).
    """
    gh, degraded = gh_available(timeout=10)
    if not gh:
        return "", degraded or "gh not on PATH"
    try:
        cp = subprocess.run(
            [gh, "variable", "get", "CI_REVIEW_PROVIDER"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return "", f"gh variable get error: {type(e).__name__}"
    if cp.returncode != 0:
        err = (cp.stderr or "").strip().lower()
        # "Variable not found" / "no variable" means the repo has no
        # CI_REVIEW_PROVIDER set — that's a valid (empty) value, not a
        # degraded read. Anything else is a real failure.
        if "not found" in err or "no variable" in err:
            return "", ""
        return "", (
            f"gh variable get failed: "
            f"{(cp.stderr or '').strip() or cp.returncode}"
        )
    return (cp.stdout or "").strip().lower(), ""


def check_provider_consistency(target_dir: Path) -> tuple[str, str]:
    """Compare local `.env:CI_REVIEW_PROVIDER` with `vars.CI_REVIEW_PROVIDER`.

    The local value is read from `<target_dir>/.env` via `read_env_key()`
    (same dotenv reader as `read_provider()`). The CI value is read via
    `gh variable get CI_REVIEW_PROVIDER`; when `gh` is absent or not
    authenticated, the check degrades to SKIP — same contract as the
    existing ci-doctor checks (`_check_gh_auth`, `_list_repo_secrets`,
    `_fetch_open_pr_state`): the audit prefers "honest can't verify" over
    a false PASS/WARN.

    Returns:
        `(status, message)` tuple where `status ∈ {OK, WARN, SKIP, FAIL}`:
          - OK   : both unset, or both set to the same value
          - WARN : exactly one is set, or both set but differ. Message
                   includes the diff AND the `gh variable set` remediation
                   command (the same one `bin/set-provider.sh:234` prints
                   as a "next steps" hint).
          - SKIP : gh absent, unauthenticated, `gh variable get` errored
                   for a reason other than "not found", OR either side
                   carries shell metacharacters (defensive — operator
                   must fix the value manually before ci-doctor can
                   safely echo a remediation).
          - FAIL : reserved for future use (currently not emitted by this
                   check). The `Check` dataclass in `lib/ci_doctor.py`
                   accepts FAIL too, so a future FAIL row would not
                   require a contract change.

    The check is advisory only — it never flips ci-doctor's verdict. A
    WARN row appears in `warnings: N` and on screen; SKIP rows are
    counted in `skipped: N`. See issue #212-D1 for the verdict-neutral
    contract.
    """
    target = Path(target_dir).resolve()
    local_val = read_env_key(target / ".env", "CI_REVIEW_PROVIDER").strip().lower()
    ci_val, degraded = _read_ci_provider_via_gh()
    if degraded:
        return "SKIP", degraded
    if not _is_safe_provider_value(local_val):
        return "SKIP", (
            "local .env:CI_REVIEW_PROVIDER contains shell metacharacters; "
            "fix manually before ci-doctor can render a remediation"
        )
    if not _is_safe_provider_value(ci_val):
        return "SKIP", (
            "vars.CI_REVIEW_PROVIDER contains shell metacharacters; "
            "fix manually before ci-doctor can render a remediation"
        )
    if not local_val and not ci_val:
        return "OK", "both .env:CI_REVIEW_PROVIDER and vars.CI_REVIEW_PROVIDER are unset"
    if local_val == ci_val:
        return "OK", f"both {local_val}"
    remediation = _remediation_msg(local_val, ci_val)
    if local_val and not ci_val:
        return (
            "WARN",
            f"local .env=CI_REVIEW_PROVIDER={local_val} but "
            f"vars.CI_REVIEW_PROVIDER is unset; sync with {remediation}",
        )
    if ci_val and not local_val:
        return (
            "WARN",
            f"local .env:CI_REVIEW_PROVIDER is unset but "
            f"vars.CI_REVIEW_PROVIDER={ci_val}; sync with {remediation}",
        )
    return (
        "WARN",
        f"local .env=CI_REVIEW_PROVIDER={local_val} but "
        f"vars.CI_REVIEW_PROVIDER={ci_val}; sync with {remediation}",
    )


def read_provider(target_dir: Path | None = None) -> str:
    """Resolve the CI review provider for `target_dir`.

    Lookup order (first allowlisted match wins):
      1. Process env `CI_REVIEW_PROVIDER` — how CI runners thread the
         repo variable through `env:`.
      2. `<target_dir>/.env` — developer-local (gitignored), managed by
         `bin/set-provider.sh <provider>`.
      3. `<target_dir>/.env.example` — template fallback so ci-doctor can
         audit a freshly-cloned repo before `.env` exists.

    Returns the lower-cased value when it matches `PROVIDER_SECRETS`,
    otherwise `"minimax"` as the documented fallback (matches the
    historical default that the now-removed tracked `.github/ci-review-
    provider.txt` used to encode on its own line). Never raises.
    """
    candidates: list[str] = []
    env_val = os.environ.get("CI_REVIEW_PROVIDER", "").strip().lower()
    if env_val:
        candidates.append(env_val)
    if target_dir is not None:
        for env_name in (".env", ".env.example"):
            v = read_env_key(target_dir / env_name, "CI_REVIEW_PROVIDER").lower()
            if v:
                candidates.append(v)
    for c in candidates:
        if c in PROVIDER_SECRETS:
            return c
    return "minimax"


@dataclass
class InstallReport:
    """Structured result of an install invocation.

    `created`/`overwritten`/`skipped` are lists of POSIX-style strings
    (forward slashes, relative to `target_dir`) for JSON-friendly output.
    `marker_path` is an absolute Path. `elapsed_ms` is wall-clock duration.
    `warnings` holds non-fatal findings from `lint_installed_workflows()`
    (e.g. a stale gate-tolerance pattern in a previously-installed
    workflow that the next `--force` refresh will replace).
    """

    created: List[str] = field(default_factory=list)
    overwritten: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    marker_path: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _now_utc_iso() -> str:
    """ISO-8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_template_source(rel_path: str) -> Path:
    """Resolve an EXPECTED_PATHS entry to its on-disk source path.

    Most templates live under `templates/ci/`. Worktree-rule files (hooks,
    rules, tests) live at the plugin root (`hooks/`, `rules/`,
    `tests/`) because that is where they are developed and tested by the
    dev-harness-kit repo itself — keeping a parallel copy under
    `templates/ci/` historically caused silent byte drift across consumer
    installs. See issue #89.

    Returns the absolute source path; raises FileNotFoundError if the
    resolved source does not exist.
    """
    # Hook files: read from the plugin-root hooks/ tree (single source of
    # truth, shared with the project's own .claude/settings.json).
    if rel_path.startswith("hooks/"):
        candidate = _HOOKS_ROOT / rel_path[len("hooks/"):]
        if not candidate.exists():
            raise FileNotFoundError(f"hook source missing: {candidate}")
        return candidate
    # Bundled CLI tool scripts (e.g. skill_usage.py): read from the
    # plugin-root tools/ tree, same single-source-of-truth rationale as
    # hooks/ above -- these are developed and tested in-place, not
    # duplicated under templates/ci/.
    if rel_path.startswith("tools/"):
        candidate = _TOOLS_ROOT / rel_path[len("tools/"):]
        if not candidate.exists():
            raise FileNotFoundError(f"tool source missing: {candidate}")
        return candidate
    # Canonical shared rules live at plugin-root rules/. They are installed
    # under .claude/rules/ in consumer repos because Claude Code discovers
    # project rules from that compatibility location.
    if rel_path == ".claude/rules/git-workflow.md":
        candidate = _PLUGIN_ROOT / "rules" / "git-workflow.md"
        if not candidate.exists():
            raise FileNotFoundError(f"rule source missing: {candidate}")
        return candidate
    # /dev-kit:babysit-pr-local entrypoints (issue #619): bin/ scripts and
    # selected lib/ helpers live at the plugin root, not under
    # templates/ci/. Resolve them directly off _PLUGIN_ROOT so the install
    # path doesn't fragment the canonical source.
    if rel_path.startswith("bin/") or rel_path.startswith("lib/"):
        candidate = _PLUGIN_ROOT / rel_path
        if not candidate.exists():
            raise FileNotFoundError(f"bin/lib source missing: {candidate}")
        return candidate
    # Default: read from the templates/ci/ tree.
    candidate = _TEMPLATES_ROOT / rel_path
    if not candidate.exists():
        raise FileNotFoundError(f"template source missing: {candidate}")
    return candidate


def _copy_template(rel_path: str, target_dir: Path, *, force: bool) -> str:
    """Copy one template file. Returns 'created' | 'overwritten' | 'skipped'.

    Raises FileNotFoundError if the template source is missing (treated as
    a programmer/install error, not a runtime idem-key collision).

    Special-case `.gitignore` (issue #202): rather than overwriting the
    consumer's existing `.gitignore`, the dev-kit fragment is appended
    inside a marked `# >>> dev-kit >>>` / `# <<< dev-kit <<<` block.
    Re-running `--force` is idempotent — the existing block is replaced
    in place; lines outside the block are preserved.

    Source resolution: see `_resolve_template_source` (issue #89 split).
    """
    if rel_path == ".gitignore":
        return _install_gitignore_fragment(_resolve_template_source(rel_path), target_dir, force=force)
    src = _resolve_template_source(rel_path)
    dst = target_dir / rel_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not force:
            return "skipped"
        shutil.copy2(src, dst)
        return "overwritten"
    shutil.copy2(src, dst)
    return "created"


# Markers delimiting the dev-kit-managed block inside `.gitignore`. Anything
# outside these markers is owned by the consumer; anything inside is owned
# by `dev-kit:ci-setup` (issue #202). Bumping these strings is a breaking
# change for existing consumer installs — old fragments would be orphaned.
_GITIGNORE_BLOCK_START = "# >>> dev-kit >>>"
_GITIGNORE_BLOCK_END = "# <<< dev-kit <<<"


def _install_gitignore_fragment(src: Path, target_dir: Path, *, force: bool) -> str:
    """Merge `templates/ci/.gitignore` into the target's `.gitignore`.

    Three cases:
      1. Target `.gitignore` does not exist → write the fragment wrapped in
         the dev-kit marked block (no markers ≠ idempotent — see issue #202).
      2. Target `.gitignore` exists and already contains the dev-kit block
         → replace the block in place; preserve lines outside it.
      3. Target `.gitignore` exists without the dev-kit block → append
         the block at the end.

    Idempotent: re-running `--force` produces the same final state. Never
    touches lines outside the marked block.

    Returns `created` (case 1), `overwritten` (cases 2 & 3 with a real
    change), or `skipped` (case 3 where the existing file already
    contains the same block content — only possible if `force=False`
    AND the block is already present).
    """
    dst = target_dir / ".gitignore"
    dst.parent.mkdir(parents=True, exist_ok=True)
    fragment = src.read_text(encoding="utf-8")
    block = f"{_GITIGNORE_BLOCK_START}\n{fragment.rstrip()}\n{_GITIGNORE_BLOCK_END}\n"
    if not dst.exists():
        # Case 1: brand-new install. Always wrap the fragment in the
        # marked block (no markers ≠ idempotent — see issue #202).
        dst.write_text(block, encoding="utf-8")
        return "created"
    existing = dst.read_text(encoding="utf-8")
    if _GITIGNORE_BLOCK_START in existing and _GITIGNORE_BLOCK_END in existing:
        # Case 2: replace the existing block in place.
        before, _, rest = existing.partition(_GITIGNORE_BLOCK_START)
        _, _, after = rest.partition(_GITIGNORE_BLOCK_END)
        new = before + block + after.lstrip("\n")
        if new == existing:
            return "skipped"
        dst.write_text(new, encoding="utf-8")
        return "overwritten"
    # Case 3: append block at end. Always done — appending is non-destructive
    # (consumer lines outside the block are preserved) and idempotent
    # (re-running finds the markers and goes through Case 2).
    new = existing.rstrip("\n") + "\n\n" + block
    dst.write_text(new, encoding="utf-8")
    return "overwritten"


def _chmod_executable(rel_paths: tuple[str, ...], target_dir: Path) -> None:
    for rel in rel_paths:
        p = target_dir / rel
        if p.exists():
            mode = p.stat().st_mode
            p.chmod(mode | 0o111)  # set +x for owner/group/other


def plugin_version(plugin_root: Path | None = None) -> str:
    """Read the canonical plugin version from `.claude-plugin/plugin.json`.

    Single source of truth for the plugin's release tag. There is no
    fallback constant — when `plugin.json` is missing or unreadable,
    returns `"0.0.0"` (the sentinel that means "no release tag pinned
    yet", i.e. an in-development checkout, not a published release).

    Args:
        plugin_root: absolute path to the dev-harness-kit checkout. When
            `None` (the default), uses `_PLUGIN_ROOT` (this module's
            parent-of-parent).

    Returns:
        The `version:` field as a string, e.g. `"0.3.0"`, or `"0.0.0"`
        when the manifest can't be read.
    """
    root = plugin_root or _PLUGIN_ROOT
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version")
        if isinstance(v, str) and v:
            return v
    except (OSError, json.JSONDecodeError):
        pass
    return "0.0.0"  # sentinel — not a published release
def _build_marker() -> dict:
    """Build the `.dev-kit/ci-config.json` payload.

    Records `installed_dev_kit_version` (from `.claude-plugin/plugin.json:version`
    at install time) so consumer-side `ci-update` can detect "dev-kit shipped
    a new version since this consumer installed". The full per-file template
    SHA map is computed separately by `_compute_template_shas()` and merged
    in by `install_ci_config()` — not part of this base payload because it
    requires filesystem access to dev-kit's source tree.
    """
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "installed_at": _now_utc_iso(),
        "installed_by": "dev-kit:ci-setup",
        "installed_dev_kit_version": plugin_version(_PLUGIN_ROOT),
        "runners": ["ci.yml", "auto-fix-pr.yml", "review.yml"],
        "provider_env_key": "CI_REVIEW_PROVIDER",
        "scripts": [
            "scripts/validate.py",
            "scripts/test.sh",
            "scripts/branch-policy.sh",
            "scripts/ci-local.sh",
        ],
        "githooks": [".githooks/pre-push"],
        "hooks": [path for path in EXPECTED_PATHS if path.startswith("hooks/")],
        "rules": [".claude/rules/git-workflow.md"],
        "tests": [
            "tests/test_worktree_guard.py",
            "tests/test_review_yml_isolation.py",
        ],
        "tools": [
            "tools/skill_usage.py",
            "tools/skill_usage_normalize.py",
            "tools/skill_usage_render.py",
            "tools/portability_check.py",
            "tools/loop_engine.py",
            "tools/_repo_name.py",
            "tools/linear_sync.py",
            "tools/linear_pr_sync.py",
        ],
        # /dev-kit:babysit-pr-local entrypoints (issue #619). Recorded in
        # the marker so consumers can audit which bin/ scripts and lib/
        # helpers landed via ci-setup vs locally-copied.
        "bin": [
            "bin/babysit-pr-local.sh",
            "bin/review-local.sh",
            "bin/set-provider.sh",
        ],
        "lib": [
            "lib/review_local_lib.sh",
            "lib/maintenance_gate.py",
            "lib/atomic.py",
            "lib/__init__.py",
        ],
    }


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of `path`'s bytes.

    Used by the drift-detection pass (issue #202) to record the template
    bytes that landed on the consumer's repo at install time. The next
    `--force` install compares the recorded SHA against the current file
    SHA to detect locally-modified files that would be silently
    overwritten.
    """
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _compute_template_shas(
    plugin_root: Path | None = None,
) -> dict[str, str]:
    """Compute SHA-256 of every EXPECTED_PATHS source as it sits in dev-kit.

    The returned map is recorded in the consumer marker under `template_shas`.
    On the next consumer-side `ci-update`, the recorded SHAs are compared
    against a fresh `_compute_template_shas()` reading to detect
    "dev-kit changed this template since you installed" — closing the gap
    that previously left consumers blind to plugin upgrades.

    The SHAs are computed against `_resolve_template_source(rel)`, NOT the
    consumer-side copy — so the consumer's local edits do not pollute the
    template baseline. A missing source file (e.g. a template that was
    renamed in dev-kit between releases) is silently skipped rather than
    raising; that path is a `new` file for the consumer, not an error.

    Args:
        plugin_root: optional dev-kit plugin checkout root. When `None`
            (the default), uses `_PLUGIN_ROOT` (this module's
            parent-of-parent).

    Returns:
        Dict mapping `rel` → 64-hex-char SHA-256. Subset of EXPECTED_PATHS
        (entries whose source could not be read are omitted).
    """
    root = plugin_root or _PLUGIN_ROOT
    out: dict[str, str] = {}
    for rel in EXPECTED_PATHS:
        try:
            src = _resolve_template_source(rel)
        except FileNotFoundError:
            # Source moved / renamed in dev-kit. Skip — caller treats this
            # as `new` for the consumer.
            continue
        # _resolve_template_source already returns a path inside the
        # plugin root; verify it actually lives under `root` so a caller
        # who passes a wrong `plugin_root` cannot be tricked into hashing
        # an arbitrary path.
        try:
            src.relative_to(root)
        except ValueError:
            continue
        if not src.is_file():
            continue
        try:
            out[rel] = _sha256_file(src)
        except OSError:
            continue
    return out


def _detect_drift(target_dir: Path, recorded_shas: dict[str, str]) -> List[str]:
    """Compare current file SHAs against `recorded_shas` (issue #202).

    For each path in `recorded_shas`:
      - if the file no longer exists at `target_dir`: skip (the path was
        deleted by the consumer; not our concern)
      - if the file's current SHA matches the recorded SHA: in sync, no
        drift
      - if the file's current SHA differs from the recorded SHA: drift;
        the consumer modified it locally since the last install. Emit a
        warning so the user knows `--force` will overwrite the change.

    Returns a list of human-readable drift warnings (one per drifted file).
    The install itself is never blocked by drift — warnings are advisory.
    """
    out: List[str] = []
    target = Path(target_dir).resolve()
    for rel, recorded in recorded_shas.items():
        p = target / rel
        if not p.is_file():
            continue
        try:
            current = _sha256_file(p)
        except OSError:
            continue
        if current != recorded:
            out.append(
                f"{rel}: locally modified since last install (drift detected); "
                f"`--force` will overwrite. Backup to {rel}.bak if you want to keep it."
            )
    return out


def _resolve_prior_marker(target: Path) -> Tuple[Path, dict]:
    """Read prior marker (if any). Returns (marker_path, prior_dict).

    Tolerates missing/malformed marker — returns {} in those cases so the
    no-op and force=True paths can both reuse the call without crashing
    on the first install or a corrupted prior marker.
    """
    marker_path = target / MARKER_REL
    return marker_path, read_json_or_default(marker_path, {})


def _is_already_installed(target: Path, marker_path: Path, force: bool) -> bool:
    """Presence-based no-op check: marker exists AND every EXPECTED_PATHS
    file is present. With `force=True` always returns False.
    """
    if force or not marker_path.exists():
        return False
    return all((target / rel).exists() for rel in EXPECTED_PATHS)


def _copy_all_templates(target: Path, force: bool, report: InstallReport) -> None:
    """Copy each EXPECTED_PATHS template into target + chmod shell scripts.

    Mutates `report.created` / `report.overwritten` / `report.skipped`
    in place. Continues past per-file errors so a single bad template
    doesn't fail the whole install.
    """
    for rel in EXPECTED_PATHS:
        try:
            outcome = _copy_template(rel, target, force=force)
        except Exception as e:
            report.errors.append(f"{rel}: {e}")
            continue
        if outcome == "created":
            report.created.append(rel)
        elif outcome == "overwritten":
            report.overwritten.append(rel)
        else:
            report.skipped.append(rel)
    _chmod_executable(EXECUTABLE_PATHS, target)


def _validate_target(target_dir: Path | None) -> Path:
    """Resolve and validate `target_dir`. Returns the resolved Path.

    Raises FileNotFoundError if None / missing; NotADirectoryError if a file.
    """
    if target_dir is None:
        raise FileNotFoundError("target_dir is None")
    target = Path(target_dir).resolve()
    if not target.exists():
        raise FileNotFoundError(f"target_dir does not exist: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"target_dir is not a directory: {target}")
    return target


def _run_lint_and_emit_summary(target_dir: Path) -> list[str]:
    """Run lint_installed_workflows on the target. Returns the warnings list."""
    return list(lint_installed_workflows(target_dir))


def install_ci_config(
    target_dir: Path,
    *,
    force: bool = False,
    print_checklist: bool = False,
    lint: bool = True,
) -> InstallReport:
    """Install dev-kit's CI templates into `target_dir`.

    Idempotent: no-op when marker exists and every EXPECTED_PATHS file is
    in place. `force=True` overwrites regardless. Returns InstallReport;
    raises FileNotFoundError / NotADirectoryError for bad targets.
    """
    started = time.monotonic()
    target = _validate_target(target_dir)
    report = InstallReport()

    # Read prior marker (if any) so the drift-detection pass (issue #202)
    # can compare current file SHAs against the SHAs recorded at the last
    # install. Drift = locally-modified file about to be overwritten by
    # `--force`. We always read the marker when present, even if `force`
    # is False, so the no-op idempotent re-install can still surface
    # drift findings from a *prior* `--force` cycle.
    existing_marker, prior_marker = _resolve_prior_marker(target)

    # Presence-based "already installed" detection: marker exists AND every
    # template file is present ⇒ nothing to copy. Phase 1 of the skill body
    # can still detect "already installed" via marker_path.
    if _is_already_installed(target, existing_marker, force):
        report.skipped.extend(EXPECTED_PATHS)
        report.marker_path = str(existing_marker)
        # Backfill new schema fields if missing. A v1.0.0 consumer marker
        # lacks `installed_dev_kit_version` and `template_shas`; the next
        # `ci-update` cannot classify drift without them. Backfill here so
        # the consumer becomes queryable in one zero-touch step. Preserves
        # `installed_at` so install history is honest. No files are touched.
        needs_backfill = (
            "installed_dev_kit_version" not in prior_marker
            or "template_shas" not in prior_marker
        )
        if needs_backfill:
            backfilled = dict(prior_marker)
            backfilled["installed_dev_kit_version"] = plugin_version(_PLUGIN_ROOT)
            backfilled["template_shas"] = _compute_template_shas()
            atomic_write_json(existing_marker, backfilled)
            report.warnings.append(
                f"{MARKER_REL}: backfilled installed_dev_kit_version + "
                f"template_shas ({len(backfilled['template_shas'])} entries)"
            )
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        # Drift detection still runs even on no-op re-installs: the
        # consumer may have modified files locally since the last
        # install, and we want the next `--force` invocation to
        # surface that.
        recorded_shas = prior_marker.get("installed_file_shas", {})
        if isinstance(recorded_shas, dict):
            report.warnings.extend(_detect_drift(target, recorded_shas))
        if lint:
            report.warnings.extend(_run_lint_and_emit_summary(target))
        return report

    # Drift detection BEFORE the copy loop (issue #202). Only meaningful
    # when `force=True` AND a prior marker recorded SHAs — without those
    # SHAs we have nothing to compare against. Drift is advisory: the
    # install proceeds; the warning tells the user their local change
    # is about to be overwritten.
    recorded_shas = prior_marker.get("installed_file_shas", {})
    if force and isinstance(recorded_shas, dict) and recorded_shas:
        report.warnings.extend(_detect_drift(target, recorded_shas))

    _copy_all_templates(target, force, report)

    # Write marker (overwrites on force, always succeeds idempotently).
    # Record SHA-256 of every EXPECTED_PATHS file so the next install's
    # drift-detection pass can identify locally-modified files (issue #202).
    marker = target / MARKER_REL
    marker_payload = _build_marker()
    new_shas: dict[str, str] = {}
    for rel in EXPECTED_PATHS:
        p = target / rel
        if p.is_file():
            try:
                new_shas[rel] = _sha256_file(p)
            except OSError:
                # Unreadable file (permissions, race). Skip — better to
                # under-record than to crash the install on transient
                # FS errors.
                continue
    # Carry forward any prior SHAs for files we didn't touch (e.g.
    # deleted locally). Preserves historical accuracy for files that
    # vanish between installs without polluting with stale-but-current
    # entries for newly-overwritten files.
    if isinstance(prior_marker.get("installed_file_shas"), dict):
        for rel, sha in prior_marker["installed_file_shas"].items():
            if rel not in new_shas:
                new_shas[rel] = sha
    marker_payload["installed_file_shas"] = new_shas
    # Record SHA-256 of each EXPECTED_PATHS source as it sits in dev-kit
    # at install time. Distinct from `installed_file_shas` (consumer-side
    # copy). The consumer-side `ci-update` skill compares the recorded
    # `template_shas` against a fresh `_compute_template_shas()` reading
    # to detect "dev-kit changed this template since you installed" —
    # closing the dev-kit ⇄ consumer drift gap.
    marker_payload["template_shas"] = _compute_template_shas()
    atomic_write_json(marker, marker_payload)
    report.marker_path = str(marker)

    # Issue #212-A3/E1: hard-verify marker is on disk. `atomic_write_json`
    # only writes the file — never raises. A subsequent read-back asserts
    # the marker is parseable JSON (catches a zero-byte / partial-write
    # outcome that would otherwise pass silently and break the build
    # pre-flight gate later). Logged as an error in the report so the
    # skill body surfaces it; the install still returns so callers can
    # surface a custom error.
    try:
        on_disk = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(on_disk, dict) or not on_disk:
            report.errors.append(
                f"{MARKER_REL}: marker written but parseable payload is empty"
            )
    except (OSError, json.JSONDecodeError) as e:
        report.errors.append(f"{MARKER_REL}: marker verification failed: {e}")

    # Lint pass on installed workflows -- catches stale gate patterns and
    # other known-bad shapes that local validate.py + ci-local.sh pass.
    # Always runs on a fresh install; on a no-op idempotent re-install the
    # skill body may opt out via the kwarg below.
    if lint:
        report.warnings.extend(_run_lint_and_emit_summary(target))

    report.elapsed_ms = int((time.monotonic() - started) * 1000)

    if print_checklist and report.ok:
        _print_post_install_checklist(target)

    return report


def detect_owner_repo(target_dir: Path) -> str:
    """Best-effort `<OWNER>/<REPO>` from git remote.

    Returns `<OWNER>/<REPO>` on success. On failure (no git, no remote,
    non-GitHub remote, timeout), returns the literal `<OWNER>/<REPO>`
    placeholder with a `(auto-detect failed: <ExceptionType>)` suffix
    so the post-install checklist still renders usefully AND the user
    sees WHY auto-detection failed (issue #92 bug 2). Never raises.
    """
    placeholder = "<OWNER>/<REPO>"
    try:
        cp = subprocess.run(
            ["git", "-C", str(target_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return f"{placeholder} (auto-detect failed: no remote)"
        url = cp.stdout.strip()
        # SSH: git@github.com:OWNER/REPO(.git)
        # HTTPS: https://github.com/OWNER/REPO(.git)
        m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        return f"{placeholder} (auto-detect failed: remote is not GitHub)"
    except (
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
    ) as e:
        return f"{placeholder} (auto-detect failed: {type(e).__name__})"


def _print_post_install_checklist(target_dir: Path) -> None:
    """Print the post-install checklist to stdout. Best-effort; never raises."""
    repo = detect_owner_repo(target_dir) or "<OWNER>/<REPO>"
    print()
    print("=== Post-install setup (do these IN ORDER) ===")
    for n, body in POST_INSTALL_CHECKLIST:
        body = body.replace("<OWNER>/<REPO>", repo)
        for line in body.split("\n"):
            if line.startswith("       "):
                print(f"     {line.lstrip()}")
            else:
                print(f"  {n}. {line}")
    print()
    print(f"Marker: {target_dir / MARKER_REL}")
    print("Verify: bash scripts/ci-local.sh")


# Patterns of known-bad install artifacts that the lint pass surfaces.
# Each entry: (path, substring, explanation). The lint is best-effort and
# never raises; matches become `InstallReport.warnings` entries so the
# skill body can print them in the summary table and the user can act on
# them (typically by re-running with `--force` to refresh the template).
_KNOWN_STALE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        ".github/workflows/review.yml",
        # Pre-0.1.3 gate hard-failed in pull_request mode on missing verdicts
        # while defaulting to Approve in workflow_dispatch mode. Internal
        # inconsistency that produced spurious CI failures on PRs whose
        # /dev-kit:* agents did not post a verdict comment.
        "Re-run via workflow_dispatch if needed",
        "stale pull_request hard-fail gate in review.yml -- the gate used to exit 1 with "
        "'Missing verdict' whenever the /dev-kit:* agents skipped posting a verdict comment, "
        "even though the gate's own documented intent (lines 354-358) tolerates missing "
        "verdicts and the workflow_dispatch branch already defaulted to Approve. Re-run with "
        "`--force` to refresh the template; the patched gate defaults missing verdicts to "
        "Approve with a ::warning:: in both event modes.",
    ),
    (
        ".github/workflows/review.yml",
        # Issue #726: pre-fix gate hard-failed whenever
        # verdict_source=needs-fallback-bootstrap-pr, contradicting its own
        # documented fallback contract (the extract-verdict step had already
        # posted a synthetic 'Verdict: Approve' tagged with that source).
        # The post-fix gate tolerates the bootstrap case on BOTH sides
        # (AND on R_SOURCE and S_SOURCE) and falls through to the rank/case
        # logic; install-broken signatures (default-approve-no-file,
        # parse-failed-no-verdict, missing source, mixed bootstrap+ran)
        # still hard-fail (issue #212-C1). The remediation text below
        # ('Merge this PR's workflow changes to main first.') only appears
        # in the OLD broken bootstrap-path; the post-fix non-bootstrap
        # branch uses a different remediation block.
        "Merge this PR's workflow changes to main first.",
        "stale bootstrap-PR hard-fail gate in review.yml (issue #726) -- the gate "
        "used to exit 1 with 'Merge this PR's workflow changes to main first' "
        "whenever the anthropics/claude-code-action@v1 anti-recursion guard "
        "skipped both review and security on a PR that modifies "
        ".github/workflows/*. The fallback contract posts a synthesized "
        "'Verdict: Approve' tagged verdict_source=needs-fallback-bootstrap-pr; "
        "the pre-fix gate contradicted this by hard-failing on agent_ran=false. "
        "Re-run with `--force` to refresh the template; the patched gate tolerates "
        "the BOTH-bootstrap case via an AND on R_SOURCE+S_SOURCE and falls "
        "through to the rank/case logic. Mixed or non-bootstrap signatures still "
        "hard-fail (issue #212-C1 install-broken protection preserved).",
    ),
)


# Workflow files checked for `#`-inside-block-scalar anti-pattern
# (issue #219 Bug 1). YAML literal block scalars (`if: |`) and folded
# block scalars (`if: >`) treat every indented line, including `#`-prefixed
# comments, as part of the expression string passed to GitHub's expression
# parser. `#` is not valid in an expression, so the parser refuses to
# compile the workflow -> startup failure on every push. The lint pass
# scans each file's parsed YAML for `if:` blocks whose values contain
# `#`-prefixed lines and reports the first such occurrence.
_IF_BLOCK_SCALAR_WORKFLOWS: tuple[str, ...] = (
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/review.yml",
    ".github/workflows/ci.yml",
)


def _lint_if_block_scalar_hashes(content: str, rel: str) -> List[str]:
    """Return one warning string per `#`-prefixed line inside any
    `if: |` / `if: >` block scalar in `content`, else [].

    The check is YAML-aware: only literal/folded block scalars under `if:`
    (or `if` at any depth — e.g. `jobs.<name>.if`) count. `#` lines inside
    `run:` shell-script blocks or `prompt:` markdown blocks are fine — those
    go through bash / markdown parsers, not the GitHub expression parser.
    """
    # Lazy import: PyYAML is only needed for the lint path; the install
    # path (install_ci_config / read_provider / plugin_version) must not
    # require it.
    import yaml  # type: ignore
    out: List[str] = []
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        # A YAML syntax error in a workflow file is already surfaced by
        # GitHub's UI; the lint pass is for the more subtle `#`-in-block
        # pattern. Skip files that don't even parse as YAML.
        return out

    def _scan(node: object, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = f"{path}.{k}" if path else str(k)
                if k == "if" and isinstance(v, str) and "\n" in v:
                    bad = [
                        ln for ln in v.splitlines()
                        if ln.lstrip().startswith("#")
                    ]
                    if bad:
                        out.append(
                            f"{rel}: {child_path}: `#`-prefixed line inside "
                            f"`if:` block scalar breaks GitHub Actions "
                            f"expression parser (issue #219 Bug 1). "
                            f"Move the comment ABOVE `if:`. "
                            f"First offender: {bad[0]!r}"
                        )
                        return
                _scan(v, child_path)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _scan(v, f"{path}[{i}]")

    _scan(doc, "")
    return out


def lint_installed_workflows(target_dir: Path) -> List[str]:
    """Scan installed EXPECTED_PATHS for known-stale patterns.

    Returns one human-readable finding per match. Lint output is
    advisory; the install itself never blocks on it.
    """
    out: List[str] = []
    target = Path(target_dir).resolve()
    for rel, needle, explain in _KNOWN_STALE_PATTERNS:
        p = target / rel
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle in content:
            out.append(f"{rel}: {explain}")
    for rel in _IF_BLOCK_SCALAR_WORKFLOWS:
        p = target / rel
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(_lint_if_block_scalar_hashes(content, rel))
    return out


def _self_test() -> int:
    """Quick CLI sanity check — invoke as `python lib/ci_setup.py`. Exits 0 on OK."""
    target = Path.cwd()
    print(f"ci_setup.py self-test — target={target}")
    print(f"  plugin_root={_PLUGIN_ROOT}")
    print(f"  templates_root={_TEMPLATES_ROOT}")
    if not _TEMPLATES_ROOT.is_dir():
        print(f"FAIL: templates dir missing: {_TEMPLATES_ROOT}", file=sys.stderr)
        return 1
    expected = list(_TEMPLATES_ROOT.rglob("*"))
    files = [str(p.relative_to(_TEMPLATES_ROOT)) for p in expected if p.is_file()]
    print(f"  found {len(files)} template files")
    missing = []
    for rel in EXPECTED_PATHS:
        try:
            _resolve_template_source(rel)
        except FileNotFoundError:
            missing.append(rel)
    if missing:
        print(f"FAIL: missing install sources: {missing}", file=sys.stderr)
        return 1
    print("OK: all EXPECTED_PATHS have canonical install sources")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
