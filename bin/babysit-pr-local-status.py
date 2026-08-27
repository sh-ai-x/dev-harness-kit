#!/usr/bin/env python3
"""Print a single ANSI-colored line summarizing the active PR's gate state.

Read-only. Designed to be invoked from:
- Claude Code's statusLine (via the existing ~/.claude/statusline-command.sh
  wrapper that calls `bin/dev-kit-hooks-status.py`, which in turn calls
  this script when a pr_gate key is requested).
- Codex's [tui.status_line] command (config.toml snippet documented in
  skills/babysit-pr-local/SKILL.md).
- The babysit-pr-local iteration tail (one line appended per loop).
- An ad-hoc terminal: `python3 bin/babysit-pr-local-status.py`.

Output: one line, <= 120 chars, ANSI-colored, ends with newline.
Exit code: always 0. A broken status line is worse than a degraded one
(no `?` glyphs + exit 0 on any failure).

Glyph mapping (per gate):
    ✓  green  pass / approved
    ✗  red    fail / blocked / changes_requested
    ·  yellow pending (live, not yet ghost)
    ?  dim    unknown / gh call failed / parse error

The script reads no `gh` calls with a timeout > 3.5s. Reads no env from
the parent shell except NO_COLOR (per no-color.org). The .dev-kit
state files are read but never written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# ANSI escape codes. Respect NO_COLOR (https://no-color.org/) and a
# non-tty stdout (CI logs, file redirect) -- always emit plain text
# when neither color is requested. Evaluated per call so tests can
# flip NO_COLOR / BABYSIT_STATUS_NO_COLOR at runtime without re-import.
def _use_color() -> bool:
    return (
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("BABYSIT_STATUS_NO_COLOR") is None
    )


def _c(code: str, text: str) -> str:
    """Wrap `text` in an ANSI SGR escape; pass-through if disabled."""
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


# --- subprocess wrappers -----------------------------------------------------


def _run(cmd: list[str], cwd: Path, timeout_s: float = 3.5) -> tuple[int, str, str]:
    """Run `cmd` with a hard timeout. Return (rc, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return (124, "", "")
    return (proc.returncode, proc.stdout, proc.stderr)


def _gh(args: list[str], cwd: Path, timeout_s: float = 3.5) -> str:
    """Run `gh <args>` with a 3.5s timeout. Return stdout, or "" on any failure.

    3.5s is the budget observed for `gh pr view --comments --json comments`
    on cold caches (~1.5s typical, occasionally 2-3s). The script's
    fail-soft contract still holds: a slow `gh` degrades to `?` glyphs,
    never a blank line.
    """
    rc, out, _ = _run(["gh", *args], cwd=cwd, timeout_s=timeout_s)
    return out if rc == 0 else ""


# --- state readers -----------------------------------------------------------


def _current_branch(cwd: Path) -> str:
    rc, out, _ = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, timeout_s=1.0)
    return out.strip() if rc == 0 else ""


def _pr_number(branch: str, cwd: Path) -> str:
    if not branch:
        return ""
    # `gh pr view --json number -q .number` returns the PR number for the
    # current branch's head, or empty if no PR is open. The -q filter is
    # a server-side JSON path extract so we never parse a full PR object.
    return _gh(["pr", "view", "--json", "number", "-q", ".number"], cwd=cwd).strip()


def _audit_comment(pr_number: str, cwd: Path) -> dict[str, str]:
    """Parse the latest `<!-- dev-kit-verdict-audit -->` PR comment.

    Returns a dict with keys: verdict, review, security, maintenance,
    provider, source. Each value defaults to "" on parse failure.
    The PR-comment body shape is owned by `lib/maintenance_gate.py`:
    line 1 is the parseable quartet, all extras sorted byte-stable.
    """
    out = _gh(
        ["pr", "view", pr_number, "--comments", "--json", "comments", "-q", ".comments"],
        cwd=cwd,
    )
    if not out:
        return {}
    try:
        comments = json.loads(out)
    except json.JSONDecodeError:
        return {}
    # `gh pr view --comments --json comments` returns comments newest-first
    # (verified against `gh` 2.x); iterate and return the first audit hit.
    for c in comments or []:
        body = c.get("body") if isinstance(c, dict) else None
        if not isinstance(body, str) or "dev-kit-verdict-audit" not in body:
            continue
        parsed = _parse_audit_quartet(body)
        if parsed:
            return parsed
    return {}


