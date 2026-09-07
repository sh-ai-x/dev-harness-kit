# hooks/references/ — runtime-loaded reference data (SSOT)

> **Not user-facing docs.** Despite the `.md` extension, every file in this
> directory is **machine-readable data** consumed by hooks and skills at
> runtime. Editing the contents changes runtime behavior; deleting files
> degrades or breaks the consuming hook.

The hooks in `hooks/*.sh` and the `inspect --slop` skill mode load their
detection banks from here rather than inlining them in shell scripts — this
keeps the patterns editable without re-bumping the script, and lets the same
bank be reused across the hook and the offline audit skill.

## Loader contract (POSIX shell + `grep`)

```bash
# Strip `#`-prefixed comments and blank lines, leaving one regex per line.
grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$BANK_FILE"
```

Bank files are **line-delimited POSIX ERE**. `#` lines and blank lines are
skipped at load time. Korean patterns are kept literal (no POSIX class
wrappers) so they match under Python `re` without locale-dependent collation.

## Index

| File | Format | Loaded by | Fallback |
|---|---|---|---|
| `l4/markers.md` | POSIX ERE bank | `hooks/l4-todo-scan.sh` (and `lib/ci_setup.py` for marker sanity) | None — hook fails closed on missing bank |
| `slop/phrases.md` | POSIX ERE bank | `hooks/slop-detector.sh` (T1), `skills/inspect --slop` (T1) | Inline v1 single regex (degraded; hook prints `WARN: references/slop/... not loaded`) |
| `slop/structures.md` | POSIX ERE bank | `hooks/slop-detector.sh` (T2), `skills/inspect --slop` (T2) | Inline v1 single regex (same `WARN`) |
| `slop/scoring.md` | plain markdown, no machine-parsed lines | `skills/inspect --slop` (1-10 × 5-dim rubric) | None |
| `slop/examples.md` | plain markdown | `skills/inspect --slop` (human reviewer reference only) | None |

`slop/README.md` carries the loader contract in more detail (severity tier,
`SLOP_LEVEL` / `SLOP_QUIET` / `SLOP_STRICT` env vars). It's the per-bank
README; this file is the per-directory index.

## When to edit

- **New detection pattern** → add a POSIX ERE line to the matching bank.
- **False positive** → remove the matching ERE (and confirm the bank loader
  re-loads on next hook invocation).
- **New language** → add a new section header (`# === <LANG> ... ===`) and
  the patterns underneath; the loader skips `#` lines.
- **Adding a new bank file** → wire it into `hooks/references/<bank>/README.md`
  and update the consumer hook's loader. Don't silently inline EREs in
  `hooks/*.sh` — that's the regression `slop-detector.sh` was rewritten to
  avoid.

## Related

- `docs/hooks/HOOK-REFERENCE.md` — full hook inventory (by what each guards
  + by the event that fires it). Each pattern bank is listed in the
  consuming hook's row.
- `hooks/slop-detector.sh` and `hooks/l4-todo-scan.sh` — the consumers.
- `tests/fixtures/slop/` — real before/after fixtures for the slop
  detector (distinct from `hooks/references/slop/examples.md`, which is the
  human-reference copy).
