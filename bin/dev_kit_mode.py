#!/usr/bin/env python3
"""dev_kit_mode.py — pure CLI for /dev-kit:mode.

Subcommands:
  resolve   — print the resolved mode (uses hooks/lib/mode-resolve.sh).
  write     — write DEV_KIT_MODE to project or local scope.
  show      — print "current mode: <X>  (set via <source>)".

No subprocess side effects beyond writing the chosen JSON file. Idempotent
on re-run: re-writing the same mode is a no-op (preserves other keys in
the settings file).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODE_LIB = REPO_ROOT / "hooks" / "lib" / "mode-resolve.sh"


def _resolve_mode(cwd: Path) -> tuple[str, str]:
    """Return (mode, source). Always delegates to hooks/lib/mode-resolve.sh
    via _bash_resolve so the 4-layer precedence rule lives in exactly one
    place. Source is reported separately via _resolve_source for the
    `show` subcommand.

    Fallback: if bash delegation fails (jq missing, etc.), fall back to
    a minimal Python-side resolution so the CLI stays usable. The
    fallback is intentionally simple (Layer 1 + Layer 4 only) — the
    authoritative contract is in mode-resolve.sh.
    """
    try:
        return _bash_resolve_full(cwd)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Fallback: Layer 1 (shell) + Layer 4 (default).
        shell_mode = os.environ.get("DEV_KIT_MODE", "")
        if shell_mode in {"full", "lite", "undev"}:
            return shell_mode, "shell"
        return _default_for_cwd(cwd), "default"


def _bash_resolve_full(cwd: Path) -> tuple[str, str]:
    """Delegate fully to mode-resolve.sh. Returns (mode, source)."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    script = (
        f'source "{MODE_LIB}"; '
        'dev_kit_mode_resolve; '
        'printf "%s\\t%s\\n" "${DEV_KIT_MODE}" "${DEV_KIT_MODE_SOURCE:-default}"'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=10, cwd=str(cwd), env=env,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                           output=result.stdout, stderr=result.stderr)
    out = result.stdout.strip().split("\t")
    if len(out) >= 2:
        return out[0], out[1]
    return out[0], "default"


def _default_for_cwd(cwd: Path) -> str:
    """Layer 4 conditional default — used as fallback only."""
    proj_root = _project_root(cwd)
    if proj_root is None:
        return "undev"
    proj_settings = proj_root / ".claude" / "settings.json"
    return "full" if _enabled_plugins(proj_settings) else "undev"


def _bash_resolve(cwd: Path) -> str:
    """Delegate to the bash helper — returns mode only. Used by older
    call sites and as a thin wrapper around _bash_resolve_full."""
    mode, _source = _bash_resolve_full(cwd)
    return mode


def _project_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, cwd=str(cwd),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _read_env(settings_file: Path) -> str | None:
    if not settings_file.is_file():
        return None
    try:
        body = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    env = body.get("env") or {}
    if isinstance(env, dict):
        v = env.get("DEV_KIT_MODE")
        if isinstance(v, str):
            return v
    return None


def _enabled_plugins(settings_file: Path) -> bool:
    if not settings_file.is_file():
        return False
    try:
        body = json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    plugins = body.get("enabledPlugins") or {}
    return any(k.startswith("dev-kit@") for k in plugins)


def cmd_resolve(args) -> int:
    cwd = Path(args.target or os.getcwd())
    mode, source = _resolve_mode(cwd)
    print(mode)
    return 0


def cmd_show(args) -> int:
    cwd = Path(args.target or os.getcwd())
    mode, source = _resolve_mode(cwd)
    print(f"current mode: {mode}  (set via {source})")
    return 0


def cmd_write(args) -> int:
    if not args.mode:
        print("error: --mode is required for write", file=sys.stderr)
        return 2
    if args.mode not in {"full", "lite", "undev"}:
        print(f"error: invalid mode {args.mode!r}; must be full|lite|undev",
              file=sys.stderr)
        return 2

    cwd = Path(args.target or os.getcwd())
    proj_root = _project_root(cwd)
    if proj_root is None:
        print("error: not in a git repo; nothing to write", file=sys.stderr)
        return 1
    if not (proj_root / ".claude").is_dir():
        print(f"error: {proj_root}/.claude does not exist; run /dev-kit:bootstrap first",
              file=sys.stderr)
        return 1

    if args.scope == "local":
        target = proj_root / ".claude" / "settings.local.json"
    else:
        target = proj_root / ".claude" / "settings.json"

    body: dict = {}
    if target.is_file():
        try:
            body = json.loads(target.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"warning: {target} exists but is not valid JSON; rewriting",
                  file=sys.stderr)
            body = {}

    body.setdefault("env", {})
    if not isinstance(body["env"], dict):
        body["env"] = {}
    body["env"]["DEV_KIT_MODE"] = args.mode

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body, indent=2) + "\n")

    print(f"wrote DEV_KIT_MODE={args.mode} to {target.relative_to(proj_root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dev-kit mode selector")
    parser.add_argument("--target", help="project dir (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_resolve = sub.add_parser("resolve", help="print resolved mode (one line)")
    p_resolve.set_defaults(func=cmd_resolve)

    p_show = sub.add_parser("show", help="print current mode + source")
    p_show.set_defaults(func=cmd_show)

    p_write = sub.add_parser("write", help="write DEV_KIT_MODE")
    p_write.add_argument("--mode", choices=["full", "lite", "undev"])
    p_write.add_argument("--scope", choices=["project", "local"], default="project")
    p_write.set_defaults(func=cmd_write)

    args = parser.parse_args(argv)
    if args.cmd is None:
        # Default: show
        return cmd_show(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
