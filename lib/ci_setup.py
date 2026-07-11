"""ci_setup.py — Install dev-kit's reusable CI templates into a target project.

Engine for the `/dev-kit:ci-setup` skill. Copies the canonical CI templates
(from `templates/ci/`) into a target repo and sets executable bits on shell
scripts.

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

import json
import re
import subprocess
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# Plugin root (resolved via __file__ so the module is location-independent).
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_ROOT = _PLUGIN_ROOT / "templates" / "ci"

# Files installed into the target repo, relative to `target_dir`.
# Order is preserved in reports (workflows first, then scripts, then
# worktree-rule files). Adding a path here also requires adding the
# corresponding template under templates/ci/.
EXPECTED_PATHS: tuple[str, ...] = (
    # CI workflows + scripts
    ".github/workflows/ci.yml",
    ".github/workflows/auto-fix-pr.yml",
    ".github/workflows/review.yml",
    ".githooks/pre-push",
    "scripts/validate.py",
    "scripts/test.sh",
    "scripts/branch-policy.sh",
    "scripts/ci-local.sh",
    # Worktree-rule enforcement (every task = new worktree + new session
    # + new branch). See .claude/rules/git-workflow.md.
    "hooks/worktree-guard.sh",
    "hooks/task-detector.sh",
    "hooks/session-start-check.sh",
    "hooks/lib/worktree-detect.sh",
    "hooks/hooks.json",
    ".claude/rules/git-workflow.md",
    "tests/test_worktree_guard.py",
)

# Files that need the executable bit after install.
EXECUTABLE_PATHS: tuple[str, ...] = (
    ".githooks/pre-push",
    "scripts/test.sh",
    "scripts/branch-policy.sh",
    "scripts/ci-local.sh",
    "scripts/validate.py",
    "hooks/worktree-guard.sh",
    "hooks/task-detector.sh",
    "hooks/session-start-check.sh",
    "hooks/lib/worktree-detect.sh",
)

# Per-skill semver (semver 2.0.0: X.Y.Z with optional `-prerelease`/`+build`).
# Used internally by `semver_lt()`. Self-contained — no `packaging` dep.
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Post-install checklist: rendered (opt-in via install_ci_config(print_checklist=True))
# AFTER the install completes. Each tuple is (number, command-block with notes).
# Empty <OWNER>/<REPO> placeholder is filled at print time from
# `git remote get-url origin` if a remote is configured; otherwise the literal
# string is shown so the user can edit it.
POST_INSTALL_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("1", "Add DEV_KIT_GITHUB_TOKEN (PAT scoped to sh-ai-x/dev-harness-kit):\n"
          "       gh secret set DEV_KIT_GITHUB_TOKEN --repo <OWNER>/<REPO> --app actions\n"
          "       (omit if sh-ai-x/dev-harness-kit is public)"),
    ("2", "Add MINIMAX_API_KEY (or ANTHROPIC_API_KEY for opt-in provider):\n"
          "       gh secret set MINIMAX_API_KEY --repo <OWNER>/<REPO>"),
    ("3", "Enable pre-push hook:  git config core.hooksPath .githooks"),
    ("4", "Push a feature branch; open a PR that does NOT modify "
          ".github/workflows/*.\n"
          "       /dev-kit:review + /dev-kit:security should fire."),
    ("5", "The first PR that ADDS review.yml cannot have the action validated "
          "by the\n"
          "       severity gate until review.yml lands on the default branch. "
          "Merge that\n"
          "       bootstrap PR first; the gate works on every PR after."),
)


@dataclass
class InstallReport:
    """Structured result of an install invocation.

    `created`/`overwritten`/`skipped` are lists of POSIX-style strings
    (forward slashes, relative to `target_dir`) for JSON-friendly output.
    `elapsed_ms` is wall-clock duration.
    `warnings` holds non-fatal findings from `lint_installed_workflows()`
    (e.g. a stale gate-tolerance pattern in a previously-installed
    workflow that the next `--force` refresh will replace).
    """

    created: List[str] = field(default_factory=list)
    overwritten: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ProbeResult:
    """One row of the pre-flight probe table.

    `state` is one of:
      - OK   : present and configured
      - WARN : present but missing/partial (skill still proceeds)
      - INFO : opt-in / informational (never blocks)
      - SKIP : gh absent or unauthenticated; the probe is silently bypassed
      - FAIL : fatal prerequisite (reserved; not currently emitted)
    """

    label: str
    state: str
    detail: str = ""


def _now_utc_iso() -> str:
    """ISO-8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copy_template(rel_path: str, target_dir: Path, *, force: bool) -> str:
    """Copy one template file. Returns 'created' | 'overwritten' | 'skipped'.

    Raises FileNotFoundError if the template source is missing (treated as
    a programmer/install error, not a runtime idem-key collision).
    """
    src = _TEMPLATES_ROOT / rel_path
    if not src.exists():
        raise FileNotFoundError(f"template source missing: {src}")
    dst = target_dir / rel_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not force:
            return "skipped"
        shutil.copy2(src, dst)
        return "overwritten"
    shutil.copy2(src, dst)
    return "created"


