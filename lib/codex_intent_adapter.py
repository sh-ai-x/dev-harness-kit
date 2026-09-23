"""codex_intent_adapter.py — Codex-side adapter for the request classifier (issue #845).

Codex does not expose Claude's ``UserPromptSubmit`` event, so the
classifier cannot be invoked from a hook the same way it is in the
Claude adapter. Instead the Codex orchestrator invokes the
classifier before spawning a subagent — every Codex prompt passes
through here first, even though there is no hook event for it.

This module is the thin client adapter that:

  1. Accepts a raw Codex prompt (the orchestrator's view of the
     user's request, possibly annotated with tool name / project
     context).
  2. Calls ``lib.request_intent.classify_request`` with
     ``client="codex"``.
  3. Emits the same metrics the Claude adapter emits so parity can
     be measured (the ``client_parity`` metric slices on
     ``client``).
  4. Returns the ``Classification`` so the orchestrator can decide
     whether to spawn a subagent or surface a clarifying question.

The adapter has no I/O. It does not create worktrees, write files,
or shell out — those steps live downstream, gated on the
classification's ``next_action`` and on the accepted ``Intent``.

A related note: the Codex runtime that will eventually wire to
this adapter does not yet exist; what this module establishes is
the importable surface that the orchestrator (and its regression
tests) can target.
"""
from __future__ import annotations

from typing import Optional

from intent_metrics import emit
from request_intent import (
    Classification,
    Client,
    classify_request,
)


def classify_codex_request(
    prompt: str,
    *,
    request_id: Optional[str] = None,
) -> Classification:
    """Classify a Codex request and emit parity-relevant metrics.

    Mirrors the Claude-side adapter (see ``lib/request_intent.py``)
    so the ``client_parity`` metric can compare
    ``(prompt, "claude-code")`` vs ``(prompt, "codex")``
    classifications without adapter drift.

    The function returns the same ``Classification`` the bare
    ``classify_request`` would return — there is no client-specific
    override. The wrapper exists so a future change (e.g. a
    Codex-specific stop-word list) has one place to land.
    """
    cls = classify_request(prompt, client="codex", request_id=request_id)
    # Emit parity slice + intent-decision-count metrics. The
    # reducer computes ``client_parity`` by joining two emission
    # streams and comparing fields. Single-side emission is
    # enough; parity is a reducer property.
    emit(
        "client_parity_observed",
        1.0,
        tags={
            "client": cls.client,
            "mode": cls.mode,
            "request_id": cls.request_id,
        },
        unit="count",
    )
    return cls


def classify_request_as(
    prompt: str,
    *,
    client: Client,
    request_id: Optional[str] = None,
) -> Classification:
    """Generic adapter entry point used by both clients.

    The Claude-side hook can call this with ``client="claude-code"``
    so both adapters share the same emit path; that is what makes
    parity measurable. (A future refactor can collapse the Claude
    and Codex adapters if they remain identical.)
    """
    cls = classify_request(prompt, client=client, request_id=request_id)
    emit(
        "client_parity_observed",
        1.0,
        tags={
            "client": cls.client,
            "mode": cls.mode,
            "request_id": cls.request_id,
        },
        unit="count",
    )
    return cls


__all__ = [
    "classify_codex_request",
    "classify_request_as",
]
