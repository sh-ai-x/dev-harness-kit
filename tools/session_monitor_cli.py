"""session_monitor_cli.py -- argparse + entrypoint.

Splits the CLI surface (``parse_args`` / ``main`` / ``_validate_modes``)
out of ``tools/session_monitor.py``. Pulling these out keeps the main
module focused on data collection + agent graph + status, while the
CLI module owns the argument contract and the dispatching logic.

Public surface (re-exported by ``tools/session_monitor.py`` so callers
keep using ``sm.parse_args`` / ``sm.main`` / ``sm._validate_modes``):
- ``parse_args``
- ``_validate_modes``
- ``main``
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Imports are deferred to call sites so this module can be loaded
# before ``tools/session_monitor.py`` has finished defining the data
# collection + dispatching functions (``build_model`` / ``build_resume``
# / ``discover_repo_root`` / etc.). The main module re-exports
# ``parse_args`` / ``_validate_modes`` / ``main`` at import time, so
# any caller that touches those first will load the cli module after
# the main module is fully constructed.
import skill_usage  # noqa: E402  (tools/ on sys.path from parent)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inline arrow-key picker over Claude Code + Codex sessions "
                    "with worktree-aware resume.")
    p.add_argument("--logs-dir", default="",
                   help="logs root (default: <main-repo>/logs)")
    p.add_argument("--repo", default="",
                   help="filter sessions to this repo name substring")
    p.add_argument("--days", type=int, default=30,
                   help="only sessions active within N days (default 30)")
    # Data-mode family: --list / --json each route the program to a
    # distinct data-emit handler. Combined sets are caught by
    # ``_validate_modes`` (which also re-checks --print-resume-command
    # since it is registered in the operator-mode group) so the caller
    # sees a clear conflict error rather than a silent precedence drop.
    p.add_argument("--list", action="store_true",
                   help="print a plain listing instead of the picker")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON (used by the "
                        "/dev-kit:session-monitor skill's AskUserQuestion flow)")
    # Operator-mode family: --print-resume-command / --picker / --cli-setup
    # each route the program to a distinct handler. argparse rejects any
    # combination as a usage error (exit 2) before main() runs, so the
    # conflict surfaces immediately instead of via silent precedence.
    op = p.add_mutually_exclusive_group()
    op.add_argument("--print-resume-command", action="store_true",
                    help="print the cwd + argv that would be exec'd on Enter, "
                         "then exit (no picker, no exec)")
    op.add_argument("--picker", action="store_true",
                    help="explicit picker intent: require the interactive "
                         "picker; error out (instead of silently degrading "
                         "to --list) when not on a TTY")
    op.add_argument("--cli-setup", action="store_true",
                    help="install a `session-monitor` shell alias into your rc "
                         "(~/.zshrc or ~/.bashrc; idempotent), then exit")
    p.add_argument("--skill-usage", action="store_true",
                   help="attach a per-worktree top-skills line (and a "
                        "global top-10 panel) using logs/*.jsonl turn + "
                        "Skill tool_use counts")
    p.add_argument("--skill-days", type=int, default=30,
                   help="window (days) for --skill-usage aggregation "
                        "(default 30; pass 0 to disable)")
    p.add_argument("--filter", metavar="PATTERN", default="",
                   help="substring filter (case-insensitive) across "
                        "session_id, branch, model, source, log_path, "
                        "worktree, status; empty = show all")
    p.add_argument("--dry-run", action="store_true",
                   help="with --cli-setup, print the alias block without "
                        "writing to the rc file")
    return p.parse_args(argv)


def _validate_modes(args) -> list[str]:
    """Return the active data-mode flags, or raise SystemExit(2) when
    more than one is set.

    Data modes (``--list`` / ``--json`` / ``--print-resume-command``)
    each route the program to a distinct handler. The legacy code let
    precedence silently pick the first match (e.g. ``--list --json``
    would print the listing and ignore ``--json``); the explicit
    conflict detector surfaces the misuse so callers can fix the
    invocation. The operator-mode family (``--cli-setup`` / ``--picker``
    / ``--print-resume-command``) is enforced by argparse's
    ``add_mutually_exclusive_group`` and surfaces as a usage error
    before main() runs.

    ``--picker`` is an explicit-intent flag that means "force the
    interactive picker, error out (instead of silently falling back to
    --list) when not on a TTY". It is mutually exclusive with the
    other operator flags, but it also cannot be combined with the data
    modes (``--list`` / ``--json``) -- those routes return before the
    TTY check ever runs, so ``--picker --json`` silently produces JSON
    and never enforces the picker's explicit-intent contract. Reject
    those combinations here so the user sees the conflict instead of
    a silent precedence drop.
    """
    data_modes = ("list", "json", "print_resume_command")
    active = [name for name in data_modes if getattr(args, name)]
    if len(active) > 1:
        flags = ", ".join(f"--{n.replace('_', '-')}" for n in active)
        print(f"[session-monitor] conflicting mode flags: {flags}. "
              f"Pick exactly one.", file=sys.stderr)
        raise SystemExit(2)
    if args.picker and active:
        data_flag = active[0].replace("_", "-")
        print(f"[session-monitor] --picker cannot be combined with "
              f"--{data_flag}; the explicit picker intent would be "
              f"silently dropped because --{data_flag} returns before "
              f"the TTY check.", file=sys.stderr)
        raise SystemExit(2)
    return active


def main(argv=None) -> int:
    from session_monitor import (  # noqa: E402  (deferred to break cycle)
        build_model,
        build_resume,
        discover_repo_root,
        filter_model,
        install_cli_alias,
        pick_session,
        print_json,
        print_plain_listing,
    )

    args = parse_args(argv)

    if args.cli_setup:
        return install_cli_alias(dry_run=args.dry_run)

    repo_root = discover_repo_root()
    logs_dir = Path(args.logs_dir) if args.logs_dir else repo_root / "logs"

    model = build_model(repo_root, logs_dir, args.repo, args.days)

    if args.filter:
        before = sum(len(w.sessions) for w in model)
        model = filter_model(model, args.filter)
        after = sum(len(w.sessions) for w in model)
        if before and not after:
            print(f"[session-monitor] --filter {args.filter!r} matched "
                  f"0 of {before} sessions", file=sys.stderr)

    skill_agg = None
    if args.skill_usage:
        window = None if args.skill_days == 0 else args.skill_days
        skill_agg = skill_usage.aggregate_skill_usage(
            str(logs_dir / '**' / '*.jsonl'), window,
            include_per_cwd=True)

    _validate_modes(args)

    if args.list:
        print_plain_listing(model, logs_dir, skill_usage_agg=skill_agg)
        return 0

    if args.json:
        print_json(model, logs_dir, skill_usage_agg=skill_agg)
        return 0

    if args.print_resume_command:
        first = next((s for w in model for s in w.sessions), None)
        if first is None:
            print("[session-monitor] no sessions to resume")
            return 0
        cwd, argv, warning = build_resume(first.agg, repo_root, first.wt_path)
        if warning:
            print(f"[session-monitor] {warning}", file=sys.stderr)
        print(f"cd {cwd} && {' '.join(argv)}")
        return 0

    total = sum(len(w.sessions) for w in model)
    if total == 0:
        print(f"[session-monitor] no sessions found under {logs_dir}")
        print("  run /dev-kit:log setup && /dev-kit:log on to start capturing.")
        return 0

    if not sys.stdout.isatty() or not sys.stdin.isatty():
        if args.picker:
            # Explicit picker intent refuses the silent --list fallback.
            print("[session-monitor] --picker requires a TTY; "
                  "use --list or --json to preview.", file=sys.stderr)
            return 1
        print("[session-monitor] not a TTY -- run this in a real terminal, "
              "or use --list to preview.", file=sys.stderr)
        print_plain_listing(model, logs_dir)
        return 0

    sel = pick_session(model)
    if sel is None:
        return 0

    cwd, resume_argv, warning = build_resume(sel.agg, repo_root, sel.wt_path)
    if warning:
        print(f"[session-monitor] {warning}", file=sys.stderr)
    try:
        os.chdir(cwd)
        os.execvp(resume_argv[0], resume_argv)
    except FileNotFoundError:
        print(f"[session-monitor] '{resume_argv[0]}' not on PATH. Run manually:\n"
              f"  cd {cwd} && {' '.join(resume_argv)}", file=sys.stderr)
        return 127
    except OSError as exc:
        # Covers ``chdir`` failure (worktree deleted between model build and
        # exec) and any other exec-time OS error.
        print(f"[session-monitor] cannot exec: {exc}. Run manually:\n"
              f"  cd {cwd} && {' '.join(resume_argv)}", file=sys.stderr)
        return 1
    return 0  # unreachable after execvp