def _chmod_executable(rel_paths: tuple[str, ...], target_dir: Path) -> None:
    for rel in rel_paths:
        p = target_dir / rel
        if p.exists():
            mode = p.stat().st_mode
            p.chmod(mode | 0o111)  # set +x for owner/group/other


def semver_lt(a: str, b: str) -> bool:
    """Return True iff semver string `a` is strictly less than `b`.

    Public API: single canonical semver 2.0.0 comparator used by
    templates/ci/scripts/validate.py (via importlib.util when lib/ is
    reachable in dev workflows, fallback self-contained otherwise)
    for the PR-build plugin-version floor check. Self-contained — no
    `packaging` import — because lib/ci_setup.py runs on consumer CI
    runners whose Python may not have it.

    Semver 2.0.0 precedence:
      - Numeric (major, minor, patch) compare first.
      - A version with a pre-release identifier has LOWER precedence
        than the same version without one (`0.1.0-rc.1 < 0.1.0`).
      - Pre-release identifiers are compared dot-separated; numeric
        segments compare as integers, alphanumeric as strings.
      - Build metadata (`+...`) is IGNORED in precedence.

    Returns False when either input fails `SEMVER_RE`; callers should
    treat that as a data-shape failure, not a comparison outcome.
    """
    if not (SEMVER_RE.match(a) and SEMVER_RE.match(b)):
        return False
    a_main, _, a_pre = a.partition("-")
    if "+" in a_pre:
        a_pre = a_pre.split("+", 1)[0]
    b_main, _, b_pre = b.partition("-")
    if "+" in b_pre:
        b_pre = b_pre.split("+", 1)[0]
    if "+" in a_main:
        a_main = a_main.split("+", 1)[0]
    if "+" in b_main:
        b_main = b_main.split("+", 1)[0]
    a_nums = tuple(int(x) for x in a_main.split("."))
    b_nums = tuple(int(x) for x in b_main.split("."))
    if a_nums != b_nums:
        return a_nums < b_nums
    if a_pre and not b_pre:
        return True   # 0.1.0-rc.1 < 0.1.0
    if b_pre and not a_pre:
        return False  # 0.1.0 > 0.1.0-rc.1
    if not a_pre and not b_pre:
        return False
    def _id_tuple(s: str) -> tuple:
        out = []
        for part in s.split("."):
            try:
                out.append((0, int(part)))
            except ValueError:
                out.append((1, part))
        return tuple(out)
    return _id_tuple(a_pre) < _id_tuple(b_pre)


