# TDD scope policy

The TDD write guard applies only to meaningful production-code changes.

## Exempt changes

Documentation, configuration, tests, fixtures, generated files, formatting,
one-off scripts, and maintenance under `tools/`, `scripts/`, `bin/`, and
`hooks/` do not require a RED/GREEN cycle.

## Required changes

Core behavior under `lib/`, `src/`, `utils/`, `services/`, `domain/`, and API
paths requires confirmed RED evidence before production code can be edited.
Run:

```bash
python3 -m lib.tdd_cycle red -- <test command>
```

After the minimum implementation passes:

```bash
python3 -m lib.tdd_cycle green -- <test command>
```

The cycle CLI resolves its state root with the same
`${DEV_KIT_TDD_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}`
fallback as `tdd-guard.sh`, so RED/GREEN evidence
lands where the guard reads it even when `DEV_KIT_TDD_ROOT` points outside
the git toplevel. An explicit `--root` still overrides the resolution.

Unknown paths use the same conservative RED requirement as production code.
An explicit `.dev-kit/.tdd-scope.json` with `tdd_required: false` can exempt
an intentionally non-production edit without invoking a model or network
service at edit time.