def _parse_audit_quartet(body: str) -> dict[str, str]:
    """Pull `key=val` pairs out of the audit-comment line 1.

    The line begins with `<!-- dev-kit-verdict-audit -->` and continues
    with `key=value` pairs separated by spaces. Values may themselves
    contain spaces (e.g. `verdict=Changes Requested`), so we cannot use
    a naive split(): we find every `known_key=` position and slice the
    substring between consecutive positions.

    Known keys are sourced from the format_audit() contract in
    lib/maintenance_gate.py. Missing keys default to absent (caller
    treats empty as "no audit yet").
    """
    KNOWN = ("verdict", "review", "security", "maintenance", "provider", "source", "head_sha")
    out: dict[str, str] = {}
    # Drop the leading HTML sentinel so we only parse the key=value
    # payload that follows.
    sentinel_end = body.find("-->")
    payload = body[sentinel_end + 3:].strip() if sentinel_end >= 0 else body.strip()
    if not payload:
        return out

    # Find every position of a known key followed by '=' (word-boundary
    # so 'review' doesn't match inside 'reviewed').
    key_positions: list[tuple[int, str]] = []
    for k in KNOWN:
        idx = 0
        needle = f"{k}="
        while True:
            pos = payload.find(needle, idx)
            if pos < 0:
                break
            # Word boundary: must be at start, after whitespace, or
            # after another key boundary char. Reject matches inside a
            # longer identifier (e.g. 'reviewer=' must not match
            # 'review=').
            if pos == 0 or payload[pos - 1].isspace():
                key_positions.append((pos, k))
            idx = pos + 1
    key_positions.sort()

    for i, (start, key) in enumerate(key_positions):
        val_start = start + len(key) + 1  # skip "key="
        val_end = key_positions[i + 1][0] if i + 1 < len(key_positions) else len(payload)
        val = payload[val_start:val_end].strip()
        # First match wins (later duplicates are ignored -- a body
        # legitimately has each key once).
        if key not in out:
            out[key] = val
    return out


def _gh_checks(pr_number: str, cwd: Path) -> list[dict]:
    """Run `gh pr checks --json name,state,bucket`; return list of dicts.

    Empty list on any failure. The 2s timeout keeps the status surface
    snappy: if `gh` is slow, we degrade to `?` glyphs, not a blank line.

    Note: we use `bucket` rather than `conclusion` because the installed
    `gh` CLI emits `bucket` (categorized as pass/fail/pending/skipping)
    rather than the raw `conclusion` string. The bucket field already
    encapsulates the same categorization `lib/pr_verify.py` uses for
    the deterministic PR verifier (PASS_BUCKETS = {pass, skipping}).
    """
    out = _gh(
        [
            "pr", "checks", pr_number,
            "--json", "name,state,bucket",
            "-q", ".[]",
        ],
        cwd=cwd,
    )
    if not out.strip():
        return []
    parsed: list = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
    return parsed


