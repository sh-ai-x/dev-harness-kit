"""Backwards-compatibility shim — DO NOT import from this path in new code.

`skills.ralph.lib.ralph_state` and `skills.ralph.lib.ralph_chain` were
promoted to top-level `lib/ralph_state` and `lib/ralph_chain` in the
2026-09-23 inspect-pass4 refactor (finding a1, see inspect-report.md).
lib/ is the canonical SSOT surface for shared Python helpers; the
skills/ namespace was a parallel hierarchy that forced every
non-ralph consumer (lib/ralph_controller, hooks/stop-verify.sh,
hooks/trace-session-end.sh) to reach into a skill's private lib.

This shim re-exports the symbol surface of the two promoted modules
so legacy call sites of the form
`from skills.ralph.lib import RalphState` keep working, and emits a
DeprecationWarning on import so any lingering use is flagged at
runtime. New code MUST use the top-level path:

    from lib.ralph_state import RalphState
    from lib.ralph_chain import ...   # or `python3 -m lib.ralph_chain`

Note: legacy code that did `from skills.ralph.lib import ralph_state`
(the module form, not the symbol form) was updated as part of the
promotion — see the audit commit. The module-attribute shim is no
longer needed because the form `from skills.ralph.lib import
ralph_state` was only used by `tests/test_ralph_controller.py`,
which now reads `from lib import ralph_state`. No external caller
remains.

This shim will be removed in a follow-up release once a `grep -r
'skills\\.ralph\\.lib'` audit confirms zero remaining call sites
(allowing for the deprecation warning emitted here).

Why sys.path is mutated: this shim lives at
`skills/ralph/lib/__init__.py`, so a bare `from lib.ralph_state`
import would resolve to the LOCAL `lib` package
(`skills.ralph.lib`) and recurse into this shim itself. To reach the
canonical top-level `lib/` package (4 dirs up), we prepend it to
sys.path once at import time before the re-export.
"""
# ruff: noqa: E402  # intentional: warnings + sys.path come before imports
from __future__ import annotations

import sys as _sys
import warnings as _warnings
from pathlib import Path as _Path

_warnings.warn(
    "skills.ralph.lib is deprecated; import from lib.ralph_state / lib.ralph_chain instead.",
    DeprecationWarning,
    stacklevel=2,
)

# _Prepend the project-root-anchored lib/ to sys.path so the
# `from lib.ralph_state` imports below resolve to the canonical
# top-level lib/ (4 dirs up from this shim), NOT the local
# `skills.ralph.lib` package we are currently inside.
_TOPLEVEL_LIB = _Path(__file__).resolve().parent.parent.parent.parent / "lib"
if str(_TOPLEVEL_LIB) not in _sys.path:
    _sys.path.insert(0, str(_TOPLEVEL_LIB))

from lib.ralph_chain import *  # noqa: E402, F401, F403
from lib.ralph_state import *  # noqa: E402, F401, F403
from lib.ralph_state import RalphState  # noqa: E402, F401
