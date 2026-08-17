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

Unknown paths are deferred to the local subscription-backed LLM judge. The
judge is a fallback for ambiguity, not the primary policy engine.
