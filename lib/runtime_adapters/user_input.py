"""Runtime-neutral user-input boundary backed by injected handlers."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

PromptCallback = Callable[[str], str]


@runtime_checkable
class PromptAdapter(Protocol):
    """Boundary that collects one answer without invoking a runtime tool."""

    def prompt_user(self, question: str) -> str:
        """Return the answer supplied by the injected runtime boundary."""
        ...


def prompt_user(question: str, boundary: PromptCallback | PromptAdapter) -> str:
    """Ask through an injected callback or prompt adapter."""
    if callable(boundary):
        return boundary(question)
    callback = getattr(boundary, "prompt_user", None)
    if callable(callback):
        return callback(question)
    raise TypeError("prompt boundary must be callable or expose callable prompt_user")


class UserInputAdapter:
    """Concrete prompt boundary that delegates to an optional callback."""

    def __init__(self, prompt_callback: PromptCallback | None = None) -> None:
        self._prompt_callback = prompt_callback

    def prompt_user(self, question: str) -> str:
        if self._prompt_callback is None:
            raise RuntimeError("prompt callback is not configured")
        return self._prompt_callback(question)


__all__ = ["PromptAdapter", "PromptCallback", "UserInputAdapter", "prompt_user"]
