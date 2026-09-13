# `.githooks/pre-commit` — staged-content lint gate

The local commit hook validates the content that is actually staged. It does
not lint unstaged worktree edits and it never auto-fixes files.

## Checks

1. Scan all staged blobs for line-start conflict markers, including indented
   and diff3 markers. A clean worktree cannot hide a marker already in the
   index.
2. Enumerate only staged Python paths with a NUL-delimited staged diff. This
   avoids re-checking every tracked Python file when an unrelated text file is
   staged and preserves unusual filenames.
3. Materialize those index entries into a temporary directory and run Ruff
   against that snapshot with the repository Ruff configuration.

Git read failures are fail-closed. A non-Python-only staged change remains a
no-op, and missing Ruff produces an actionable installation error when a
Python file is staged.

## Tests

`tests/test_pre_commit_lint.py` covers clean and failing staged Python,
unstaged-worktree divergence, unrelated staged files, conflict markers,
invalid index reads, missing Ruff, and filenames containing spaces or quotes.

The hook is activated with:

```bash
git config core.hooksPath .githooks
```
