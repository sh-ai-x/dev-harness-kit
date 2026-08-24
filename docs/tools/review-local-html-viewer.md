# `bin/review-local-server.py` — localhost HTML live-streaming viewer

> Complements `docs/local-ci.md` (the local mirror of `review.yml` +
> `maintenance.yml`). Where `local-ci.md` documents the **terminal**
> path (`bin/review-local.sh --pr N`), this page documents the
> **HTML viewer** path (`bin/review-local-server.py --port 8765`).

## Why this exists

`bin/review-local.sh --pr N` streams verdict lines to stdout, one per
gate (`review=` / `security=` / `maintenance=`). On a fast run that is
~12 lines total; on a slow run it can sit silent for 30–60 seconds
between gates while a single LLM judge thinks. Watching a terminal
for "did anything happen?" is the wrong UX for a multi-gate pipeline.

`bin/review-local-server.py` replaces that with a localhost HTML page
that:

- shows the three gates (review / security / maintenance) as dots
  that flip `gray → running → approved / changes / blocked` in real
  time, and
- streams the full `bin/review-local.sh` stdout into a `<pre>` region
  as it is produced (Server-Sent Events; EventSource auto-reconnects
  on transient drops).

The terminal path stays the default for SSH / headless use cases
(`bin/review-local.sh --pr N` works without the server).

## Quick start

```bash
# Terminal 1 — start the server on 127.0.0.1:8765
bin/review-local-server.py --port 8765

# Browser — open the auto-redirected URL
open http://127.0.0.1:8765           # → 302 → /pr/<your-branch-PR>

# Or pin a specific PR:
open http://127.0.0.1:8765/pr/725
```

The page is single static HTML (`tools/review-local-preview.html`),
no JS framework, no build step. Opening it on a phone (same network)
also works — the server binds only to `127.0.0.1`, so you would
need an SSH tunnel for that (`ssh -L 8765:127.0.0.1:8765 …`).

## Routes

| Method | Path | Behavior |
|--------|------|----------|
| `GET`  | `/` | 302 → `/pr/<current-branch-PR>` (resolved via `gh`) |
| `GET`  | `/pr/<N>` | Serves `tools/review-local-preview.html` (no PR lookup; the page does it client-side) |
| `GET`  | `/pr/<N>/stream` | SSE: stdout of `bin/review-local.sh --pr N` line-by-line as JSON `data:` frames; EventSource auto-reconnects on transient drops; closing the tab kills the subprocess (SIGTERM → reaps) |
| `GET`  | `/healthz` | JSON: `{status: ok, active_streams: N}` for liveness probes |
| any    | anything else | 404 |

## Safety properties

- **127.0.0.1 only.** The server refuses to bind to `0.0.0.0` even
  when `--host` is passed. There is no auth; the boundary is the
  loopback. If you need LAN/remote access, terminate at an SSH
  tunnel, not a reverse proxy.
- **PR number sanitized to digits** before argv splice. The handler
  splits `/pr/<N>/...`, strips non-digits, and refuses non-positive
  values — so `/pr/123;rm -rf /` becomes `123` and `/pr/-1` is 400.
- **4-stream concurrency cap.** A 5th concurrent `/stream` request
  returns 503 (the handler replies `{"error":"busy"}` and closes).
  This caps accidental fork-bombs; raise with `--max-streams N`
  if a CI pipeline legitimately needs more.
- **Subprocess lifetime = SSE connection lifetime.** When the
  browser tab closes, the EventSource stream ends, the handler
  closes the subprocess pipe, the subprocess receives SIGTERM, and
  the slot frees. No zombies, no detached background jobs.

## CLI flags

```
bin/review-local-server.py [--port N] [--host 127.0.0.1] [--max-streams N]
                            [--bin-path PATH] [--html-path PATH]
                            [--repo PATH]
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--port` | `8765` | TCP port |
| `--host` | `127.0.0.1` | Bind address (the default is non-negotiable for the safety property above) |
| `--max-streams` | `4` | Concurrency cap |
| `--bin-path` | auto-detected | Override path to `bin/review-local.sh` (useful for plugin-cache installs) |
| `--html-path` | auto-detected | Override path to `tools/review-local-preview.html` |
| `--repo` | cwd | Repo root used for `gh pr view` lookups |

## Verification (Iron Law L3)

```
python3 -m pytest tests/test_review_local_server.py -v
=> 7 tests passed in ~10s (exit 0)

python3 -m pytest tests/test_review_local_sh.py::TestCwdIndependence -v
=> 1 test passed in ~1s (exit 0)

bash -n bin/review-local-server.py && bash -n bin/review-local.sh
=> syntax OK (exit 0)
```

## Files

| Path | Role |
|------|------|
| `bin/review-local-server.py` | stdlib `http.server` (no Flask dep) — routes + SSE handler + subprocess supervision |
| `bin/review-local.sh` | unchanged contract; new `--help` short-circuit moved before the manifest check so the cwd-independence test path is reachable |
| `tools/review-local-preview.html` | single static page; vanilla JS only; regex-driven gate-indicator dots |
| `tests/test_review_local_server.py` | 7 hermetic tests (healthz, html serve, SSE frame sequence, concurrency cap, unknown route, etc.) |
| `tests/test_review_local_sh.py` | cwd-independence regression guard for `bin/review-local.sh` |

## Related

- `docs/local-ci.md` — the local-mirror CI orchestration overview.
- `docs/CODEBASE-MAP.md` — `bin/` inventory (this script is listed under "CI mirrors").
- `bin/ci-claude-p.sh` — sibling used as reference for the SSE framing design.
- Issue #619 D2 — cwd-independence regression that motivated the `--help` short-circuit.