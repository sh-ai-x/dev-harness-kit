# hooks/references/ — runtime-loaded reference data (SSOT)

> **Not user-facing docs.** Despite the `.md` extension, every file in this
> directory is **machine-readable data** consumed by hooks and skills at
> runtime. Editing the contents changes runtime behavior; deleting files
> degrades or breaks the consuming hook.

The directory exists so future hooks can ship non-code data banks alongside
their `.sh` consumers without re-bumping the script (the same split the
slop-detector used to use, before v3 collapsed the regex bank into a
single LLM-judge call — see `eval/prompts/judge-slop.md`).

## Loader contract (POSIX shell + `grep`)

```bash
# Strip `#`-prefixed comments and blank lines, leaving one regex per line.
grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$BANK_FILE"
```

Bank files are **line-delimited POSIX ERE**. `#` lines and blank lines are
skipped at load time. Korean patterns are kept literal (no POSIX class
wrappers) so they match under Python `re` without locale-dependent collation.

## Index

This directory currently ships no runtime banks — the previous slop
regex tiers were removed in the v3 LLM-judge refactor. New banks
should follow the loader contract above and wire their consumer hook
in `docs/hooks/HOOK-REFERENCE.md`.

## Related

- `docs/hooks/HOOK-REFERENCE.md` — full hook inventory (by what each guards
  + by the event that fires it). Each pattern bank is listed in the
  consuming hook's row.
- `eval/prompts/judge-slop.md` — LLM judge prompt that replaced the
  `hooks/references/slop/` regex banks.
