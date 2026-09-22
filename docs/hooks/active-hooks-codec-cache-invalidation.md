# Active-hooks codec cache invalidation

> SSOT for the `lib/active_hooks_codec.py::_MATRIX_CACHE` invalidation
> contract. Pinned by `tests/test_active_hooks_codec.py::test_set_stage_invalidates_cache_even_when_mtime_unchanged`
> and `...::test_disable_override_invalidates_cache_even_when_mtime_unchanged`.

## The contract

`load_matrix(project_root)` memoizes parsed JSON keyed on
`(project_root, st_mtime)` so long-running sessions (one matrix read per
`PreToolUse` event via `stage-gate.sh`) pay one disk read per `mtime`
change instead of one read per invocation.

Mutating writers (`set_stage`, `disable_override`) **explicitly evict
every cache entry whose path matches the write's `project_root`** after
`atomic_write_json` returns — independent of whether the rewrite bumped
`st_mtime`.

## Why the explicit invalidate

The `(path, mtime)` cache key naturally diverges on filesystems where
`os.replace` produces a new `st_mtime` (ext4 with nanosecond resolution,
APFS). On coarse-mtime filesystems (NFS, some Docker overlay layers,
FAT32) `os.replace` can leave `st_mtime` unchanged, so the
`(path, mtime)` cache key would still match and serve the pre-write
payload. The explicit invalidate closes that window for the codecs
without requiring callers to coordinate on a different cache key.

## What calls it

- `lib/active_hooks_codec.py::set_stage` — after `atomic_write_json`.
- `lib/active_hooks_codec.py::disable_override` — after `atomic_write_json`.

## What does NOT call it

- `tools/regenerate_active_hooks.py` — does not own the codec cache.
  The regen writer only owns the `events` / `schema_version` /
  `generated_at` slice and is invoked by `hooks/session-start-check.sh`
  on every SessionStart, so the next `load_matrix` runs against a fresh
  session where the cache is empty.
- `lib/active_hooks_codec.py::ensure_matrix` — initialization path,
  cache starts empty in any new process.

## See also

- `hooks/index.md` — top-level hook matrix SSOT.
- `lib/active_hooks_codec.py::_invalidate_cache` — implementation.
- `tests/test_active_hooks_codec.py` — regression coverage.