def plugin_version(plugin_root: Path | None = None) -> str:
    """Read the canonical plugin version from `.claude-plugin/plugin.json`.

    Single source of truth for the plugin's release tag. Falls back to
    `PLUGIN_CI_SETUP_VERSION` (the legacy constant) when the field is
    missing — preserves back-compat with checkouts that still have the
    pre-PR #31 / pre-this-PR layout.

    Args:
        plugin_root: absolute path to the dev-harness-kit checkout. When
            `None` (the default), uses `_PLUGIN_ROOT` (this module's
            parent-of-parent).

    Returns:
        The `version:` field as a string, e.g. `"0.3.0"`.
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
    return PLUGIN_CI_SETUP_VERSION


def _installed_snapshot_root() -> Path:
    """Return the path the running skill should treat as the installed snapshot.

    Defaults to the Claude Code plugin cache layout
    (`~/.claude/plugins/cache/dev-kit/dev-kit/<version>/`), with a
    marketplace-clone fallback (`~/.claude/plugins/marketplaces/dev-kit/`).
    Override `DEV_KIT_INSTALLED_ROOT` to test or run offline.
    """
    override = os.environ.get("DEV_KIT_INSTALLED_ROOT")
    if override:
        return Path(override)
    cache = Path.home() / ".claude" / "plugins" / "cache" / "dev-kit" / "dev-kit"
    if cache.is_dir():
        versions = sorted([p for p in cache.iterdir() if p.is_dir()])
        if versions:
            return versions[-1]  # semver-max / last created
    marketplace = Path.home() / ".claude" / "plugins" / "marketplaces" / "dev-kit"
    if marketplace.is_dir():
        return marketplace
    return Path()


def per_skill_drift(plugin_root: Path) -> dict:
    """Compare per-skill SKILL.md file content between HEAD and the installed snapshot.

    No frontmatter parsing, no per-skill version metadata. We diff raw
    content; if the bytes differ, the skill on the user's installed
    snapshot is "behind HEAD." Drift detection at this resolution is
    cheaper to maintain (no per-skill bookkeeping), honest (the user's
    installed copy may differ from HEAD in ways a version number
    doesn't capture), and good-enough (skill content diff is the ground
    truth for "did this skill change?").

    Args:
        plugin_root: absolute path to the dev-harness-kit checkout
            (the directory that contains `skills/`).

    Returns:
        dict[str, str] mapping skill name → drift tag:
          - `"behind"` if the installed snapshot's SKILL.md bytes differ
            from HEAD's (or the snapshot is missing the file)
          - `"ahead"` if the snapshot has a SKILL.md that HEAD doesn't
            (skill was deleted upstream — unusual)
          - `"current"` if bytes match
          - `"no_install"` if the snapshot root is missing (fresh
            install pre-first-refresh — every skill is `"no_install"`)

        Empty dict if `plugin_root/skills/` is missing.
    """
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return {}
    installed_root = _installed_snapshot_root()
    has_install = bool(installed_root and installed_root.is_dir())
    result: dict = {}
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        head_bytes = skill_md.read_bytes()
        if not has_install:
            result[child.name] = "no_install"
            continue
        installed_skill = installed_root / "skills" / child.name / "SKILL.md"
        if not installed_skill.exists():
            result[child.name] = "behind"
            continue
        if installed_skill.read_bytes() == head_bytes:
            result[child.name] = "current"
        else:
            result[child.name] = "behind"
    return result


def install_ci_config(
    target_dir: Path,
    *,
    force: bool = False,
    print_checklist: bool = False,
    lint: bool = True,
) -> InstallReport:
    """Install dev-kit's CI templates into `target_dir`. Idempotent.

    A no-op (all EXPECTED_PATHS reported as `skipped`) when every
    template file is already in place. With `force=True`, all template
    files are overwritten regardless. There is no separate install
    marker; idempotency comes from the file-presence check alone.

    Args:
        target_dir: absolute path to the target project root. Must exist
            and be a directory (raises FileNotFoundError otherwise).
        force: when True, overwrite existing target files matching
            EXPECTED_PATHS. Default False (skip + report).
        print_checklist: when True and the install succeeds (no errors),
            print the post-install checklist after the copy loop.
            Default False to preserve existing test contracts.

    Returns:
        InstallReport with created/overwritten/skipped/errors lists and
        the wall-clock duration in `elapsed_ms`.

    Raises:
        FileNotFoundError: target_dir is missing or not a directory, OR a
            template source file is missing (the plugin is incomplete).
        NotADirectoryError: target_dir exists but is a regular file/symlink
            to one.
    """
    started = time.monotonic()

    if target_dir is None:
        raise FileNotFoundError("target_dir is None")
    target = Path(target_dir).resolve()
    if not target.exists():
        raise FileNotFoundError(f"target_dir does not exist: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"target_dir is not a directory: {target}")

    report = InstallReport()

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

    # Set executable bit on shell-style files + validate.py.
    _chmod_executable(EXECUTABLE_PATHS, target)

    # Lint pass on installed workflows -- catches stale gate patterns and
    # other known-bad shapes that local validate.py + ci-local.sh pass.
    if lint:
        report.warnings.extend(lint_installed_workflows(target))

    report.elapsed_ms = int((time.monotonic() - started) * 1000)

    if print_checklist and report.ok:
        _print_post_install_checklist(target)

    return report


def preflight_probe(repo: str = "") -> List[ProbeResult]:
    """Run a 5-line `gh` probe against the consumer's environment.

    All gh calls are read-only and never print secret values. When `gh`
    is absent or unauthenticated, every probe returns SKIP and the skill
    prints a one-line note -- the user can still install without gh.
    """
    import json as _json
    import subprocess
    import re as _re
    results: List[ProbeResult] = []
    gh = shutil.which("gh")
    if not gh:
        results.append(ProbeResult("gh CLI", "SKIP", "gh not on PATH"))
        for label in (
            "Repo reachable",
            "DEV_KIT_GITHUB_TOKEN set",
            "MINIMAX_API_KEY set",
            "ANTHROPIC_API_KEY set (opt-in)",
        ):
            results.append(ProbeResult(label, "SKIP", "gh not on PATH"))
        return results

    def _run(args):
        cp = subprocess.run(
            [gh, *args], capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            return ProbeResult("placeholder", "OK", "")
        detail = ""
        if cp.stderr:
            detail = cp.stderr.strip().splitlines()[-1]
        return ProbeResult("placeholder", "WARN", detail)

    auth = _run(["auth", "status"])
    auth = ProbeResult("gh auth status", auth.state, auth.detail)
    if auth.state != "OK":
        for label in (
            "Repo reachable",
            "DEV_KIT_GITHUB_TOKEN set",
            "MINIMAX_API_KEY set",
            "ANTHROPIC_API_KEY set (opt-in)",
        ):
            results.append(ProbeResult(label, "SKIP", "gh not authenticated"))
        results.insert(0, auth)
        return results
    results.append(auth)

    if not repo:
        for label in (
            "Repo reachable",
            "DEV_KIT_GITHUB_TOKEN set",
            "MINIMAX_API_KEY set",
            "ANTHROPIC_API_KEY set (opt-in)",
        ):
            results.append(ProbeResult(label, "SKIP", "no repo context"))
        return results

    repo_view = _run(["repo", "view", repo, "--json", "name"])
    repo_view = ProbeResult("Repo reachable", repo_view.state, repo_view.detail)
    if repo_view.state != "OK":
        results.append(repo_view)
        for label in (
            "DEV_KIT_GITHUB_TOKEN set",
            "MINIMAX_API_KEY set",
            "ANTHROPIC_API_KEY set (opt-in)",
        ):
            results.append(ProbeResult(label, "SKIP", "repo not reachable"))
        return results
    results.append(repo_view)

    secrets_json = ""
    try:
        cp = subprocess.run(
            [gh, "secret", "list", "--repo", repo, "--json", "name"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            secrets_json = cp.stdout
    except Exception:
        pass

    secret_names = set()
    if secrets_json:
        try:
            secret_names = {
                row.get("name", "") for row in _json.loads(secrets_json)
            }
        except Exception:
            secret_names = set()

    for secret, state_when_missing in (
        ("DEV_KIT_GITHUB_TOKEN", "WARN"),
        ("MINIMAX_API_KEY", "WARN"),
        ("ANTHROPIC_API_KEY", "INFO"),
    ):
        present = secret in secret_names
        results.append(ProbeResult(
            label=f"{secret} set",
            state="OK" if present else state_when_missing,
            detail="" if present else "absent",
        ))

    return results


def _detect_owner_repo(target_dir: Path) -> str:
    """Best-effort `<OWNER>/<REPO>` from git remote, else empty string."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(target_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return ""
        url = cp.stdout.strip()
        # SSH: git@github.com:OWNER/REPO(.git)
        # HTTPS: https://github.com/OWNER/REPO(.git)
        m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return ""


def _print_post_install_checklist(target_dir: Path) -> None:
    """Print the post-install checklist to stdout. Best-effort; never raises."""
    repo = _detect_owner_repo(target_dir) or "<OWNER>/<REPO>"
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
    print(f"Verify: bash scripts/ci-local.sh")


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
)


def lint_installed_workflows(target_dir: Path) -> List[str]:
    """Scan installed EXPECTED_PATHS for known-stale patterns.

    Returns a list of human-readable findings (one per match). The intent
    is to surface patterns that previously made the install look healthy
    locally (validate.py + ci-local.sh both pass) but produced red CI in
    GitHub Actions. The skill body renders the findings in the install
    summary and the user can act on them -- the install itself is never
    blocked by lint output.
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
    missing = [r for r in EXPECTED_PATHS if not (_TEMPLATES_ROOT / r).exists()]
    if missing:
        print(f"FAIL: missing templates: {missing}", file=sys.stderr)
        return 1
    print("OK: all EXPECTED_PATHS present in templates/")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
