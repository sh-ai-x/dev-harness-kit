# bare-except narrowing pattern

## Rule

Never `except Exception:` — narrow to the realistic failure set of the inner
ops, mirroring `lib/analysis_core/runner.py:_mask_secrets`.

## Why

- `Exception` swallows `BaseException` cousins (`SystemExit`, `KeyboardInterrupt`),
  so SIGTERM/SIGINT during a write or tree-walk is silently absorbed.
- Hides programmer errors (`AttributeError`, `NameError`, `KeyError`) behind
  stale fallback strings like `'(tree extraction failed — STALE)'`.
- Lets timing signals go silently to `0.0` when ISO strings parse-fail
  (looks like a sub-millisecond run, indistinguishable from a malformed input).

## Narrowing map (PR #705 baseline)

| File | Site | Narrow tuple |
|---|---|---|
| `lib/atomic.py` | `atomic_write_json` | `(OSError, ValueError, TypeError)` |
| `lib/atomic.py` | `atomic_write_text` | `(OSError, UnicodeEncodeError)` |
| `lib/execute.py` | `_transition_completed` | `(ValueError, TypeError)` |
| `lib/execute.py` | `_step_post_collect` | helper + caller uses `(ValueError, TypeError)` |
| `lib/write_project_md.py` | `_safe_tree` | `(OSError, ValueError)` |
| `lib/write_project_md.py` | `_safe_deps` | `(OSError, UnicodeDecodeError)` |
| `lib/behavior_scorers/efficiency.py` | `_save_baseline` | `(OSError, ValueError, TypeError)` |

## Test pattern

Every narrowing gets a regression test verifying:
1. realistic failure mode raises the narrow tuple
2. cleanup still fires (no `.tmp` leftover)
3. `KeyboardInterrupt` / `SystemExit` propagate (BaseException, not Exception)
4. AttributeError / NameError propagate (programmer errors surface loudly)

## When adding a new site

1. Read the inner ops (3 lines max — `json.dump` + `os.fdopen` + `os.replace`,
   or `datetime.fromisoformat`, or `path.read_text(encoding=...)`).
2. Match the tuple to what those ops can actually raise (stdlib docs
   for each: `json.JSONDecodeError` ⊂ `ValueError`, `UnicodeDecodeError`
   ⊂ `ValueError` but distinct enough to deserve its own slot when the
   failure is encoding-specific).
3. Add a RED test that asserts the realistic exception raises + cleanup
   runs + a non-realistic exception (AttributeError / KeyboardInterrupt)
   propagates.
4. Verify before claiming done: pytest exits 0; the targeted run covers
   the new site + any downstream importer (`test_ci_setup_atomic_import`
   pins the `atomic.py` import contract).

## See also

- `.dev-kit/inspect-report.md` — the source inspect baseline (cleancode dim)
- `lib/analysis_core/runner.py:_mask_secrets` — the established narrow-exception pattern this doc mirrors
- `docs/quality/maintenance-gate.md` — the docs-updated gate rule that
  required this file to exist when the cleancode sites were changed
