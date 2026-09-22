# Runtime Portability



# Runtime Portability



> **Status: ARCHIVAL — substrate deleted in PR-G (commit `6a00288`).**
>
> `lib/runtime_adapters/`, `tests/test_portability.py`, and
> `.github/workflows/test-portability.yml` no longer exist. The opening
> sections below (TL;DR, Neutrality matrix, Common patterns) describe the
> adapter package as if it were live. Treat them as **design history**,
> not as a contract — there is no current cross-runtime adapter to
> import from.
>
> **Today's portable-by-convention contract** (post-retraction):
> runtime detection is `os.environ.get("CLAUDECODE")` /
> `os.environ.get("CODEX_CLI")` (each runtime sets its own signal).
> Any code that wants normalized token/session data must read the
> runtime-native transcript directly (no shared dataclass). The CI
> enforcement section ("CI enforcement (retracted in PR-G…)") further
> down records what was removed.

---

This document is the canonical classification of every public API in
`lib/runtime_adapters/` by **neutrality**: which parts of the contract
are identical across Claude Code and Codex, and which parts require an
adapter shim. It is the Phase 0.9 deliverable (issue #345) and the
single source of truth for "is X portable?" — every later phase refers
back to this matrix.

## TL;DR

```python
from runtime_adapters import (
    RuntimeAdapter,        # Protocol — neutral surface
    TokenLog,              # neutral dataclass
    SessionEvent,          # neutral dataclass
    ClaudeCodeAdapter,     # Claude Code concrete impl
    CodexAdapter,          # Codex concrete impl
)
```

Any code that imports only from the `runtime_adapters` package and
operates through the `RuntimeAdapter` Protocol is portable by
construction. Any code that imports `ClaudeCodeAdapter` /
`CodexAdapter` directly is intentionally pinned to one runtime — that
is allowed (e.g. the `/dev-kit:runtime` skill), but the call site must
opt in via `adapter.is_current()`.

## Neutrality matrix

| API                              | Neutral? | How both adapters implement it                                  |
|----------------------------------|:--------:|-----------------------------------------------------------------|
| `name()`                         | ✅        | Returns the stable runtime id string (`"claude-code"` / `"codex"`). No portability concern. |
| `is_current()`                   | ❌        | Runtime-specific env signals + binary probe. **Not portable** — by design. The Protocol exists *because* each runtime has a distinct self-detection story. |
| `read_token_log(window)`         | ✅        | Both adapters return a `TokenLog` dataclass with the same 5 fields. Field semantics are normalized: Codex `cached_input_tokens` is subtracted from `input_tokens` so `input_tokens` means "fresh, non-cached". |
| `read_session_events(session_id)`| ✅        | Both adapters return `list[SessionEvent]`. Event names are **runtime-native**, not neutral (Claude emits `PreToolUse`, Codex emits `before_tool_use`); see `hook_event_name()` for the mapping. |
| `hook_event_name(neutral_name)`  | ⚠️        | Claude is the identity mapping (Claude hook names ARE the neutral set). Codex maps the canonical neutral set to its own event names and passes unknown names through unchanged. **The neutral set is the contract**; runtime-native names are the implementation detail. |
| `prompt_user(question)`          | ✅        | Both adapters delegate to an injected callback. Same `RuntimeError` ("prompt callback is not configured") when unwired. |
| `workspace_root()`               | ✅        | Both resolve via the same priority chain: explicit `project_root` arg > runtime env signal (`CLAUDE_PROJECT_DIR` / `CODEX_PROJECT_DIR`) > `Path.cwd()`. |
| `install_skill(name, dir)`       | ✅        | Both delegate to an injected installer callback. Same `RuntimeError` when unwired. |

## What "neutral" means in practice

A neutral API has **byte-identical observable behavior** across
adapters for the same input. Concretely:

```python
# Both adapters return identical TokenLog for an identical empty
# workspace_root (the "missing file" case is the canonical neutral
# example — see test_both_adapters_return_same_shape_on_empty_input).
claude = ClaudeCodeAdapter(project_root=tmp).read_token_log("7d")
codex  = CodexAdapter(project_root=tmp).read_token_log("7d")
assert type(claude) is type(codex)            # TokenLog == TokenLog
assert claude.input_tokens == codex.input_tokens  # 0 == 0
```

This is the **cross-runtime equality guarantee** that Phase 1+ code
relies on. A future third adapter (any runtime that can normalize its
token logs into a `TokenLog`) drops in without touching Phase 1+.

## What "not neutral" means in practice

`is_current()` is the only method explicitly **not** portable: it
exists precisely because each runtime has its own self-detection
signature. Two reasons this is the right call:

1. **Callers want to ASK, not assume.** Any code that wants to know
   "is the user actually running Claude Code right now?" must read the
   runtime-specific signal. There is no neutral answer.
2. **The runtime-portability guarantee is about NORMALIZED OUTPUT,
   not DETECTION.** Phase 1+ code reads normalized data through the
   Protocol; it does not (and must not) detect the runtime itself.

`hook_event_name()` is the only method that is *partially* neutral:
the **neutral set** is the contract (a fixed list of canonical event
names), but the **implementation** is per-runtime. Codex passes
unknown neutral names through unchanged so a future neutral event
works in Codex automatically; the cost is that any Codex-specific
event not yet in the neutral set is invisible to Claude Code callers.

## Common patterns

### Pattern 1: "Give me the current adapter"

```python
from runtime_adapters import ClaudeCodeAdapter, CodexAdapter

def current_adapter():
    for cls in (ClaudeCodeAdapter, CodexAdapter):
        if cls().is_current():
            return cls()
    raise RuntimeError("No supported runtime detected")
```

This is the only place in the codebase that may inspect
`is_current()`. Downstream code receives the resulting adapter and
never asks again.

### Pattern 2: "Read normalized token usage"

```python
from runtime_adapters import TokenLog

def last_24h_tokens(adapter) -> TokenLog:
    return adapter.read_token_log("24h")
```

Portable. Identical input → identical output across runtimes.

### Pattern 3: "Map a neutral hook event to its runtime-native name"

```python
def emit(adapter, neutral_name: str) -> None:
    native = adapter.hook_event_name(neutral_name)
    # `native` is what this runtime's hook protocol expects.
```

Portable — the neutral name is the API; the runtime-native name is
the implementation.

## CI enforcement (retracted in PR-G, removed in current branch)

PR-G (commit `6a00288`) deleted the entire Phase 0.9 portability
substrate — `lib/runtime_adapters/` (had zero production callers) and
`tests/test_portability.py`. With no adapter module left to test, the
remaining env-signal check in `.github/workflows/test-portability.yml`
only verified GitHub Actions platform behavior, so this branch deletes
that workflow as well.

The portability contract is now honored only by **convention** in
downstream code: `os.environ.get("CLAUDECODE")` / `os.environ.get("CODEX_CLI")`
for runtime detection, with no shared abstraction. Restore the deleted
substrate (adapter package + 28-test contract suite + CI matrix) before
any future cross-runtime guarantee can be re-asserted.

## Related

- Issue #329 — Phase 0 parent.
- Issue #343 — `__init__.py` exports (deleted with PR-G).
- Issue #344 — `tests/test_portability.py` (deleted with PR-G).
- Issue #345 — CI matrix + this doc.
- Commit `6a00288` — the PR-G retraction.
