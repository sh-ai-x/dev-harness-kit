"""dual_import.py — package-or-flat import shim.

The `lib/` package ships in two layouts:
  * source repo: `lib/__init__.py` makes `lib` a Python package, so
    intra-package `from .X import Y` resolves inside the package.
  * consumer install: `bin/install.sh` copies individual `*.py` files
    flat into `<target>/lib/` without `__init__.py`, so the package
    form fails and we fall back to top-level `from X import Y`.

Every `lib/ci_*.py` file used to repeat the same try/except dance.
Centralize it here so a future layout change (e.g. always shipping an
`__init__.py`) is one edit instead of N.
"""
from __future__ import annotations

import importlib
import sys
from typing import Iterable, Optional, Tuple, Any


def from_dual(module: str, names: Iterable[str]) -> Tuple[Any, ...]:
    """Try `from lib.{module} import <names>` (package form) then fall back
    to `from {module} import <names>` (flat-file form).

    Returns a tuple with one entry per requested name, in the same order.
    Atomic: if any name is missing in the resolved module, raises
    AttributeError, mirroring `from X import A, B` semantics.

    Uses `importlib.import_module` rather than `__import__(..., level=1)`
    so the helper works whether or not the caller is itself loaded as a
    top-level script (where `__name__` is `__main__` and relative imports
    would fail).
    """
    names = list(names)
    pkg_rel = f"lib.{module}"
    try:
        mod = importlib.import_module(pkg_rel)
    except ImportError:
        # Flat-file consumer install: `lib/` on sys.path, no __init__.py.
        if module in sys.modules:
            mod = sys.modules[module]
        else:
            mod = importlib.import_module(module)
    return tuple(getattr(mod, name) for name in names)


def from_dual_optional(module: str, names: Iterable[str]) -> Tuple[Optional[Any], ...]:
    """Like `from_dual` but returns `None` per name if both attempts fail.

    Use for optional siblings (e.g. `ci_doctor` importing
    `diff_ci_install` from `ci_update`, which is not installed in the
    source-repo checkout) so the consumer install can degrade to
    `None` rather than crashing the module.
    """
    names = list(names)
    try:
        return from_dual(module, names)
    except ImportError:
        return tuple(None for _ in names)
