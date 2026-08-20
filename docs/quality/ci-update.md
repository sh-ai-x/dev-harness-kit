# `ci-update` — full usage

`/dev-kit:ci-update` closes the gap between dev-kit's source templates
and the CI templates a consumer installed at some point in the past.
Before this skill, a consumer had no way to detect "dev-kit v0.3.287
shipped a security fix to `review.yml`"; the only refresh path was
`/dev-kit:ci-setup --force`, which is destructive (no selective mode,
no backup, no rollback). `ci-update` makes the gap visible and the
refresh safe.

## 1. The marker schema bump

`ci-setup` now writes two additional fields into `.dev-kit/ci-config.json`:

```json
{
  "schema_version": "1.0.0",
  "installed_at": "2026-08-11T10:37:02Z",
  "installed_by": "dev-kit:ci-setup",
  "installed_dev_kit_version": "0.3.287",
  "template_shas": {
    ".github/workflows/ci.yml": "fe9b7e77…",
    ".github/workflows/auto-fix-pr.yml": "9f485f78…",
    ".github/workflows/review.yml": "aee9816c…",
    …
  },
  "installed_file_shas": { … }
}
```

- `installed_dev_kit_version` — the dev-kit release tag at install time
  (read from `.claude-plugin/plugin.json:version`).
- `template_shas` — per-file SHA-256 of every `EXPECTED_PATHS` source as
  it sat in dev-kit at install time. Distinct from `installed_file_shas`,
  which is the SHA of the consumer's installed copy.

These two fields turn the marker from "presence-only" into "version +
content fingerprint". The next `ci-update` compares the recorded
`template_shas` against a fresh reading; differences are drift.

### Backwards compatibility

Consumers on the previous v1.0.0 marker schema (no
`installed_dev_kit_version`, no `template_shas`) get automatic backfill
on the next `/dev-kit:ci-setup` invocation — the no-op idempotent
re-install detects the missing fields and writes them. No files are
touched. `installed_at` is preserved so install history is honest.

## 2. Drift classification

For every `EXPECTED_PATHS` entry, the diff engine computes three
SHA-256 fingerprints:

- `target_sha` — the consumer's current on-disk file (or `None` if absent)
- `installed_sha` — `marker.installed_file_shas[rel]` (consumer-side SHA at install)
- `template_sha` — `marker.template_shas[rel]` (dev-kit SHA at install)

It then compares `consumer_changed = target_sha != installed_sha` and
`dev_kit_changed = template_sha != installed_sha`:

| consumer_changed | dev_kit_changed | State | Action |
|---|---|---|---|
| false | false | `unchanged` | (skip) |
| true | false | `consumer_modified` | prompt / skip on `--apply`; write on `--force` |
| false | true | `updated` | write |
| true | true | `diverged` | prompt / skip on `--apply`; write on `--force` |
| (target absent) | * | `new` | write |

`.gitignore` is excluded from the diff and the apply path (it is a
marked-block merge; see `lib/ci_setup.py:_install_gitignore_fragment`).

## 3. Usage examples

### Dry-run: see what dev-kit changed

```bash
$ /dev-kit:ci-update

ci-update: installed=v0.3.280; new=2 updated=5 consumer_modified=1 diverged=0

| Path                                   | State             | Backup | Action                |
|----------------------------------------|-------------------|--------|-----------------------|
| .github/workflows/review.yml            | updated           | n/a    | write from source     |
| .github/workflows/auto-fix-pr.yml      | updated           | n/a    | write from source     |
| scripts/extract-verdict.py             | updated           | n/a    | write from source     |
| .github/workflows/_verdict_fb.py       | new               | n/a    | write from source     |
| tools/linear_pr_sync.py                | new               | n/a    | write from source     |
| scripts/ci-local.sh                    | consumer_modified | .bak   | prompt / skip         |
| .gitignore                             | (merge target)    | n/a    | run ci-setup --force  |

dry-run: no files written; pass --apply or --force to write.
```

### Apply: refresh templates, prompt on consumer edits

```bash
$ /dev-kit:ci-update --apply

[1/2] writing updated files from dev-kit source…
  + .github/workflows/review.yml
  + .github/workflows/auto-fix-pr.yml
  + scripts/extract-verdict.py
[2/2] 1 consumer-modified file(s) need explicit consent…
  ? Overwrite scripts/ci-local.sh (you edited it locally)?
    Backup will be written to scripts/ci-local.sh.bak
    [Overwrite / Skip / Cancel] → Skip
[done] 3 written, 1 skipped, marker refreshed to v0.3.287
```

### Force: overwrite everything (with backups)

```bash
$ /dev-kit:ci-update --force

[1/3] backing up consumer-modified + diverged files…
  + scripts/ci-local.sh.bak
[2/3] writing all drifted files from dev-kit source…
  + 5 updated
  + 2 new
  + 1 consumer-modified (with .bak)
[3/3] marker refreshed to v0.3.287
[done] 8 written, 1 backed up, 0 skipped
```

## 4. Hook integration

`ci-update` is gated by:

- `worktree-guard.sh` — refuses edits in the main checkout (the diff
  and apply both run inside a worktree)
- `stop-verify.sh` — quoted exit code + summary table required before
  declaring done
- `secret-scan.sh` — never writes a token to a template file

## 5. Exit codes

| Code | Meaning |
|---|---|
| 0 | success (dry-run or apply) |
| 1 | argument error |
| 2 | marker missing or `installed_dev_kit_version` unknown (predates schema; run `/dev-kit:ci-setup` to backfill) |
| 3 | source unreadable (template path missing) |
| 4 | apply error (write failed) |

## 6. ci-doctor integration

`/dev-kit:ci-doctor` includes a `templates current` check that uses the
same diff engine. The check returns:

- **PASS** — every file matches dev-kit, no drift
- **INFO** — drift exists but only `new` + `updated` files (no consumer friction)
- **WARN** — consumer has modified files locally OR a file is stale
- **SKIP** — marker lacks `installed_dev_kit_version` (predates schema)
- **FAIL** — diff engine raised

## 7. Files written

| Path | When |
|---|---|
| `.dev-kit/ci-config.json` | always (atomic; preserves `installed_at`) |
| `<rel>.bak` | when overwriting an existing consumer file whose SHA differs from source |
| `.dev-kit/hand-off/ci-update→ci-doctor.md` | post-apply, for audit trail |

## 8. Files NOT touched

- `.gitignore` — marked-block merge; run `/dev-kit:ci-setup --force` to re-merge
- Files outside `EXPECTED_PATHS` — never read or written
- The dev-kit repo itself — `ci-update` is consumer-side; `lib/ci_update.py`
  uses `_PLUGIN_ROOT` only as a read-only source resolver

## 9. Backups and rollback

`<rel>.bak` files are NOT auto-pruned. They live alongside the
overwritten file. Manual cleanup:

```bash
git status                       # see which .bak files git tracks
git clean -f -- '*.bak'          # remove unstaged .bak files
# Or commit them, then delete after merge:
git rm '*.bak' && git commit
```

## 10. Related

- `/dev-kit:ci-setup` — initial install + marker write
- `/dev-kit:ci-doctor` — read-only audit (uses the same diff engine)
- `lib/ci_update.py` — diff + apply engine
- `lib/ci_setup.py:_compute_template_shas` — SHA computation against dev-kit source
- `lib/ci_doctor.py:_check_templates_current` — the ci-doctor row that calls into ci-update
