#!/usr/bin/env python3
"""gates_state.py — Project-scoped CI workflow gate SSOT for /dev-kit:gate-select.

State lives at ``.dev-kit/gates.json``, committed in the consumer repo (the
team-visible on/off switch). ``/dev-kit:gate-select`` is its canonical
writer; ``ci-setup`` reads it to derive the install set; ``ci-doctor``
audits it; ``gate-select sync`` pushes per-gate flags to
``gh variable set GATES_<NAME>_ENABLED`` so the workflow ``if:`` conditions
read ``vars.GATES_<NAME>_ENABLED``.

Three gates are first-class today (``review``, ``security``, ``maintenance``);
a fourth gate may be added as an additive bump without a schema version
change because the validator only allows the pinned keys.

Schema v1.1.0 (additive, forward-compat from v1.0.0):
- Each gate may additionally carry optional dynamic-skip fields
  (``dynamic_eligible``, ``scope_globs``, ``cost_estimate``,
  ``judge_model``, ``skip_when``, ``forced_run``) declared in
  ``DYNAMIC_FIELDS_DEFAULT``. These power
  ``/dev-kit:gate-dynamic`` + ``bin/review-local.sh --dynamic-skip``;
  the legacy 3-key shape (v1.0.0) auto-upgrades in-memory on read.
- The validator uses a SUBSET rule (``{enabled, workflow, var} ⊆ keys ⊆
  REQUIRED ∪ DYNAMIC``) instead of strict equality — unknown keys still
  raise, but operator-known optional fields are accepted.

Schema lives in this module as ``SCHEMA_VERSION`` + ``DEFAULT_GATES`` so
``tests/test_ci_setup.py::test_marker_schema_version_current`` can pin the
contract and ``ci_setup`` can read ``DEFAULT_GATES`` for marker projection
without re-implementing the gate inventory.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic import atomic_write_json  # noqa: E402

# Dual-import gh_cli so consumer installs that land `lib/gh_cli.py` next to
# `lib/gates_state.py` (the flat-bundle layout) keep working. Mirrors the
# shim pattern in `lib/ci_setup.py:82-102`.
try:
    from lib.gh_cli import gh_available  # type: ignore
except ImportError:
    from gh_cli import gh_available  # type: ignore

STATE_REL_PATH = Path(".dev-kit") / "gates.json"
SCHEMA_VERSION = "1.1.0"

# Per-gate optional fields for the dynamic-skip layer (consumed by
# `lib/gate_dynamic.py` + `bin/review-local.sh --dynamic-skip`). The
# validator allows any subset of these alongside the required
# `{enabled, workflow, var}` keys. Defaults are seeded on every read so
# callers can introspect a gate entry without a `gate.get(...)` dance.
DYNAMIC_FIELDS_DEFAULT: dict = {
    "dynamic_eligible": False,   # opt-in: gate can be considered for LLM-driven skip
    "scope_globs": [],           # file patterns this gate covers (e.g. ["lib/**", "skills/**"])
    "cost_estimate": 0.0,        # USD hint
    "judge_model": "",           # override judge model (empty = use default)
    "skip_when": "",             # static skip conditions (e.g. "docs_only")
    "forced_run": False,         # operator override — never SKIP this gate
}

# The 3 first-class gate keys. The validator rejects any other top-level
# `gates.<key>` to keep the schema tight; adding a fourth gate is a
# coordinated bump (extend this frozenset + DEFAULT_GATES + the
# `lib/gates_state.<gate>.workflow` comment in templates/ci).
VALID_GATE_KEYS: frozenset[str] = frozenset({"review", "security", "maintenance"})

# The single source of truth for the per-gate inventory. Each gate is
# `{enabled: bool, workflow: str, var: str}` — `workflow` is the file on
# disk in `.github/workflows/`, `var` is the GH repo variable name that
# the workflow's `if:` reads and `sync` pushes via `gh variable set`.
# Adding a gate requires: extend `VALID_GATE_KEYS` + `DEFAULT_GATES`,
# ship a template + add it to `_CI_PATHS_BEFORE_HOOKS`, add the
# `if: vars.<gate.var> != 'false'` line on the gate job.
DEFAULT_GATES: dict = {
    "review": {
        "enabled": True,
        "workflow": "review.yml",
        "var": "GATES_REVIEW_ENABLED",
    },
    "security": {
        "enabled": True,
        "workflow": "security.yml",
        "var": "GATES_SECURITY_ENABLED",
    },
    "maintenance": {
        "enabled": True,
        "workflow": "maintenance.yml",
        "var": "GATES_MAINTENANCE_ENABLED",
    },
}

# Stable iteration order for CLI / sync. Mirrors the order in
# `DEFAULT_GATES` (Python dict insertion order is guaranteed since 3.7).
GATE_ORDER: tuple = tuple(DEFAULT_GATES.keys())


class ValidationError(ValueError):
    """Raised when a gates.json payload fails schema validation.

    The error message includes the field path (e.g.
    `gates.security.var: must equal 'GATES_SECURITY_ENABLED'`) so callers
    can surface a precise remediation message.
    """


def _state_path(root: Optional[Path] = None) -> Path:
    return (root or Path(".")) / STATE_REL_PATH


def _now_utc_iso() -> str:
    """ISO-8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plugin_version_or_zero() -> str:
    """Read `.claude-plugin/plugin.json:version` at runtime.

    Never raises. Returns `"0.0.0"` when the manifest is missing or
    malformed (the in-development sentinel — see
    `lib/ci_setup.plugin_version` for the rationale on runtime derivation
    vs hardcoding the version constant).
    """
    try:
        manifest = (
            Path(__file__).resolve().parent.parent
            / ".claude-plugin"
            / "plugin.json"
        )
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version")
        if isinstance(v, str) and v:
            return v
    except (OSError, json.JSONDecodeError):
        pass
    return "0.0.0"


