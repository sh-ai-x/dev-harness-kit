---
name: gate-artifacts
category: config
description: Create or delete managed GitHub Actions gate artifacts backed by gates.json.
alpha: enforcement
when_to_use: |
  - User types /dev-kit:gate-artifacts plan <gate>
  - User types /dev-kit:gate-artifacts create <gate>
  - User types /dev-kit:gate-artifacts delete <gate>
  - A repo needs a custom GitHub Actions gate wired into dev-kit's gates.json SSOT
allowed-tools: Read Bash AskUserQuestion
disallowed-tools: Write Edit WebFetch Agent
model: opus
disable-model-invocation: false
user-invocable: true
---
> [← Skills index](../../README.md)

## What it does

`/dev-kit:gate-artifacts` is the operator-facing wrapper around `lib/gate_artifacts.py`. It creates and deletes dev-kit-managed custom GitHub Actions gate workflows while keeping `.dev-kit/gates.json` as the single gate state source of truth. Delete is fail-closed: only artifacts recorded in `.dev-kit/gate-artifacts.json` with matching checksums may be removed.

## Gate names

Custom gate names must be lowercase kebab-case, 3-40 characters, start with a letter, end with a letter or digit, contain no `--`, and avoid reserved names such as `ci`, `auto-fix-pr`, `branch-policy`, `review`, `security`, and `maintenance`.

A gate named `perf-smoke` maps to:

- workflow: `.github/workflows/perf-smoke.yml`
- state key: `.dev-kit/gates.json:gates.perf-smoke`
- GitHub variable: `GATES_PERF_SMOKE_ENABLED`

## Subcommands

```bash
/dev-kit:gate-artifacts plan perf-smoke
/dev-kit:gate-artifacts create perf-smoke
/dev-kit:gate-artifacts delete perf-smoke
```

The skill runs the corresponding deterministic helper command:

```bash
python -m lib.gate_artifacts plan <gate>
python -m lib.gate_artifacts create <gate>
python -m lib.gate_artifacts delete <gate>
```

## Safety rules

- Never hand-edit `.dev-kit/gate-artifacts.json`; let the helper update it.
- `create` refuses to overwrite an existing unmanaged workflow unless the operator explicitly passes the helper's `--overwrite` flag after reviewing the diff.
- `delete` refuses unmanaged gates, built-in gates, path traversal, malformed manifests, and checksum drift.
- The helper updates `.gitignore` with GJC runtime entries `.gjc/` and `.worktrees/`; it must not ignore source paths under `skills/`, `docs/`, `lib/`, `templates/`, `tests/`, or `hooks/`.
- Automated tests must mock or render GitHub CLI behavior; do not mutate live GitHub repository variables in tests.

## Verification

Run focused checks after changes:

```bash
python -m pytest tests/test_gate_artifacts.py tests/test_gates_state.py tests/test_gates_state_cli.py
python -m pytest tests/test_naming.py tests/test_skill_authoring.py tests/test_skill_governance.py tests/test_readme_skill_registry.py
git check-ignore -v .gjc/sentinel .worktrees/sentinel
git check-ignore -v skills/gate-artifacts/SKILL.md lib/gate_artifacts.py tests/test_gate_artifacts.py
```

The final `git check-ignore` command must produce no match for source paths.

## Next step

Run `/dev-kit:gate-select sync` after creating or deleting gates when GitHub repository variables need to be updated.
