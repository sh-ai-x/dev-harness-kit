"""_registry.py — Fixer registry (separate module to avoid circular imports)."""
from __future__ import annotations

_REGISTRY: dict = {}


def register(name: str, instance):
    if name in _REGISTRY:
        return _REGISTRY[name]
    _REGISTRY[name] = instance
    return instance


def list_fixers():
    return sorted(_REGISTRY.keys())


def get_fixer(name: str):
    if name not in _REGISTRY:
        raise KeyError(f"Unknown fixer: {name}. Available: {list_fixers()}")
    return _REGISTRY[name]