def _blank_state() -> dict:
    """The fresh-state payload — all keys but ``gates`` empty.

    `installed_at`, `installed_by`, `installed_dev_kit_version` are set
    by the writer at write time (so read returns them only after the
    first write); see `write_state` for the merge.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "installed_at": "",
        "installed_by": "dev-kit:gate-select",
        "installed_dev_kit_version": "",
        "provider_env_key": "CI_REVIEW_PROVIDER",
        "gates": {},
    }


def apply_defaults(state: dict) -> dict:
    """Merge any missing gate keys from DEFAULT_GATES (enabled=True).

    Pure transform: never writes through. Idempotent. Used by `read_state`
    so a partial-state `gates.json` (only `{"review": ...}`) still
    resolves cleanly with `security` + `maintenance` defaulted on. A
    hand-edited extra key that is NOT in `VALID_GATE_KEYS` is left
    alone here — `validate()` rejects it; `read_state` raises.

    v1.1.0: also backfills DYNAMIC_FIELDS_DEFAULT on every gate entry so
    callers can `entry["dynamic_eligible"]` without a guard. Operator-set
    values (e.g. `dynamic_eligible=True`) are preserved via `setdefault`.
    """
    if not isinstance(state, dict):
        raise ValidationError("state must be a dict")
    out = dict(state)
    gates = dict(out.get("gates") or {})
    for key, default in DEFAULT_GATES.items():
        if key not in gates:
            gates[key] = dict(default)
    # Backfill dynamic fields on every gate entry — preserves operator-set
    # values via `setdefault`, fills absent keys with DYNAMIC_FIELDS_DEFAULT.
    for entry in gates.values():
        if isinstance(entry, dict):
            for dyn_key, dyn_default in DYNAMIC_FIELDS_DEFAULT.items():
                entry.setdefault(dyn_key, dyn_default)
    out["gates"] = gates
    return out


def validate(state: object) -> None:
    """Raise ValidationError if `state` does not match the gates.json schema.

    Rules (each violation carries a field path in the message):
      1. `state` is a dict.
      2. `schema_version == "1.1.0"`. (Older 1.0.0 files are auto-migrated
         in-memory by `read_state` before reaching here.)
      3. `gates.keys() ⊆ VALID_GATE_KEYS` (no extra keys).
      4. Each gate value is a dict whose keys are a subset of
         `{enabled, workflow, var} ∪ DYNAMIC_FIELDS_DEFAULT`. The required
         `{enabled, workflow, var}` keys must be present. Unknown keys raise.
      5. `enabled` is a JSON bool (NOT truthy-string).
      6. `workflow` is a non-empty string ending in `.yml` matching the gate key.
      7. `var` is a non-empty string matching `GATES_<KEY_UPPER>_ENABLED`.
      8. Optional dynamic-skip fields (when present) have the right type:
         `dynamic_eligible`/`forced_run` → bool; `scope_globs` → list[str];
         `cost_estimate` → number; `judge_model`/`skip_when` → str.
    """
    if not isinstance(state, dict):
        raise ValidationError("state: must be a dict")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(
            f"schema_version: must equal {SCHEMA_VERSION!r} "
            f"(got {state.get('schema_version')!r})"
        )
    gates = state.get("gates")
    if not isinstance(gates, dict):
        raise ValidationError("gates: must be a dict")
    extra = set(gates.keys()) - VALID_GATE_KEYS
    if extra:
        raise ValidationError(
            f"gates: unknown key(s) {sorted(extra)} — "
            f"allowed: {sorted(VALID_GATE_KEYS)}"
        )
    required_keys = frozenset({"enabled", "workflow", "var"})
    allowed_keys = required_keys | frozenset(DYNAMIC_FIELDS_DEFAULT)
    for key in VALID_GATE_KEYS:
        # Only validate keys that are PRESENT (apply_defaults synthesizes
        # missing keys before validation in the write path). This lets
        # `read_state` of a partial file succeed without forcing the
        # operator to write the full 3-key shape.
        if key not in gates:
            continue
        gate = gates[key]
        if not isinstance(gate, dict):
            raise ValidationError(f"gates.{key}: must be a dict")
        gate_keys = set(gate.keys())
        if not required_keys.issubset(gate_keys):
            missing = sorted(required_keys - gate_keys)
            raise ValidationError(
                f"gates.{key}: missing required key(s) {missing} "
                f"(got {sorted(gate_keys)})"
            )
        unknown = gate_keys - allowed_keys
        if unknown:
            raise ValidationError(
                f"gates.{key}: unknown key(s) {sorted(unknown)} — "
                f"allowed: {sorted(allowed_keys)}"
            )
        enabled = gate.get("enabled")
        if not isinstance(enabled, bool):
            raise ValidationError(
                f"gates.{key}.enabled: must be a JSON bool "
                f"(got {type(enabled).__name__} {enabled!r})"
            )
        workflow = gate.get("workflow")
        expected_workflow = f"{key}.yml"
        if not isinstance(workflow, str) or workflow != expected_workflow:
            raise ValidationError(
                f"gates.{key}.workflow: must equal {expected_workflow!r} "
                f"(got {workflow!r})"
            )
        var = gate.get("var")
        expected_var = f"GATES_{key.upper()}_ENABLED"
        if not isinstance(var, str) or var != expected_var:
            raise ValidationError(
                f"gates.{key}.var: must equal {expected_var!r} (got {var!r})"
            )
        # Type checks for v1.1.0 optional fields (only when present).
        if "dynamic_eligible" in gate and not isinstance(gate["dynamic_eligible"], bool):
            raise ValidationError(
                f"gates.{key}.dynamic_eligible: must be a JSON bool "
                f"(got {type(gate['dynamic_eligible']).__name__})"
            )
        if "forced_run" in gate and not isinstance(gate["forced_run"], bool):
            raise ValidationError(
                f"gates.{key}.forced_run: must be a JSON bool "
                f"(got {type(gate['forced_run']).__name__})"
            )
        if "scope_globs" in gate:
            globs = gate["scope_globs"]
            if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
                raise ValidationError(
                    f"gates.{key}.scope_globs: must be a list of strings "
                    f"(got {type(globs).__name__})"
                )
        if "cost_estimate" in gate and not isinstance(gate["cost_estimate"], (int, float)):
            raise ValidationError(
                f"gates.{key}.cost_estimate: must be a number "
                f"(got {type(gate['cost_estimate']).__name__})"
            )
        for str_field in ("judge_model", "skip_when"):
            if str_field in gate and not isinstance(gate[str_field], str):
                raise ValidationError(
                    f"gates.{key}.{str_field}: must be a string "
                    f"(got {type(gate[str_field]).__name__})"
                )


def read_state(root: Optional[Path] = None) -> dict:
    """Read `.dev-kit/gates.json` and return the applied-defaults state.

    - Missing file → defaults synthesized (all 3 gates enabled).
    - Corrupt JSON / IO error → `ValidationError` (callers can catch;
      the CLI does so and exits 2).
    - Partial `gates` (only `review`) → defaults fill in the rest.
    - Extra unknown `gates` key → `ValidationError` (strict).
    - `schema_version: "1.0.0"` → in-memory migration to v1.1.0: each gate
      gets DYNAMIC_FIELDS_DEFAULT backfilled via `setdefault`. The on-disk
      file is NOT rewritten; the next explicit `set` / `write_state` call
      persists the bump.
    """
    path = _state_path(root)
    if not path.exists():
        return apply_defaults(_blank_state())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValidationError(f"{path}: read error: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationError(f"{path}: invalid JSON: {e}") from e
    # Read-time auto-migration v1.0.0 → v1.1.0. Only adds keys that are
    # absent — operator-known values (e.g. dynamic_eligible=True already
    # written by a 1.1.0-aware CLI) are preserved verbatim. The disk file
    # is untouched here; the next `write_state` call persists the bump.
    if isinstance(raw, dict) and raw.get("schema_version") == "1.0.0":
        raw["schema_version"] = "1.1.0"
        gates_obj = raw.get("gates")
        if isinstance(gates_obj, dict):
            for entry in gates_obj.values():
                if isinstance(entry, dict):
                    for k, v in DYNAMIC_FIELDS_DEFAULT.items():
                        entry.setdefault(k, v)
    validate(raw)
    return apply_defaults(raw)


def write_state(state: dict, root: Optional[Path] = None) -> dict:
    """Validate, stamp installed_at/owner/version, atomic-write.

    Returns the persisted payload (post-stamp, post-validate). Raises
    `ValidationError` on shape violation before the temp-file dance so a
    bad caller never half-writes. ``installed_at`` is bumped on every
    write so the file's mtime is honest about the last edit; the same
    pattern as `lib/ci_setup._backfill_marker_schema`.
    """
    if not isinstance(state, dict):
        raise ValidationError("state: must be a dict")
    stamped = dict(state)
    stamped["installed_at"] = _now_utc_iso()
    stamped["installed_by"] = "dev-kit:gate-select"
    stamped["installed_dev_kit_version"] = _plugin_version_or_zero()
    finalized = apply_defaults(stamped)
    validate(finalized)
    atomic_write_json(_state_path(root), finalized)
    return finalized


def is_enabled(gate: str, root: Optional[Path] = None) -> bool:
    """True iff `gate` is enabled in gates.json. Unknown gate → True (fail-on).

    Unknown gate names are treated as enabled — a typo in a workflow's
    `vars.GATES_<NAME>_ENABLED != 'false'` check must never silently
    disable a gate the operator intended to keep on. Mirrors the
    "correctness = always on" design in `lib/harness_mode_state.py`.
    """
    if gate not in VALID_GATE_KEYS:
        return True
    return bool(read_state(root)["gates"][gate]["enabled"])


def workflow_for(gate: str) -> str:
    """Return the workflow basename for `gate` (e.g. ``review.yml``)."""
    if gate not in VALID_GATE_KEYS:
        raise ValidationError(f"unknown gate: {gate!r}")
    return DEFAULT_GATES[gate]["workflow"]


def var_for(gate: str) -> str:
    """Return the GH repo variable name for `gate` (e.g. ``GATES_REVIEW_ENABLED``)."""
    if gate not in VALID_GATE_KEYS:
        raise ValidationError(f"unknown gate: {gate!r}")
    return DEFAULT_GATES[gate]["var"]


def runners_from_gates(root: Optional[Path] = None) -> list:
    """Return the per-judge workflow basenames that are enabled.

    Output is in stable GATE_ORDER — `review`, `security`, `maintenance`
    filtered to those with `enabled=True`. Used by `lib/ci_setup` to
    derive the `runners` field of the marker when `gates.json` is
    present (replaces the legacy `exclude=` argument as the SSOT for
    what gets installed).
    """
    state = read_state(root)
    return [
        DEFAULT_GATES[key]["workflow"]
        for key in GATE_ORDER
        if state["gates"][key]["enabled"]
    ]


# ----------------------------------------------------------------------------
# sync — push the per-gate enabled/disabled flag to GH repo variables.
# ----------------------------------------------------------------------------

# Best-effort owner/repo probe. Mirrors `lib/ci_setup.detect_owner_repo`
# (lines 1426-1458) byte-for-byte so the body-equivalence test
# `tests/test_gates_state.py::TestDetectOwnerRepoBodyEquivalence` passes
# without depending on `git init`. The two copies MUST stay in sync;
# the assertion guards against drift (issue #834 round 2 MAJOR #3).
def detect_owner_repo(target_dir: Path) -> str:
    """Best-effort `<OWNER>/<REPO>` from git remote.

    Returns `<OWNER>/<REPO>` on success. On failure (no git, no remote,
    non-GitHub remote, timeout), returns the literal `<OWNER>/<REPO>`
    placeholder with a `(auto-detect failed: <ExceptionType>)` suffix
    so the post-install checklist still renders usefully AND the user
    sees WHY auto-detection failed. Never raises.
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


