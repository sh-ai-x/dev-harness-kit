#!/usr/bin/env bash
# log-setup.sh — copy tools/save_log.py + create logs/ scaffold in target project.
#
# Run once per project before /log on so that the installed hook command
# (`python3 ${CLAUDE_PROJECT_DIR}/tools/save_log.py`) has its script to call.
# Or run with --global once to capture every project on the machine.
#
# Idempotent: re-running updates save_log.py to the current source version
# (use --force to overwrite an existing copy if the SHA differs) and creates
# the logs/ tree if missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--target DIR | --global] [--force] [--all-worktrees]

Creates the target project's logging scaffold:
  <target>/tools/save_log.py     — copied from \$LOGHOOKS_DIR (or ~/dev/loghooks)
  <target>/logs/.gitkeep         — keeps the logs/ tree in version control
  <target>/logs/claude-code/     — Claude Code transcripts land here
  <target>/logs/codex/           — Codex transcripts land here

Idempotent: re-running refreshes save_log.py to the current source version.
--force overwrites even if the local copy SHA matches.
--all-worktrees also runs setup + hook install for every existing sibling
                     worktree under <target>/.worktrees/*/. Legacy
                     <target>/.claude/worktrees/*/ and
                     <target>/.codex/worktrees/*/ roots are also scanned for
                     backwards compatibility. Use this once after --target
                     on the main checkout to close the per-worktree gap.
--global          install to \$HOME/.claude/ instead of a per-project
                  target. Recommended for multi-project / multi-worktree
                  users — a single setup captures every session anywhere
                  on the machine. Use this with `log-on.sh --global` and
                  `log-off.sh --global`. Mutually exclusive with --target
                  and --all-worktrees.

Env:
  LOGHOOKS_DIR   source repo (default: \$HOME/dev/loghooks)
  TARGET_DIR     target project (default: \$PWD)
EOF
}

FORCE=0
ALL_WORKTREES=0
GLOBAL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET_DIR="$2"; shift 2 ;;
        --force)  FORCE=1; shift ;;
        --all-worktrees) ALL_WORKTREES=1; shift ;;
        --global) GLOBAL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown arg: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ "$GLOBAL" -eq 1 ]]; then
    if [[ -n "${TARGET_DIR:-}" || "$ALL_WORKTREES" -eq 1 ]]; then
        echo "ERROR: --global is mutually exclusive with --target and --all-worktrees" >&2
        exit 1
    fi
    TARGET_DIR="$(resolve_global_dir)"
    if [[ ! -d "$TARGET_DIR" ]]; then
        mkdir -p "$TARGET_DIR"
    fi
    echo "Global install: $TARGET_DIR"
fi

require_jq
LOGHOOKS_DIR="$(resolve_loghooks_dir)"
TARGET_DIR="$(resolve_target_dir)"

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "ERROR: target dir does not exist: $TARGET_DIR" >&2
    exit 4
fi

SRC_PY="$LOGHOOKS_DIR/tools/save_log.py"
# --global install: save_log.py lives directly at $HOME/.claude/save_log.py
# (no tools/ subdir) so the global hook can reference a single canonical
# path that is stable across projects and machines.
if [[ "$GLOBAL" -eq 1 ]]; then
    DST_PY="$TARGET_DIR/save_log.py"
else
    DST_PY="$TARGET_DIR/tools/save_log.py"
fi

if [[ ! -f "$SRC_PY" ]]; then
    echo "ERROR: source script missing: $SRC_PY" >&2
    exit 2
fi

if [[ "$GLOBAL" -ne 1 ]]; then
    mkdir -p "$TARGET_DIR/tools"
fi

# Portable SHA-256: prefer coreutils sha256sum, fall back to BSD shasum.
# Alpine / distroless / many debian-slim images ship only sha256sum;
# macOS / FreeBSD ship only shasum.
sha256_cmd() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256
    else echo "ERROR: no sha256 tool installed (need sha256sum or shasum)" >&2; return 1
    fi
}

LOCAL_SHA=""
if [[ -f "$DST_PY" ]]; then
    LOCAL_SHA="$(sha256_cmd < "$DST_PY" | awk '{print $1}')"
fi
SRC_SHA="$(sha256_cmd < "$SRC_PY" | awk '{print $1}')"

# Guard against clobbering a target file that already matches its own
# project's git HEAD. LOGHOOKS_DIR is an independently-versioned source
# repo that can drift behind/ahead of what a project has already
# committed to tools/save_log.py. Comparing only against LOGHOOKS_DIR's
# SHA meant a fresh worktree's already-correct, git-tracked copy could
# be silently overwritten the moment the two diverged. --force bypasses
# this guard explicitly (same as it bypasses the source-SHA check).
HEAD_SHA=""
if [[ "$GLOBAL" -ne 1 && "$FORCE" -eq 0 && -f "$DST_PY" ]]; then
    if [[ "$(git -C "$TARGET_DIR" rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]]; then
        REL_PY="${DST_PY#"$TARGET_DIR"/}"
        HEAD_SHA="$(git -C "$TARGET_DIR" show "HEAD:$REL_PY" 2>/dev/null \
                     | sha256_cmd | awk '{print $1}' || true)"
    fi
fi

if [[ -n "$LOCAL_SHA" && "$LOCAL_SHA" == "$SRC_SHA" && "$FORCE" -eq 0 ]]; then
    echo "OK: $DST_PY already up to date (sha matches)"
elif [[ -n "$LOCAL_SHA" && -n "$HEAD_SHA" && "$LOCAL_SHA" == "$HEAD_SHA" ]]; then
    echo "OK: $DST_PY matches project git HEAD (leaving as-is; use --force to sync from \$LOGHOOKS_DIR anyway)"
else
    if [[ -n "$LOCAL_SHA" ]]; then
        echo "Updating $DST_PY (sha: ${LOCAL_SHA:0:8} -> ${SRC_SHA:0:8})"
    else
        echo "Creating $DST_PY"
    fi
    cp -p "$SRC_PY" "$DST_PY"
    chmod 0755 "$DST_PY"
fi

# Scaffold logs/ tree (only meaningful for per-project installs; global
# install still creates a logs/ in $HOME/.claude/ for symmetry but it's
# a no-op for capture since save_log.py redirects to <main_repo>/logs/).
mkdir -p "$TARGET_DIR/logs/claude-code" "$TARGET_DIR/logs/codex"

# Write a .gitkeep so the empty subdirs survive `git status`.
GITKEEP="$TARGET_DIR/logs/.gitkeep"
if [[ ! -f "$GITKEEP" ]]; then
    printf '# conversation transcripts land here. .jsonl files are gitignored.\n' >"$GITKEEP"
fi

# Add a logs/.gitignore that ignores the captured transcripts but keeps
# the directory. Idempotent: skipped if a logs/.gitignore already exists.
LOG_GITIGNORE="$TARGET_DIR/logs/.gitignore"
if [[ ! -f "$LOG_GITIGNORE" ]]; then
    cat >"$LOG_GITIGNORE" <<'GI'
# ignore captured transcripts
*.jsonl

# keep these subdirs present
!.gitkeep
!claude-code/
!codex/
GI
fi

echo
echo "Setup complete for: $TARGET_DIR"
if [[ "$GLOBAL" -eq 1 ]]; then
    echo "  scripts: save_log.py"
    echo
    echo "Next: run /dev-kit:log on --global   to enable the global hooks."
    echo "      (or /dev-kit:log on --target <dir> for a per-project install)"
else
    echo "  scripts: tools/save_log.py"
    echo "  logs:    logs/{claude-code,codex}/"
    echo
    echo "Next: run /dev-kit:log on   to enable the hooks."
fi

if [[ "$ALL_WORKTREES" -eq 1 ]]; then
    # .worktrees is the shared canonical root. Keep the two historical roots
    # discoverable so upgrading does not strand existing worktrees.
    WT_ROOTS=(
        "$TARGET_DIR/.worktrees"
        "$TARGET_DIR/.claude/worktrees"
        "$TARGET_DIR/.codex/worktrees"
    )
    existing_root=0
    for root in "${WT_ROOTS[@]}"; do
        if [[ -d "$root" ]]; then
            existing_root=1
            break
        fi
    done
    if [[ "$existing_root" -eq 0 ]]; then
        echo
        echo "--all-worktrees: no canonical or legacy worktree roots; nothing to backfill."
        exit 0
    fi
    ok=0
    failed=0
    for root in "${WT_ROOTS[@]}"; do
        [[ -d "$root" ]] || continue
        for wt in "$root"/*/; do
            [[ -d "$wt" ]] || continue
            name="$(basename "$wt")"
            echo
            echo "==> backfilling worktree: $name (root: $root)"
            if ! TARGET_DIR="$wt" FORCE="$FORCE" "$SCRIPT_DIR/log-setup.sh" --target "$wt" >/dev/null; then
                echo "WARN: setup failed for $wt" >&2
                failed=$((failed + 1))
                continue
            fi
            if TARGET_DIR="$wt" "$SCRIPT_DIR/log-on.sh" --target "$wt" --claude-only; then
                ok=$((ok + 1))
            else
                echo "WARN: hook install failed for $wt" >&2
                failed=$((failed + 1))
            fi
        done
    done
    echo
    echo "--all-worktrees summary: ok=$ok failed=$failed (roots: ${WT_ROOTS[*]})"
fi