def _bucket_checks(checks: list[dict]) -> dict[str, int]:
    """Reduce a `gh pr checks` listing to {pass, fail, pending, ghost} counts.

    The installed `gh` CLI provides a `bucket` field that is already
    the categorized version of the raw `state` (values: pass, fail,
    pending, skipping, cancel). We use it directly so the script
    doesn't have to mirror `lib/babysit_pr_reliability.classify_check`'s
    APPROVED/FAILING conclusion vocab.

    `skipping` counts as `pass` (matches `lib/pr_verify.PASS_BUCKETS`).
    `cancel` / anything unmapped counts as `fail` (matches the
    verifier's fail-closed policy outside its allow-list).
    """
    PASS_BUCKETS = {"pass", "skipping"}
    FAIL_BUCKETS = {"fail", "cancel"}
    out = {"pass": 0, "fail": 0, "pending": 0, "ghost": 0}
    for c in checks:
        if not isinstance(c, dict):
            continue
        bucket = (c.get("bucket") or "").lower()
        if bucket in PASS_BUCKETS:
            out["pass"] += 1
        elif bucket in FAIL_BUCKETS:
            out["fail"] += 1
        elif bucket == "pending":
            out["pending"] += 1
        else:
            # Unknown bucket (should not happen with current `gh`):
            # fail-closed to `fail` so the operator sees a red marker
            # and investigates. Matches the verifier's policy of
            # refusing to approve anything outside its allow-list.
            out["fail"] += 1
    return out


def _lock_body(cwd: Path) -> str:
    p = cwd / ".dev-kit" / "babysit.lock"
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _iter_from_log(cwd: Path) -> str:
    """Pull `iter=<n>` from the last line of `.dev-kit/babysit.log` (if any)."""
    p = cwd / ".dev-kit" / "babysit.log"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    last = text.rstrip().splitlines()[-1] if text.rstrip() else ""
    for tok in last.split():
        if tok.startswith("iter="):
            return tok.split("=", 1)[1]
    return ""


# --- glyph + line composition ------------------------------------------------


def _gate_glyph(verdict: str) -> str:
    """Map a single per-gate verdict to ✓/✗/·/? with color."""
    if not verdict:
        return dim("?")
    low = verdict.lower()
    if low in {"approve", "approved"}:
        return green("✓")
    if low in {"blocked"}:
        return red("✗")
    if low in {"changes_requested", "changes requested", "changesrequested"}:
        return yellow("✗")
    if low in {"pending", "running"}:
        return yellow("·")
    return dim("?")


def _ci_summary(buckets: dict[str, int]) -> str:
    """Compose a compact `N✓ N✗ (failing-name)` substring. ≤ 40 chars."""
    parts: list[str] = []
    if buckets.get("pass"):
        parts.append(green(f"{buckets['pass']}✓"))
    if buckets.get("fail"):
        parts.append(red(f"{buckets['fail']}✗"))
    if buckets.get("pending"):
        parts.append(yellow(f"{buckets['pending']}·"))
    if buckets.get("ghost"):
        parts.append(dim(f"{buckets['ghost']}?"))
    if not parts:
        return dim("·")
    return " ".join(parts)


# --- entry point -------------------------------------------------------------


def render(cwd: Path) -> str:
    """Build the one-line summary for `cwd` (a git checkout). Never raises."""
    branch = _current_branch(cwd)
    pr = _pr_number(branch, cwd)

    if not pr:
        return dim(f"no PR on {branch or 'detached'}")

    audit = _audit_comment(pr, cwd)
    checks = _gh_checks(pr, cwd)
    buckets = _bucket_checks(checks)
    lock = _lock_body(cwd)
    iter_n = _iter_from_log(cwd)

    review = _gate_glyph(audit.get("review", ""))
    security = _gate_glyph(audit.get("security", ""))
    maint = _gate_glyph(audit.get("maintenance", ""))
    ci = _ci_summary(buckets)

    head = bold(f"PR#{pr}")
    if branch:
        head = f"{head} {dim(branch)}"
    gates = f"review={review} sec={security} maint={maint}"
    line = f"{head} │ {gates} │ CI {ci}"

    if lock:
        line += f" │ {yellow('babysit')}"
        if iter_n:
            line += f" {dim(f'iter={iter_n}')}"
    return line


def main(argv: list[str]) -> int:
    # Single positional arg optional: a project root. Default to cwd.
    cwd = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        line = render(cwd)
    except Exception:  # noqa: BLE001 -- status surface must never raise
        line = dim("?")
    # Truncate to 200 chars max so a slow `gh` response can't blow up
    # the status bar; the line is structured for at-a-glance, not detail.
    print(line[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