def _sync_one(
    gh: str,
    repo: str,
    gate: str,
    body: str,
    *,
    timeout: int = 10,
) -> tuple:
    """Run `gh variable set <var> --repo <r> --body <body>` for one gate.

    Returns (ok, stderr_tail). Never raises — failures land in the
    caller's `failed` list so a single broken var doesn't abort the rest.
    """
    var = var_for(gate)
    try:
        cp = subprocess.run(
            [gh, "variable", "set", var, "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}: {e}"
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout or "").strip() or f"exit={cp.returncode}"
    return True, ""


def sync(
    *,
    root: Optional[Path] = None,
    repo: Optional[str] = None,
    _gh: Optional[str] = None,
    _degraded: str = "",
) -> dict:
    """Push gates.json on/off flags to GH repo variables.

    Each gate becomes one ``gh variable set GATES_<NAME>_ENABLED --body
    "<enabled>"`` call. Result shape::

        {"gh_path": str|None,
         "repo": str,
         "results": {<gate>: {"ok": bool, "stderr": str}}, ...}

    Returns ``{"gh_path": None, "degraded": "<reason>"}`` when gh is
    unauthenticated / absent — the caller surfaces this as a SKIP / WARN.
    """
    out: dict = {"results": {}, "repo": repo or ""}
    gh_path = _gh
    if gh_path is None and not _degraded:
        gh_path, _degraded = gh_available(timeout=5)
    if not gh_path:
        out["gh_path"] = None
        out["degraded"] = _degraded or "gh not on PATH"
        return out
    out["gh_path"] = gh_path
    detected = repo or detect_owner_repo((root or Path(".")).resolve())
    if detected.startswith("<OWNER>/<REPO>"):
        out["gh_path"] = gh_path
        out["degraded"] = f"no github remote: {detected}"
        return out
    out["repo"] = detected
    state = read_state(root)
    for gate in GATE_ORDER:
        body = "true" if state["gates"][gate]["enabled"] else "false"
        ok, stderr = _sync_one(gh_path, detected, gate, body)
        out["results"][gate] = {"ok": ok, "stderr": stderr}
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _init_synthesize(root: Optional[Path]) -> dict:
    """Build a fresh gates.json payload from `marker.runners`.

    Reads `.dev-kit/ci-config.json:runners`. Each first-class gate
    (`review.yml`, `security.yml`, `maintenance.yml`) becomes a gate
    entry: enabled when the corresponding workflow is in `marker.runners`
    (or when no marker is present — i.e. operator never ran ci-setup),
    disabled otherwise. ci.yml + auto-fix-pr.yml are always-on so they
    are NOT represented as gates.

    Writes the synthesized payload via `write_state` so the resulting
    file passes the same schema check a hand-edited gates.json would.
    Prints the payload + a one-line migration hint.
    """
    import json as _json
    import sys as _sys
    marker_path = (root or Path(".")) / Path(".dev-kit") / "ci-config.json"
    installed = set()
    # Treat a missing OR unreadable/corrupt marker as "no marker" — we
    # don't want a corrupt file to silently disable gates by accident.
    # The operator can `ci-doctor` to find the corrupt marker.
    no_marker = True
    if marker_path.is_file():
        try:
            payload = _json.loads(marker_path.read_text(encoding="utf-8"))
            runners = payload.get("runners") or []
            if isinstance(runners, list):
                installed = {r for r in runners if isinstance(r, str)}
                no_marker = False
        except (OSError, ValueError):
            pass
    # Build per-gate enabled flags. When no marker is present (fresh
    # install OR corrupt marker), default everything to enabled; the
    # operator can `disable` after init.
    gates = {}
    for key, default in DEFAULT_GATES.items():
        workflow = default["workflow"]
        enabled = True if no_marker else (workflow in installed)
        gates[key] = dict(default, enabled=enabled)
    synthesized = {
        "schema_version": SCHEMA_VERSION,
        "installed_by": "dev-kit:gate-select",
        "provider_env_key": "CI_REVIEW_PROVIDER",
        "gates": gates,
    }
    written = write_state(synthesized, root)
    # Migration hint goes to stderr so it doesn't pollute a JSON capture.
    print(
        f"::notice::synthesized gates.json with {sum(g['enabled'] for g in gates.values())} "
        f"enabled + {sum(not g['enabled'] for g in gates.values())} disabled gates",
        file=_sys.stderr,
    )
    return written


def _set_field(state: dict, gate: str, key: str, value: str) -> dict:
    """Set one nested field on the gate entry, parsing stringy bools.

    Used by the `set <gate> <key> <value>` CLI sub-command so the
    operator doesn't have to hand-edit JSON. `enabled` accepts
    ``true|false|1|0|yes|no`` (case-insensitive, the same allowlist
    the bash `bin/set-provider.sh:234` uses for provider values).
    `dynamic_eligible`/`forced_run` accept the same bool aliases;
    `scope_globs` accepts comma-separated strings OR a JSON list;
    `cost_estimate` accepts a JSON number string; the rest are strings.
    """
    if gate not in VALID_GATE_KEYS:
        raise ValidationError(f"unknown gate: {gate!r}")
    allowed_keys = {"enabled", "workflow", "var"} | set(DYNAMIC_FIELDS_DEFAULT)
    if key not in allowed_keys:
        raise ValidationError(
            f"unknown field {key!r} — allowed: {sorted(allowed_keys)}"
        )
    gates = dict(state.get("gates") or {})
    entry = dict(gates.get(gate) or {})
    if key == "enabled" or key in ("dynamic_eligible", "forced_run"):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            entry[key] = True
        elif v in ("false", "0", "no"):
            entry[key] = False
        else:
            raise ValidationError(
                f"{key}: must be true|false|1|0|yes|no (got {value!r})"
            )
    elif key == "workflow":
        entry["workflow"] = value
    elif key == "var":
        entry["var"] = value
    elif key == "scope_globs":
        # Accept either a JSON list (`["lib/**"]`) or a comma-separated
        # string (`lib/**,skills/**`). The CLI surfaces both shapes.
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f"scope_globs: invalid JSON list (got {value!r}): {e}"
                ) from e
            if not isinstance(parsed, list) or not all(
                isinstance(g, str) for g in parsed
            ):
                raise ValidationError(
                    f"scope_globs: must be a list of strings (got {value!r})"
                )
            entry[key] = parsed
        else:
            entry[key] = [g.strip() for g in stripped.split(",") if g.strip()]
    elif key == "cost_estimate":
        try:
            entry[key] = float(value)
        except ValueError as e:
            raise ValidationError(
                f"cost_estimate: must be a number (got {value!r})"
            ) from e
    else:
        # judge_model, skip_when — pass through as-is.
        entry[key] = value
    gates[gate] = entry
    new = dict(state)
    new["gates"] = gates
    return new


