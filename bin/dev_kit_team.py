#!/usr/bin/env python3
"""dev_kit_team.py — pure CLI for /dev-kit:team.

Subcommands:
  resolve   — print the resolved team value (uses hooks/lib/team-resolve.sh).
  write     — write DEV_KIT_TEAM to project or local scope.
  show      — print "team: <on|off>  (set via <source>)".

No subprocess side effects beyond writing the chosen JSON file. Idempotent
on re-run: re-writing the same value is a no-op (preserves other keys in
the settings file).

Independent of DEV_KIT_MODE (full|lite|undev). The two env-vars
coexist; `mode` selects the skill/hook subset, `team` toggles whether
.dev-kit/ is tracked in git.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEAM_LIB = REPO_ROOT / "hooks" / "lib" / "team-resolve.sh"


def _resolve_team(cwd: Path) -> tuple[str, str]:
    """Return (team, source). Always delegates to hooks/lib/team-resolve.sh
    via _bash_resolve so the 4-layer precedence rule lives in exactly one
    place. Source is reported separately for the `show` subcommand.

    Fallback: if bash delegation fails (jq missing, etc.), fall back to
    a minimal Python-side resolution so the CLI stays usable. The
    fallback is intentionally simple (Layer 1 + Layer 4 only) — the
    authoritative contract is in team-resolve.sh.
    """
    try:
        return _bash_resolve_full(cwd)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Fallback: Layer 1 (shell) + Layer 4 (default).
        shell_team = os.environ.get("DEV_KIT_TEAM", "")
        if shell_team.lower() in {"on", "1", "true"}:
            return "on", "shell"
        if shell_team.lower() in {"off", "0", "false"}:
            return "off", "shell"
        return "off", "default"


def _bash_resolve_full(cwd: Path) -> tuple[str, str]:
    """Delegate fully to team-resolve.sh. Returns (team, source)."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    script = (
        f'source "{TEAM_LIB}"; '
        'dev_kit_team_resolve; '
        'printf "%s\\t%s\\n" "${DEV_KIT_TEAM}" "${DEV_KIT_TEAM_SOURCE:-default}"'
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
        v = env.get("DEV_KIT_TEAM")
        if isinstance(v, str):
            return v
    return None


def cmd_resolve(args) -> int:
    cwd = Path(args.target or os.getcwd())
    team, _source = _resolve_team(cwd)
    print(team)
    return 0


def cmd_show(args) -> int:
    cwd = Path(args.target or os.getcwd())
    team, source = _resolve_team(cwd)
    # Label is constant ("team") for both branches; matches the mode CLI
    # shape (`f"current mode: {mode}  (set via {source})"`) which also
    # emits no label prefix.
    label = "team"
    print(f"{label}: {team.upper()}  (set via {source})")
    return 0


def cmd_write(args) -> int:
    if not args.state:
        print("error: --on/--off is required for write", file=sys.stderr)
        return 2
    if args.state not in {"on", "off"}:
        # argparse `choices=` already guards this; kept as belt-and-braces.
        print(f"error: invalid state {args.state!r}; must be on|off",
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
    if args.state == "off":
        # Off = remove the key entirely. The silent default is "off", so
        # leaving the key set to "off" would be indistinguishable from
        # explicit-off but would clutter the JSON.
        body["env"].pop("DEV_KIT_TEAM", None)
    else:
        body["env"]["DEV_KIT_TEAM"] = "1"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body, indent=2) + "\n")

    if args.state == "off":
        print(f"removed DEV_KIT_TEAM from {target.relative_to(proj_root)} "
              f"(now defaults to off)")
    else:
        print(f"wrote DEV_KIT_TEAM=1 to {target.relative_to(proj_root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dev-kit team toggle")
    parser.add_argument("--target", help="project dir (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_resolve = sub.add_parser("resolve", help="print resolved team value (one line)")
    p_resolve.set_defaults(func=cmd_resolve)

    p_show = sub.add_parser("show", help="print current team value + source")
    p_show.set_defaults(func=cmd_show)

    p_write = sub.add_parser("write", help="write DEV_KIT_TEAM")
    state = p_write.add_mutually_exclusive_group(required=True)
    state.add_argument("--on", dest="state", action="store_const", const="on")
    state.add_argument("--off", dest="state", action="store_const", const="off")
    p_write.add_argument("--scope", choices=["project", "local"], default="project")
    p_write.set_defaults(func=cmd_write)

    args = parser.parse_args(argv)
    if args.cmd is None:
        # Default: show
        return cmd_show(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
