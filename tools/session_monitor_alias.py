"""session_monitor_alias.py -- shell-alias install (``--cli-setup``).

Splits the ``--cli-setup`` / ``--dry-run`` handler out of
``tools/session_monitor.py``. The installer writes a marker-wrapped
managed block into the user's shell rc file (``~/.zshrc`` /
``~/.bashrc`` / ``~/.profile``) so the ``session-monitor`` alias is
always available, idempotent across re-runs, and safe to upgrade by
re-running ``--cli-setup``.

Public surface (re-exported by ``tools/session_monitor.py`` so callers
keep using ``sm.install_cli_alias``, ``sm._alias_block``, etc.):
- ``CLI_ALIAS_NAME``, ``_CLI_BEGIN``, ``_CLI_END``
- ``_shell_rc``, ``_alias_block``, ``_strip_managed_block``, ``_render_rc``
- ``install_cli_alias``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CLI_ALIAS_NAME = "session-monitor"
_CLI_BEGIN = "# >>> dev-harness-kit session-monitor alias >>>"
_CLI_END = "# <<< dev-harness-kit session-monitor alias <<<"


def _shell_rc(env=None) -> Path:
    """Best-effort user rc file for the current login shell: ``~/.zshrc``
    for zsh, ``~/.bashrc`` for bash, else ``~/.profile``."""
    env = env if env is not None else os.environ
    shell = env.get("SHELL", "")
    home = Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".profile"


def _alias_block(script_path: Path, python_exe: str) -> str:
    """The managed rc block (marker-wrapped) defining the alias."""
    return (f"{_CLI_BEGIN}\n"
            f"alias {CLI_ALIAS_NAME}='{python_exe} {script_path}'\n"
            f"{_CLI_END}")


def _strip_managed_block(text: str) -> str:
    """Remove any prior managed alias block plus trailing blank lines so
    re-running ``--cli-setup`` never duplicates or drifts."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.strip() == _CLI_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == _CLI_END:
                skipping = False
            continue
        out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    return "\n".join(out)


def _render_rc(existing: str, block: str) -> str:
    """Pure: rc contents with the managed block appended, replacing any
    prior copy. Idempotent -- feeding its own output back yields the same
    string. Always ends with a single trailing newline."""
    base = _strip_managed_block(existing)
    if base:
        return f"{base}\n\n{block}\n"
    return f"{block}\n"


def install_cli_alias(*, script_path: Path | None = None,
                      python_exe: str | None = None,
                      rc: Path | None = None,
                      dry_run: bool = False) -> int:
    """Install (or refresh) the ``session-monitor`` shell alias in the
    user's rc file. Idempotent via marker-wrapped managed block."""
    script_path = script_path or Path(__file__).resolve().parent / "session_monitor.py"
    python_exe = python_exe or sys.executable or "python3"
    rc = rc or _shell_rc()
    block = _alias_block(script_path, python_exe)

    if dry_run:
        print(f"[session-monitor] would write to {rc}:\n")
        print(block)
        print(f"\n[session-monitor] then activate with:  source {rc}")
        return 0

    existing = rc.read_text() if rc.exists() else ""
    verb = "refreshed" if _CLI_BEGIN in existing else "installed"
    rc.write_text(_render_rc(existing, block))
    print(f"[session-monitor] {verb} '{CLI_ALIAS_NAME}' alias in {rc}")
    print(f"  alias {CLI_ALIAS_NAME}='{python_exe} {script_path}'")
    print(f"[session-monitor] activate now:  source {rc}")
    return 0
