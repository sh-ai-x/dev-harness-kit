# `.githooks/pre-commit` — staged-content lint gate

**Language:** English

This document is the SSOT for what the local commit-time gate does. The
script (`.githooks/pre-commit`) is intentionally short and may grow to a
thin Python shim, but its behavior contract lives here.

## Activation

```bash
git config core.hooksPath .githooks
```

Ruff must be installed and on `$PATH`. If it is not, the hook fails
closed with a `::error::` line telling the user how to install it
(`brew install ruff` / `apt install ruff`).

## What it checks, in order

1. **Conflict-marker scan** — `git grep --cached -z -n -E '^[ \t]*(<{7}|={7}|\|{7}|>{7})' -- .`
   - Runs against the **index**, so a dirty worktree cannot bypass it.
   - Anchored with `^[ \t]*`, so it catches indented conflict markers and
     diff3 base markers (`|||||||`) but not seven-char runs that appear
     mid-line (e.g. a Python string literal `"==========="`).
   - Caveat: a Markdown Setext underline (`=======` under a heading)
     still matches because the regex has no multi-line context.
     False-positive suppression for Setext is intentionally out of scope.
2. **Staged Python enumeration** — `git ls-files --cached -z -- '*.py'`
   - NUL-terminated output preserves filenames with embedded quote,
     backslash, space, tab, or newline bytes (which `git diff --name-only`
     would C-quote and break downstream `git checkout-index`).
3. **Fail-closed Git reads** — every Git read (`grep`, `ls-files`,
   `rev-parse`, `checkout-index`) distinguishes "no match" (exit 1) from
   a genuine read failure (any other non-zero status). A read failure
   exits with the upstream status and a stable stderr tag so CI can
   route the failure correctly.
4. **Staged-blob materialization** — `git checkout-index --prefix=$tmp/`
   copies the selected index entries into a fresh tempdir.
5. **Ruff against the snapshot** — `ruff check --no-fix --quiet --config
   "$repo_root/.ruff.toml" -- .` runs once against the materialized
   snapshot. `--config` pins to the repo's Ruff config so a stale
   user-level override cannot silently weaken enforcement.

## What it explicitly does NOT do

- **Auto-fix.** Findings block the commit; the user must fix them,
  `git add` the result, and retry. The commit-blocked stderr message
  prints the exact `ruff check --fix -- <files>` command, plus a
  `--no-verify` escape hatch for emergency hotfixes.
- **Touch the worktree.** Ruff runs against a tempdir copy, so a
  worktree the user is mid-edit in is never modified by the hook.
- **Re-write repo-tracked config.** The old behavior that mutated
  `.github/ci-review-provider.txt` from `.env:CI_REVIEW_PROVIDER` was
  removed in #230.

## Behavior contract — pinned by tests

The hook is pinned by `tests/test_pre_commit_lint.py`:

| Behavior | Test |
| --- | --- |
| Clean staged Python passes | `test_clean_staged_py_passes` |
| `F401` blocks with actionable stderr | `test_F401_blocks_with_actionable_stderr` |
| Lints staged blob, not worktree | `test_lints_staged_blob_not_worktree` |
| Indented conflict markers blocked | `test_blocks_indented_conflict_markers_in_staged_blob` |
| diff3 base marker (`|||||||`) blocked | `test_blocks_diff3_merge_base_marker` |
| Invalid `GIT_INDEX_FILE` fails closed | `test_fails_closed_when_index_is_invalid` |
| Plain conflict markers blocked | `test_blocks_conflict_markers_in_any_staged_blob` |
| Non-Python commit is a no-op | `test_no_staged_py_is_noop_even_without_ruff` |
| Missing Ruff emits actionable error | `test_missing_ruff_emits_actionable_error` |
| Filenames with special chars lint correctly | `test_lints_staged_file_with_special_chars_in_name` |

## Failure modes

| stderr pattern | meaning | remediation |
| --- | --- | --- |
| `[pre-commit] BLOCKED: staged conflict marker found.` | Conflict marker hit | Edit the file, remove the marker, `git add`, retry |
| `[pre-commit] ERROR: unable to inspect staged content (git grep exited N).` | Index read failure | Check `GIT_INDEX_FILE`; rerun `git status` |
| `[pre-commit] ERROR: unable to list staged Python files (git ls-files exited N).` | Same | Same |
| `[pre-commit] ERROR: unable to read staged Python blobs (git checkout-index exited N).` | Same | Same |
| `[pre-commit] BLOCKED: commit blocked by Ruff findings.` | Ruff findings | Run the printed `ruff check --fix -- <files>` |
| `[pre-commit] ::error:: ruff is required to lint staged Python files.` | Ruff not on `$PATH` | `brew install ruff` or `apt install ruff` |

## Related

- [`docs/local-ci.md`](../local-ci.md) — the pre-push pytest gate
  (`/dev-kit:babysit-pr-local`). Different mechanism, different stage.
- [`docs/hooks/HOOK-REFERENCE.md`](HOOK-REFERENCE.md) — full hook matrix
  for the Claude Code / Codex enforcement layer.
- [Issue #795](https://github.com/sh-ai-x/dev-harness-kit/issues/795) —
  the staged-vs-worktree divergence this hook closes.
