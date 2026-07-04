"""fixers/__init__.py — Registry entry point."""
from __future__ import annotations

from .base import FixerABC, Issue  # noqa: F401
from ._registry import register, list_fixers, get_fixer  # noqa: F401

# Trigger registration by importing each fixer module
from . import (  # noqa: F401
    hooks, iron_law, bootstrap, plan, build,
    review, security, audit, a2a,
)
