#!/usr/bin/env python3
"""Create/delete managed GitHub Action gate artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Dual-import so consumer installs that ship `lib/*.py` flat (no
# `__init__.py` in the consumer `lib/`) keep working.
try:
    from . import gates_state  # type: ignore
    from .atomic import atomic_write_json  # type: ignore
except ImportError:
    import gates_state  # noqa: E402
    from atomic import atomic_write_json  # noqa: E402

MANIFEST_REL_PATH = Path(".dev-kit") / "gate-artifacts.json"
MANAGED_BY = "dev-kit:gate-artifacts"
MANIFEST_SCHEMA_VERSION = "1.0.0"
GITIGNORE_ENTRIES = (".gjc/", ".worktrees/")


class GateArtifactError(ValueError):
    """Raised when a gate artifact operation is unsafe or invalid."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise GateArtifactError(f"path escapes root: {path}") from exc


def _manifest_path(root: Path) -> Path:
    return root / MANIFEST_REL_PATH


def _blank_manifest() -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "managed_by": MANAGED_BY,
        "updated_at": "",
        "gates": {},
    }


def read_manifest(root: Path) -> dict[str, Any]:
    path = _manifest_path(root)
    if not path.exists():
        return _blank_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateArtifactError(f"{path}: invalid manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise GateArtifactError(f"{path}: manifest must be a JSON object")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise GateArtifactError(
            f"{path}: schema_version must equal {MANIFEST_SCHEMA_VERSION!r}"
        )
    if data.get("managed_by") != MANAGED_BY:
        raise GateArtifactError(f"{path}: managed_by must equal {MANAGED_BY!r}")
    if not isinstance(data.get("gates"), dict):
        raise GateArtifactError(f"{path}: gates must be an object")
    return data


def write_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    out = dict(manifest)
    out["schema_version"] = MANIFEST_SCHEMA_VERSION
    out["managed_by"] = MANAGED_BY
    out["updated_at"] = _now_utc_iso()
    out.setdefault("gates", {})
    path = _manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, out)
    return out


def workflow_body(gate: str) -> str:
    var = gates_state.var_for(gate)
    title = gate.replace("-", " ").title()
    return f"""name: Gate / {title}

on:
  pull_request:
  workflow_dispatch:

jobs:
  {gate.replace('-', '_')}:
    if: vars.{var} != 'false'
    runs-on: ubuntu-latest
    steps:
      - name: Gate placeholder
        run: |
          echo "{gate} gate is enabled. Replace this managed placeholder with real checks."
"""


def ensure_gitignore(root: Path) -> dict[str, Any]:
    path = root / ".gitignore"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = before.splitlines()
    added: list[str] = []
    for entry in GITIGNORE_ENTRIES:
        if entry not in lines:
            lines.append(entry)
            added.append(entry)
    if added:
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"path": ".gitignore", "added": added}


def _load_state(root: Path) -> dict[str, Any]:
    return gates_state.read_state(root)


def _write_state(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    return gates_state.write_state(state, root)


def create_gate(root: Path, gate: str, *, overwrite: bool = False) -> dict[str, Any]:
    root = root.resolve()
    gates_state.validate_gate_name(gate, allow_builtin=False)
    workflow_rel = Path(".github") / "workflows" / gates_state.workflow_for(gate)
    workflow_path = root / workflow_rel
    body = workflow_body(gate)

    manifest = read_manifest(root)
    manifest_gates = dict(manifest.get("gates") or {})
    existing_record = manifest_gates.get(gate)
    if existing_record and not overwrite:
        return {"changed": False, "gate": gate, "reason": "already managed"}
    if workflow_path.exists() and not overwrite:
        raise GateArtifactError(f"refusing to overwrite existing unmanaged workflow: {workflow_rel}")

    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(body, encoding="utf-8")

    state = _load_state(root)
    gates = dict(state.get("gates") or {})
    gates[gate] = gates_state.default_gate_entry(gate, enabled=True)
    state["gates"] = gates
    _write_state(root, state)

    gitignore = ensure_gitignore(root)
    artifact = {
        "path": workflow_rel.as_posix(),
        "kind": "github-actions-workflow",
        "sha256": _sha256_text(body),
    }
    manifest_gates[gate] = {
        "name": gate,
        "workflow": gates_state.workflow_for(gate),
        "var": gates_state.var_for(gate),
        "managed_by": MANAGED_BY,
        "created_at": existing_record.get("created_at") if isinstance(existing_record, dict) else _now_utc_iso(),
        "updated_at": _now_utc_iso(),
        "artifacts": [artifact],
    }
    manifest["gates"] = manifest_gates
    write_manifest(root, manifest)
    return {
        "changed": True,
        "gate": gate,
        "workflow": workflow_rel.as_posix(),
        "var": gates_state.var_for(gate),
        "gitignore": gitignore,
    }


def delete_gate(root: Path, gate: str) -> dict[str, Any]:
    root = root.resolve()
    gates_state.validate_gate_name(gate, allow_builtin=False)
    manifest = read_manifest(root)
    manifest_gates = dict(manifest.get("gates") or {})
    record = manifest_gates.get(gate)
    if not isinstance(record, dict):
        raise GateArtifactError(f"refusing to delete unmanaged gate: {gate}")
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        raise GateArtifactError(f"manifest gate {gate}: artifacts must be a list")

    removed: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise GateArtifactError(f"manifest gate {gate}: artifact must be an object")
        rel = artifact.get("path")
        expected_sha = artifact.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected_sha, str):
            raise GateArtifactError(f"manifest gate {gate}: artifact path/sha256 required")
        path = (root / rel).resolve()
        normalized = _rel(path, root)
        if normalized != rel:
            raise GateArtifactError(f"manifest gate {gate}: non-normal path {rel!r}")
        if not path.exists():
            continue
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            raise GateArtifactError(
                f"refusing to delete modified managed artifact {rel}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        path.unlink()
        removed.append(rel)

    state = _load_state(root)
    gates = dict(state.get("gates") or {})
    gates.pop(gate, None)
    state["gates"] = gates
    _write_state(root, state)

    manifest_gates.pop(gate, None)
    manifest["gates"] = manifest_gates
    write_manifest(root, manifest)
    return {"changed": bool(removed), "gate": gate, "removed": removed}


def plan_gate(root: Path, gate: str) -> dict[str, Any]:
    gates_state.validate_gate_name(gate, allow_builtin=False)
    return {
        "gate": gate,
        "workflow": (Path(".github") / "workflows" / gates_state.workflow_for(gate)).as_posix(),
        "var": gates_state.var_for(gate),
        "manifest": MANIFEST_REL_PATH.as_posix(),
        "gitignore_entries": list(GITIGNORE_ENTRIES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="manage dev-kit GitHub Action gate artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "create", "delete"):
        sp = sub.add_parser(name)
        sp.add_argument("gate")
        sp.add_argument("--root", default=".")
    sub.choices["create"].add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "plan":
            result = plan_gate(root, args.gate)
        elif args.command == "create":
            result = create_gate(root, args.gate, overwrite=args.overwrite)
        elif args.command == "delete":
            result = delete_gate(root, args.gate)
        else:
            raise AssertionError(args.command)
    except (GateArtifactError, gates_state.ValidationError) as exc:
        print(f"gate_artifacts: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
