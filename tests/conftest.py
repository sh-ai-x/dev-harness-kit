"""tests/conftest.py — emit one ``contract.test`` event per pytest run.

This conftest hooks ``pytest_sessionfinish`` and appends one structured
event to ``.dev-kit/trace/events.jsonl`` so the harness stability
submetric's ``contract_test_pass_rate`` signal is wired automatically.
Without this hook, every worktree reports ``INSUFFICIENT_EVIDENCE`` for
the contract pass rate, which masks real regressions.

The filename matters: pytest auto-registers hook implementations only
from files named exactly ``conftest.py``. ``pytest.ini`` here is
``testpaths``-only (no ``addopts``, no ``-p``) and the repo defines no
``pytest_plugins``, so naming this module anything else makes the hook
dead code that never fires. See ``tests/test_conftest_contract.py``.

Always-on with opt-out: this hook emits ``contract.test`` on every
``pytest`` invocation (local dev runs, CI runs, ``worktree-janitor``
cron — any time pytest finishes a session). Set
``DEV_KIT_TRACE_LOCAL=0`` to silence on a per-invocation basis (e.g.
``DEV_KIT_TRACE_LOCAL=0 pytest ...``). The CI-gating previous
revisions attempted (only fire when ``CI=true``) was
fundamentally broken: CI runs use ephemeral filesystems, so the
emitted events vanished before any local reducer could read them.
Emitting on every run is the only design that actually wires the
metric.

Best-effort: the hook runs in a try/except so a missing ``lib`` module
or a non-git root never breaks the test run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Env-var whitelist for the telemetry subprocess. Anything not on this list
# — secrets (ANTHROPIC_API_KEY, GH_TOKEN, OPENAI_API_KEY, AWS_*, …),
# ephemeral CI tokens, or arbitrary caller vars — is dropped before the
# `python -m lib.trace_log append-event` subprocess inherits our env. The
# append_event path's evidence_ref ends up in `.dev-kit/trace/events.jsonl`,
# which is committed to the repo and shipped with PR artifacts; a leaked
# secret there is a high-severity A02-2 / A03-1 hit.
_TELEMETRY_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
    "PYTHONPATH", "PYTHONHOME",
    "CI", "GITHUB_ACTIONS", "GITHUB_WORKSPACE", "GITHUB_REPOSITORY",
    "RUNNER_TEMP", "RUNNER_OS", "RUNNER_ARCH",
    "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_COLORS",
})


def _safe_env_for_telemetry() -> dict:
    """Build an env for the telemetry subprocess with secrets stripped."""
    env = {
        k: v for k, v in os.environ.items()
        if k in _TELEMETRY_SAFE_ENV_KEYS or k.startswith("DEV_KIT_")
    }
    env.setdefault("DEV_KIT_AGENT", "pytest")
    return env


# Keys that, if set in the test runner's env, would leak into a
# subprocess invocation of a hook or binary and skew the policy
# resolution / API-key fallback the test is trying to pin. Strip
# these from any subprocess env before invoking a hook or wrapper
# that reads `DEV_KIT_GUARDS*` / `ANTHROPIC_*` itself. Tests that
# intentionally set a specific value should set it explicitly on
# top of the cleaned env.
_HARNESS_FREE_SKIP_KEYS = frozenset({
    "DEV_KIT_GUARDS", "DEV_KIT_GUARDS_SOURCE", "DEV_KIT_GUARD_ROOT",
}) | {k for k in os.environ if k.startswith("ANTHROPIC_")}


def harness_free_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `os.environ` with harness-controlled keys
    (`DEV_KIT_GUARDS*`, `ANTHROPIC_*`) removed. Optional `extra` is
    merged on top so callers can still pin specific values.
    """
    env = {k: v for k, v in os.environ.items() if k not in _HARNESS_FREE_SKIP_KEYS}
    if extra:
        env.update(extra)
    return env


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ANN001
    """Append one contract.test event after a pytest session ends.

    Emits on every invocation by default; opt out with
    ``DEV_KIT_TRACE_LOCAL=0`` in the environment.
    """
    if os.environ.get("DEV_KIT_TRACE_LOCAL") == "0":
        # Per-invocation opt-out (e.g. CI runs that want to keep the
        # trace log clean for downstream artifacts).
        return
    try:
        cwd = Path(os.getcwd())
        outcome = "passed" if exitstatus == 0 else "failed"
        run_id = os.environ.get("DEV_KIT_RUN_ID") or f"pytest-{os.getpid()}"
        python = sys.executable or shutil.which("python3") or "python3"
        env = _safe_env_for_telemetry()
        cmd = [
            python, "-m", "lib.trace_log", "append-event",
            "--type", "contract.test",
            "--subject-id", "harness-contract",
            "--run-id", run_id,
            "--workflow-id", "contract",
            "--stage", "contract",
            "--outcome", outcome,
            "--root", str(cwd),
        ]
        # Best-effort: never let telemetry break the test run.
        subprocess.run(cmd, env=env, cwd=str(cwd), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:
        # Telemetry is best-effort — swallow everything.
        pass