def main(argv=None) -> int:
    """CLI entry point. Returns the process exit code.

    `argv` defaults to `sys.argv[1:]` so `python -m lib.gates_state ...`
    Just Works; tests pass an explicit list for hermetic execution.
    """
    parser = argparse.ArgumentParser(
        description="project-scoped CI workflow gate SSOT (.dev-kit/gates.json)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show_p = sub.add_parser(
        "show", help="print the resolved state of every gate"
    )
    show_p.add_argument(
        "--json", action="store_true", help="compact single-line JSON"
    )
    show_p.add_argument(
        "--root", default=None, help="project root (default: cwd)"
    )

    for action in ("enable", "disable"):
        sp = sub.add_parser(action, help=f"{action} one gate's `enabled` flag")
        sp.add_argument("gate", choices=GATE_ORDER)
        sp.add_argument("--root", default=None)

    set_p = sub.add_parser(
        "set", help="set one nested field on a gate entry (JSON-escapable)"
    )
    set_p.add_argument("gate", choices=GATE_ORDER)
    set_p.add_argument(
        "key",
        choices=("enabled", "workflow", "var") + tuple(DYNAMIC_FIELDS_DEFAULT),
    )
    set_p.add_argument("value")
    set_p.add_argument("--root", default=None)

    sync_p = sub.add_parser(
        "sync", help="push enabled flags to gh variable set GATES_<NAME>_ENABLED"
    )
    sync_p.add_argument("--root", default=None)
    sync_p.add_argument("--repo", default=None)

    init_p = sub.add_parser(
        "init",
        help=(
            "synthesize .dev-kit/gates.json from .dev-kit/ci-config.json runner list. "
            "Each runner in marker.runners (other than ci.yml + auto-fix-pr.yml) "
            "becomes a gate with enabled=true; each non-installed workflow "
            "becomes enabled=false."
        ),
    )
    init_p.add_argument("--root", default=None)

    validate_p = sub.add_parser(
        "validate", help="read + validate; exit 0 ok, 2 ValidationError"
    )
    validate_p.add_argument("--root", default=None)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ValidationError as e:
        print(f"gates_state: {e}", file=sys.stderr)
        return 2


