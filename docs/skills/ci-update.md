> [← Skills index](../README.md) · [Project README](../../README.md)

# `ci-update`

**Category:** `bootstrap` · **Alpha:** `state` · **Invocation:** `/dev-kit:ci-update` (human-invoked)

`ci-update` detects + selectively applies drift between the consumer's installed CI templates and the current dev-kit source. Classifies every `EXPECTED_PATHS` entry into one of four states (`new` / `updated` / `consumer_modified` / `diverged`) and offers a safe apply path that backs up before overwriting. Closes the dev-kit ⇄ consumer gap that previously left consumers blind to plugin upgrades.

## When to use it

- The user types `/dev-kit:ci-update` and wants to see what dev-kit shipped since their last `ci-setup`.
- The user is preparing a PR that refreshes stale CI templates.
- A dev-kit release shipped a security fix to `.github/workflows/review.yml` (or any other `EXPECTED_PATHS` file).
- `ci-doctor` flagged a `templates current` WARN row.

## How it works

A 4-phase orchestration via `lib/ci_update.py`:

**Phase 1 — Detect** (no LLM call): resolve the consumer marker; if `installed_dev_kit_version` is missing, refuse and redirect to `/dev-kit:ci-setup` (the idempotent re-install backfills the field).

**Phase 2 — Diff** (no LLM call): `diff_ci_install(target, plugin_root=…)` walks every `EXPECTED_PATHS` entry, classifies each into one of four states, and returns an `UpdateReport`. Renders a 4-column table (`Path | State | Backup | Action`) and a one-line summary.

**Phase 3 — Apply** (optional, requires `--apply` or `--force`): for each approved file, write `<rel>.bak` first when the on-disk bytes differ from dev-kit's source, then copy from dev-kit source via `_resolve_template_source(rel)`. Refuses to overwrite `consumer_modified` + `diverged` files without explicit per-file consent (mode=`apply`) or `--force`.

**Phase 4 — Re-record marker**: writes a fresh marker with the new `installed_dev_kit_version`, refreshed `template_shas`, and re-hashed `installed_file_shas`. Preserves `installed_at` so install history is honest. Atomic write via `lib/atomic.py`.

## Usage

```bash
/dev-kit:ci-update [--apply] [--force] [--no-backup] [--target DIR] [--provider NAME]
```

| Flag | Effect |
|---|---|
| *(0-arg)* | Dry-run diff + table. No writes. |
| `--apply` | Write `new` + `updated`. Prompt on `consumer_modified` + `diverged`. |
| `--force` | Overwrite all four states. No prompts. |
| `--no-backup` | Skip `.bak` creation (explicit user choice; NOT recommended). |
| `--target DIR` | Cross-repo apply. Hidden flag. |
| `--provider NAME` | Override CI review provider for lint pass. Hidden flag. |

Failure exit codes: `1` = arg error, `2` = marker missing or `installed_dev_kit_version` unknown, `3` = source unreadable, `4` = apply error.

## Drift states

| State | Meaning | `apply` | `force` |
|---|---|---|---|
| `new` | In dev-kit's `EXPECTED_PATHS` but not at the target | write | write |
| `updated` | Dev-kit changed since install, consumer unchanged | write | write |
| `consumer_modified` | Consumer edited their copy, dev-kit unchanged | prompt / skip | write (with `.bak`) |
| `diverged` | Both sides changed | prompt / skip | write (with `.bak`) |
| `unchanged` | All three SHAs match | n/a | n/a |

`.gitignore` is excluded from the diff and the apply path (its install is a marked-block merge — see `lib/ci_setup.py:_install_gitignore_fragment`). To re-merge the dev-kit fragment, run `/dev-kit:ci-setup --force`.

## Output

- `.dev-kit/ci-config.json` — refreshed with the new `installed_dev_kit_version` + `template_shas` + re-hashed `installed_file_shas`. `installed_at` preserved.
- `<rel>.bak` files alongside any overwritten consumer-modified or diverged file (when `backup=True`).
- Summary table printed to stdout; also written to `.dev-kit/hand-off/ci-update→ci-doctor.md` for the audit run.

## Backwards compatibility

Consumers with v1.0.0 markers (no `installed_dev_kit_version`) get automatic backfill on the next `/dev-kit:ci-setup` invocation — the no-op idempotent re-install path detects the missing fields and writes them without touching any files. After backfill, `/dev-kit:ci-update` works normally.

## Related

- [bootstrap](bootstrap.md) — typically run before this skill.
- [`ci-setup`](ci-setup.md) — writes the marker `ci-update` reads; refuses to start without it (back-compat via backfill).
- [`ci-doctor`](ci-doctor.md) — `templates current` check uses the same diff engine; surface a `WARN` row when refresh is needed.
- `/dev-kit:babysit-pr` — auto-fixes PRs flagged by the review gate.
- `docs/quality/ci-update.md` — full usage doc with worked examples.

---
*Source: [`skills/ci-update/SKILL.md`](../../skills/ci-update/SKILL.md)*
