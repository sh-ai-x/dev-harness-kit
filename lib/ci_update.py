"""ci_update.py — Detect + apply drift between installed CI templates and current dev-kit source.

Engine for the `/dev-kit:ci-update` skill. Reads the consumer marker
(`.dev-kit/ci-config.json`), classifies every EXPECTED_PATHS entry into
one of four drift states (new / updated / consumer_modified / diverged),
and offers a safe apply path that backs up before overwriting.

Closes the dev-kit ⇄ consumer gap: consumers who ran `/dev-kit:ci-setup`
at an older dev-kit version can detect "dev-kit shipped a new version"
by comparing the marker's `installed_dev_kit_version` and `template_shas`
against fresh readings. The same engine powers `ci-doctor`'s
"templates current" check.

Two public entry points:
    - `diff_ci_install(target_dir, *, plugin_root=None) -> UpdateReport`
      read-only diff; never writes.
    - `apply_ci_update(target_dir, *, mode='dry-run', backup=True,
                       plugin_root=None) -> UpdateReport`
      writes per `mode`. mode='apply' touches only NEW + UPDATED (refuses
      to overwrite CONSUMER_MODIFIED + DIVERGED without explicit consent).
      mode='force' overwrites all four states.

Mirrors the dual-import dance in `lib/ci_doctor.py:39-56` so the module
loads correctly from both the source repo (lib/ package) and from a
consumer repo where `lib/ci_update.py` is a flat file on sys.path.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# Dual-import shim (inspect 2026-08-27 dup-5): centralized in
# `lib/_dual_import.py` so the same try/except dance is not hand-copied
# in 4 places.
from lib._dual_import import from_dual

(
    EXPECTED_PATHS,
    MARKER_REL,
    _compute_template_shas,
    _resolve_prior_marker,
    _sha256_file,
    plugin_version,
) = from_dual(
    "ci_setup",
    [
        "EXPECTED_PATHS",
        "MARKER_REL",
        "_compute_template_shas",
        "_resolve_prior_marker",
        "_sha256_file",
        "plugin_version",
    ],
)

(atomic_write_json,) = from_dual("atomic", ["atomic_write_json"])


# Sentinel installed_dev_kit_version when the marker predates the version
# field. `ci-update` cannot classify drift against dev-kit without the
# recorded SHAs; consumers in this state are told to run `ci-setup`
# first to backfill.
_UNKNOWN_VERSION = "unknown"


# Files whose install path merges consumer-owned content with the
# dev-kit fragment (see `_install_gitignore_fragment`). The consumer's
# on-disk bytes will ALWAYS differ from the dev-kit template bytes by
# design — that is the point of the marked-block merge. Treating the
# difference as "updated" or "diverged" would surface a false drift
# signal on every install. The diff classifier skips these files; the
# underlying merge logic still preserves user-owned lines outside the
# marked block.
_MERGE_FILES: frozenset[str] = frozenset({
    ".gitignore",
})


@dataclass
class UpdateReport:
    """Structured result of `diff_ci_install` or `apply_ci_update`.

    Each list holds POSIX-style relative paths (forward slashes,
    relative to `target_dir`) for JSON-friendly output. `template_shas`
    is a fresh snapshot of every EXPECTED_PATHS source SHA at diff time
    — distinct from the marker-side `installed_file_shas` (consumer copy).
    `installed_dev_kit_version` is what the consumer's marker records;
    `target_version` (only set by apply) is the runtime version the
    consumer was upgraded TO.
    """

    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    consumer_modified: list[str] = field(default_factory=list)
    diverged: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    installed_dev_kit_version: str = ""
    target_version: str = ""
    template_shas: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _resolve_plugin_root(plugin_root: Path | None) -> Path:
    """Pick the dev-kit checkout root. Defaults to a sensible fallback."""
    if plugin_root is not None:
        return plugin_root
    # When imported from the source repo, `ci_setup._PLUGIN_ROOT` is the
    # canonical source. When imported from a consumer repo, callers must
    # pass `plugin_root` explicitly (typically resolved from
    # `${CLAUDE_PLUGIN_ROOT}`). Without a hint, return the source-root
    # sentinel — callers in a consumer repo will hit the dual-import
    # path and get whatever Path(__file__) resolves to, which is the
    # consumer's own repo (wrong for SHA comparisons). The skill body
    # is expected to thread plugin_root through.
    from ci_setup import _PLUGIN_ROOT  # type: ignore
    return _PLUGIN_ROOT


def _classify(
    target_sha: str | None,
    template_sha: str | None,
    installed_sha: str | None,
) -> str:
    """Classify one file into new / updated / unchanged / consumer_modified / diverged.

    Inputs are SHA-256 hex digests. `None` means "unknown":
      - target_sha=None → file is absent at target
      - template_sha=None → file is not in dev-kit's current EXPECTED_PATHS
      - installed_sha=None → marker has no record of this file (e.g. a
        pre-SHA-tracking consumer)

    Semantics — change is measured against `installed_sha` (the snapshot
    captured at install time), which is the common baseline:
      - consumer_changed = target_sha != installed_sha
      - dev_kit_changed  = template_sha != installed_sha

    Returns one of: "new", "updated", "unchanged", "consumer_modified",
    "diverged".
    """
    if target_sha is None:
        return "new"
    if template_sha is None:
        # File is at the consumer but no longer in EXPECTED_PATHS. Treat
        # as unchanged from dev-kit's perspective — the consumer keeps
        # its local copy; no diff action is taken (out of scope for v1).
        return "unchanged"
    if installed_sha is None:
        # Marker lacks a prior SHA. The safest assumption is "consumer
        # was unchanged when this file shipped" — i.e. target_sha ==
        # template_sha implies unchanged; otherwise the consumer has
        # edited something dev-kit never tracked (consumer_modified).
        if target_sha == template_sha:
            return "unchanged"
        return "consumer_modified"
    consumer_changed = target_sha != installed_sha
    dev_kit_changed = template_sha != installed_sha
    if not consumer_changed and not dev_kit_changed:
        return "unchanged"
    if consumer_changed and not dev_kit_changed:
        return "consumer_modified"
    if not consumer_changed and dev_kit_changed:
        return "updated"
    # Both changed; if they happened to land on the same bytes, treat as
    # unchanged (the consumer manually synced).
    if target_sha == template_sha:
        return "unchanged"
    return "diverged"


def _build_report(
    target: Path,
    plugin_root: Path,
    *,
    started: float,
    classification: dict[str, str],
    warnings: list[str],
    template_shas: dict[str, str],
    installed_dev_kit_version: str,
) -> UpdateReport:
    """Bucket per-file state lists into a fresh UpdateReport."""
    r = UpdateReport(
        elapsed_ms=int((time.monotonic() - started) * 1000),
        installed_dev_kit_version=installed_dev_kit_version,
        template_shas=dict(template_shas),
        warnings=list(warnings),
    )
    for rel, state in classification.items():
        getattr(r, state).append(rel)
    return r


def _load_marker_state(target: Path) -> tuple[dict, Path, list[str]]:
    """Read marker; return (prior_marker_dict, marker_path, warnings)."""
    marker_path, prior = _resolve_prior_marker(target)
    warnings: list[str] = []
    if not marker_path.exists():
        warnings.append(f"{MARKER_REL}: marker missing; ci-update cannot classify drift")
        return {}, marker_path, warnings
    installed_version = prior.get("installed_dev_kit_version", _UNKNOWN_VERSION)
    if installed_version == _UNKNOWN_VERSION or not installed_version:
        warnings.append(
            f"{MARKER_REL}: installed_dev_kit_version is missing or 'unknown'. "
            f"Run `/dev-kit:ci-setup` first to backfill the version field, "
            f"then re-run ci-update."
        )
        installed_version = _UNKNOWN_VERSION
    return prior, marker_path, warnings


def diff_ci_install(
    target_dir: Path,
    *,
    plugin_root: Path | None = None,
) -> UpdateReport:
    """Read-only diff: classify every EXPECTED_PATHS entry by drift state.

    Never writes. Returns the same `UpdateReport` shape as `apply_ci_update`
    so callers can render a uniform table whether the user previews or
    applies.
    """
    started = time.monotonic()
    target = Path(target_dir).resolve()
    if not target.is_dir():
        r = UpdateReport()
        r.errors.append(f"target_dir is not a directory: {target}")
        r.elapsed_ms = int((time.monotonic() - started) * 1000)
        return r

    prior, marker_path, warnings = _load_marker_state(target)
    root = _resolve_plugin_root(plugin_root)

    # Snapshot dev-kit's current EXPECTED_PATHS SHAs.
    try:
        live_template_shas = _compute_template_shas(root)
    except Exception as e:
        r = UpdateReport(warnings=warnings, elapsed_ms=int((time.monotonic() - started) * 1000))
        r.errors.append(f"_compute_template_shas failed: {e}")
        return r

    # Marker-recorded template_shas (may be missing for v1.0.0 consumers).
    marker_template_shas = prior.get("template_shas", {}) if isinstance(prior, dict) else {}
    marker_installed_shas = prior.get("installed_file_shas", {}) if isinstance(prior, dict) else {}
    installed_version = prior.get("installed_dev_kit_version", _UNKNOWN_VERSION) if isinstance(prior, dict) else _UNKNOWN_VERSION

    # Per-file classification. Walk EXPECTED_PATHS (the authoritative list
    # of files dev-kit ships). For each rel, compute target SHA, use
    # recorded template_sha for "what dev-kit looked like at install",
    # and recorded installed_file_sha for "what the consumer had at install".
    classification: dict[str, str] = {}
    for rel in EXPECTED_PATHS:
        # Skip files whose install path merges consumer-owned content
        # with the dev-kit fragment (e.g. `.gitignore`). The consumer's
        # on-disk bytes will ALWAYS differ from the dev-kit template
        # bytes by design — that's the point of the marked-block merge.
        if rel in _MERGE_FILES:
            continue
        target_p = target / rel
        target_sha: str | None = None
        if target_p.is_file():
            try:
                target_sha = _sha256_file(target_p)
            except OSError as e:
                warnings.append(f"{rel}: cannot hash consumer copy: {e}")
                continue
        template_sha = marker_template_shas.get(rel) or live_template_shas.get(rel)
        # Prefer the marker-recorded template_sha for classification
        # (it reflects what was promised at install time); fall back to
        # the live snapshot for v1.0.0 markers where template_shas is empty.
        if rel not in marker_template_shas and rel in live_template_shas:
            warnings.append(
                f"{rel}: not in marker template_shas (marker predates schema). "
                f"Falling back to live dev-kit snapshot."
            )
        installed_sha = marker_installed_shas.get(rel)
        classification[rel] = _classify(target_sha, template_sha, installed_sha)

    return _build_report(
        target,
        root,
        started=started,
        classification=classification,
        warnings=warnings,
        template_shas=live_template_shas,
        installed_dev_kit_version=installed_version,
    )


def _backup(target: Path, rel: str) -> str | None:
    """Copy `target/rel` to `target/rel.bak` if it exists.

    Returns the backup path as a string, or None if no backup was
    created (file does not exist or write failed).
    """
    p = target / rel
    bak = target / (rel + ".bak")
    if not p.is_file():
        return None
    try:
        shutil.copy2(p, bak)
        return str(bak)
    except OSError:
        return None


def _write_from_source(
    target: Path,
    rel: str,
    plugin_root: Path,
) -> bool:
    """Copy dev-kit source for `rel` into target. Returns True on success."""
    from ci_setup import _resolve_template_source  # type: ignore
    try:
        src = _resolve_template_source(rel)
    except FileNotFoundError:
        return False
    try:
        src.relative_to(plugin_root)
    except ValueError:
        return False
    if not src.is_file():
        return False
    dst = target / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def apply_ci_update(
    target_dir: Path,
    *,
    mode: str = "dry-run",
    backup: bool = True,
    plugin_root: Path | None = None,
) -> UpdateReport:
    """Apply the diff to `target_dir` per `mode`.

    Modes:
      - 'dry-run' (default): never writes. Returns what would happen.
      - 'apply': writes NEW + UPDATED. Refuses to touch CONSUMER_MODIFIED
        or DIVERGED without explicit per-file consent (those are still
        classified in the report for the caller to render a prompt).
      - 'force': writes all four states with backup-before-overwrite.

    `backup=True` creates `<rel>.bak` whenever an existing consumer file
    is about to be overwritten (only when the on-disk SHA differs from
    the source SHA — idempotent re-runs don't keep stacking `.bak` files
    of unchanged content).

    After apply, the consumer marker is rewritten with the fresh
    `installed_dev_kit_version` + `template_shas` so the next `ci-update`
    sees a stable baseline.
    """
    started = time.monotonic()
    target = Path(target_dir).resolve()
    if mode not in ("dry-run", "apply", "force"):
        r = UpdateReport()
        r.errors.append(f"unknown mode: {mode!r}; expected dry-run|apply|force")
        r.elapsed_ms = int((time.monotonic() - started) * 1000)
        return r
    if not target.is_dir():
        r = UpdateReport()
        r.errors.append(f"target_dir is not a directory: {target}")
        r.elapsed_ms = int((time.monotonic() - started) * 1000)
        return r

    # Always start from the diff view so apply reuses the same classification.
    diff = diff_ci_install(target, plugin_root=plugin_root)

    r = UpdateReport(
        new=list(diff.new),
        updated=list(diff.updated),
        unchanged=list(diff.unchanged),
        consumer_modified=list(diff.consumer_modified),
        diverged=list(diff.diverged),
        warnings=list(diff.warnings),
        elapsed_ms=diff.elapsed_ms,
        installed_dev_kit_version=diff.installed_dev_kit_version,
        template_shas=dict(diff.template_shas),
    )

    root = _resolve_plugin_root(plugin_root)

    # Decide which states to write.
    if mode == "dry-run":
        # No writes. Surface a hint that no action was taken.
        r.warnings.append("dry-run: no files written; pass --apply or --force to write.")
        r.elapsed_ms = int((time.monotonic() - started) * 1000)
        return r

    writable_states: set[str]
    if mode == "apply":
        writable_states = {"new", "updated"}
        # Surface consumer_modified + diverged as not-written (caller
        # renders a prompt asking for explicit consent).
        if r.consumer_modified:
            r.warnings.append(
                f"apply mode skipped {len(r.consumer_modified)} consumer_modified "
                f"file(s); pass --force to overwrite."
            )
        if r.diverged:
            r.warnings.append(
                f"apply mode skipped {len(r.diverged)} diverged "
                f"file(s); pass --force to overwrite."
            )
    else:  # force
        writable_states = {"new", "updated", "consumer_modified", "diverged"}

    # Walk writable states; backup first when requested.
    for state in ("new", "updated", "consumer_modified", "diverged"):
        if state not in writable_states:
            continue
        for rel in getattr(r, state):
            # Skip files whose install path is a merge (e.g. `.gitignore`).
            # `apply_ci_update` does not re-run the merge; consumers who
            # want the marker block refreshed should run ci-setup --force,
            # which preserves user-owned lines outside the marked block.
            if rel in _MERGE_FILES:
                continue
            target_p = target / rel
            # Documented contract (SKILL.md:148-149): only back up when the
            # on-disk bytes differ from what dev-kit will write. An
            # idempotent re-run whose target file already matches the
            # source skips the .bak so .bak files of unchanged content
            # never accumulate on disk.
            if target_p.is_file() and backup:
                target_sha_now = _sha256_file(target_p)
                template_sha = r.template_shas.get(rel)
                if target_sha_now != template_sha:
                    bak_path = _backup(target, rel)
                    if bak_path:
                        r.backed_up.append(rel + ".bak")
            ok = _write_from_source(target, rel, root)
            if not ok:
                r.errors.append(f"{rel}: failed to write from dev-kit source")

    # Refresh marker: new installed_dev_kit_version + refreshed
    # template_shas + (re-hashed) installed_file_shas. Preserves the
    # marker's installed_at and prior history fields.
    marker_path = target / MARKER_REL
    if marker_path.is_file():
        try:
            prior_marker = json.loads(marker_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            r.errors.append(f"{MARKER_REL}: marker re-read failed: {e}")
            prior_marker = {}
        new_marker = dict(prior_marker) if isinstance(prior_marker, dict) else {}
        new_marker["installed_dev_kit_version"] = plugin_version(root)
        new_marker["template_shas"] = _compute_template_shas(root)
        # Re-hash installed_file_shas against the on-disk bytes
        new_installed_shas: dict[str, str] = {}
        for rel in EXPECTED_PATHS:
            p = target / rel
            if p.is_file():
                try:
                    new_installed_shas[rel] = _sha256_file(p)
                except OSError:
                    continue
        # Carry forward prior entries for files no longer on disk
        prior_installed = prior_marker.get("installed_file_shas", {}) if isinstance(prior_marker, dict) else {}
        if isinstance(prior_installed, dict):
            for rel, sha in prior_installed.items():
                if rel not in new_installed_shas:
                    new_installed_shas[rel] = sha
        new_marker["installed_file_shas"] = new_installed_shas
        new_marker["installed_by"] = "dev-kit:ci-update"
        atomic_write_json(marker_path, new_marker)
        r.target_version = new_marker["installed_dev_kit_version"]

    r.elapsed_ms = int((time.monotonic() - started) * 1000)
    return r


def _self_test() -> int:
    """Quick CLI sanity check; `python3 lib/ci_update.py` exits 0 on OK."""
    print(f"ci_update.py self-test — plugin_root={_resolve_plugin_root(None)}")
    print(f"  EXPECTED_PATHS count: {len(EXPECTED_PATHS)}")
    print("  UpdateReport attrs: new, updated, unchanged, consumer_modified, diverged, "
          "backed_up, errors, warnings, elapsed_ms, installed_dev_kit_version, "
          "target_version, template_shas")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
