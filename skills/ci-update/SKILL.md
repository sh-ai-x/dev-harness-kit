---
name: ci-update
category: bootstrap
description: Detect + selectively apply drift between installed CI templates and current dev-kit source. 4-state per-file classification with backup-before-overwrite.
alpha: state
when_to_use:
  - User types /dev-kit:ci-update
  - Consumer wants to see what dev-kit changed since their last /dev-kit:ci-setup
  - User is preparing a PR that refreshes stale CI templates
  - A dev-kit release shipped a security fix to .github/workflows/review.yml
allowed-tools: Read Write Glob Bash AskUserQuestion
disallowed-tools: Agent WebFetch Edit
model: sonnet
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

# /dev-kit:ci-update — Refresh CI Templates

## Iron Law

**Read-only by default. Backup-before-overwrite on every write. Never touches files outside `EXPECTED_PATHS`.** Closes the dev-kit ⇄ consumer gap: a consumer who ran `/dev-kit:ci-setup` at dev-kit v0.3.200 has no way to see "v0.3.287 shipped new templates" — the marker is content-only until this skill reads its `installed_dev_kit_version` + `template_shas` fields. The same drift classifier powers `ci-doctor`'s `templates current` check, so an audit before refresh is cheap.

## What it does

Reads the consumer marker `.dev-kit/ci-config.json`, classifies every `EXPECTED_PATHS` entry into one of four drift states, and offers a safe apply path:

| State | Meaning | Action on `--apply` |
|---|---|---|
| `new` | In dev-kit's `EXPECTED_PATHS` but not installed | write from source |
| `updated` | Dev-kit changed since install, consumer unchanged | write from source |
| `consumer_modified` | Consumer edited their copy, dev-kit unchanged | prompt / skip |
| `diverged` | Both sides changed | prompt / skip |

`--force` overwrites consumer-modified + diverged files too, with a `<rel>.bak` written first when `backup=True` (the default).

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

Failure exit codes: `1` = arg error, `2` = marker missing, `3` = source unreadable, `4` = apply error.

## 3-Phase Orchestration

### Phase 1 — Detect

1.1. Parse arguments (`--target`, `--apply`, `--force`, `--no-backup`).
1.2. Resolve the consumer marker via `_resolve_prior_marker(target)`.
1.3. If `installed_dev_kit_version` is missing or `"unknown"`:
   - Emit a WARN: "marker predates schema; run `/dev-kit:ci-setup` first to backfill"
   - Refuse to compute state classification; exit 2
   - The consumer's marker was written before this skill existed; the
     backfill happens automatically on the next `/dev-kit:ci-setup`
     call (no-op idempotent re-install path).
1.4. Resolve `plugin_root` from `${CLAUDE_PLUGIN_ROOT}` (preferred) or
     `lib/ci_setup._PLUGIN_ROOT` (fallback when invoked in the source
     repo). The skill body always passes `plugin_root` explicitly into
     `diff_ci_install` / `apply_ci_update`.

### Phase 2 — Diff

```bash
python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, 'lib')
from ci_update import diff_ci_install
report = diff_ci_install(Path('${TARGET_DIR}'), plugin_root=Path('${PLUGIN_ROOT}'))
print(f'new={len(report.new)} updated={len(report.updated)} unchanged={len(report.unchanged)} consumer_modified={len(report.consumer_modified)} diverged={len(report.diverged)}')
"
```

2.1. Render a 4-column table:

```
| Path | State | Backup | Action |
|------|-------|--------|--------|
| scripts/validate.py | updated | n/a | write from source |
| .github/workflows/review.yml | consumer_modified | .bak | prompt |
| .gitignore | (skipped — merge target) | n/a | run ci-setup --force to re-merge |
```

2.2. If all four lists are empty → PASS; the consumer is fully current.
2.3. Emit a one-line summary: `installed=v0.3.287; new=2 updated=5 consumer_modified=1 diverged=0`.

### Phase 3 — Apply

On `--apply` (or `--force`):

3.1. If `mode='apply'` AND there are `consumer_modified` or `diverged`
     files → AskUserQuestion, one prompt per file:
     - "Overwrite `.github/workflows/review.yml` (you edited it locally)?
       Backup will be written to `.bak`. [Overwrite / Skip / Cancel]"
     - Default: Skip. User must explicitly opt in to each overwrite.
3.2. For each approved file: `shutil.copy2(target, target + ".bak")`
     first (only when bytes differ from source); then copy from dev-kit
     source via `_resolve_template_source(rel)`.
3.3. After apply, rewrite the marker with:
     - `installed_dev_kit_version`: fresh `plugin_version(plugin_root)`
     - `template_shas`: fresh `_compute_template_shas(plugin_root)`
     - `installed_file_shas`: re-hashed consumer copies
     - `installed_by`: `"dev-kit:ci-update"` (so audit trails distinguish
       initial install from refresh)
     - `installed_at`: PRESERVED (do not bump; refresh is not a re-install)
3.4. Print the summary table again post-apply, with a `Backed up: N`
     line and the new `installed_dev_kit_version`.

### Phase 4 — Hand-off

After a successful apply:

- Recommend `/dev-kit:ci-doctor` to confirm the broader CI readiness
  (secrets, gh auth, branch protection).
- Recommend committing the updated templates + the refreshed marker
  in one PR. The commit message should follow Conventional Commits:
  `chore(ci): refresh dev-kit templates to v0.3.287`.

## Files NOT touched

- **`.gitignore`**: skip in the diff and skip in apply. The install path
  for `.gitignore` is a marked-block merge (see
  `lib/ci_setup.py:_install_gitignore_fragment`); it does not match
  `shutil.copy2` semantics. Consumers who want the marker block
  refreshed should run `ci-setup --force` — that path preserves
  user-owned lines outside the `# >>> dev-kit >>>` / `# <<< dev-kit <<<`
  markers.

## Rules (no exceptions)

- **Never** write to a file outside `EXPECTED_PATHS`. Stale artifacts
  outside the canonical inventory are the consumer's responsibility.
- **Never** delete files. `ci-update` is additive / refresh-only; removal
  is a separate concern (future `--prune-stale` flag, not in v1).
- **Always** create `<rel>.bak` before overwriting when `backup=True`
  AND `target_sha != template_sha`. Skipping the backup when the bytes
  would be identical keeps idempotent re-runs from stacking stale
  `.bak` files.
- **Always** preserve `installed_at` in the marker on apply — refresh
  is not a re-install.
- Refuse to operate when the marker lacks `installed_dev_kit_version`
  (predates schema). The user must run `ci-setup` first.

## Hook integration

| Hook | Mode |
|---|---|
| worktree-guard | ON (edits happen in worktree, never main) |
| stop-verify | ON — quoted exit code + summary table required before "done" |
| secret-scan | ON (PostToolUse) — never write a token to a template file |

## Output

- Updated `.dev-kit/ci-config.json` (refreshed version + template_shas +
  re-hashed installed_file_shas).
- `<rel>.bak` files alongside any overwritten consumer-modified file.
- The summary table printed to stdout (also written to
  `.dev-kit/hand-off/ci-update→ci-doctor.md` so the audit run has the
  pre/post context).

## Next step

Hand off to `/dev-kit:ci-doctor` (audit confirms readiness) then
`git add` + commit + push + open a PR. The refreshed marker records
the new `installed_dev_kit_version`; the next `ci-update` run starts
from the new baseline.

---

*Source: [`skills/ci-update/SKILL.md`](../../skills/ci-update/SKILL.md)*