def _dispatch(args) -> int:
    if args.command == "show":
        root = Path(args.root) if args.root else None
        state = read_state(root)
        gates_out = {}
        for k in GATE_ORDER:
            entry = state["gates"][k]
            gates_out[k] = {
                "enabled": entry["enabled"],
                "workflow": entry["workflow"],
                "var": entry["var"],
            }
            # v1.1.0 — include dynamic-skip fields when they deviate from
            # defaults, so the operator can audit gate-skip eligibility
            # from `show` without opening the raw JSON.
            for dyn_key, dyn_default in DYNAMIC_FIELDS_DEFAULT.items():
                if entry.get(dyn_key) != dyn_default:
                    gates_out[k][dyn_key] = entry[dyn_key]
        out = {
            "schema_version": state.get("schema_version"),
            "gates": gates_out,
        }
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if args.command in ("enable", "disable"):
        root = Path(args.root) if args.root else None
        state = read_state(root)
        new = _set_field(state, args.gate, "enabled", "true" if args.command == "enable" else "false")
        write_state(new, root)
        print(f"{args.gate}: enabled={new['gates'][args.gate]['enabled']}")
        return 0
    if args.command == "set":
        root = Path(args.root) if args.root else None
        state = read_state(root)
        new = _set_field(state, args.gate, args.key, args.value)
        write_state(new, root)
        print(f"{args.gate}.{args.key} = {new['gates'][args.gate][args.key]!r}")
        return 0
    if args.command == "sync":
        root = Path(args.root) if args.root else None
        repo = getattr(args, "repo", None)
        result = sync(root=root, repo=repo)
        if result.get("degraded"):
            print(
                f"::warning::gates_state sync: {result['degraded']} — "
                f"variables may be out of date until 'gh auth login' && sync again",
                file=sys.stderr,
            )
            return 3
        failed = [g for g, r in result["results"].items() if not r["ok"]]
        for gate in GATE_ORDER:
            r = result["results"][gate]
            mark = "✓" if r["ok"] else "✗"
            body = "true" if read_state(root)["gates"][gate]["enabled"] else "false"
            print(f"{mark} {gate}: var={var_for(gate)} body={body}")
        if failed:
            print(
                f"gates_state sync: {len(failed)}/{len(result['results'])} failed",
                file=sys.stderr,
            )
            return 1
        return 0
    if args.command == "init":
        root = Path(args.root) if args.root else None
        out = _init_synthesize(root)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        root = Path(args.root) if args.root else None
        read_state(root)  # raises ValidationError on bad shape
        print("OK")
        return 0
    return 1  # argparse `required=True` should make this unreachable


if __name__ == "__main__":
    sys.exit(main())
