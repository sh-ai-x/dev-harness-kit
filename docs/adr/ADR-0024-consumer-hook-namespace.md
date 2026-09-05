# ADR-0024: keep consumer hook payload at `hooks/` (NOT `.dev-kit/hooks/`)

**Status:** Accepted (2026-09-05)
**Trigger:** Closed PR #785 — mechanical mirror of dev-kit-lite commit `45da476e` (PR #12) into dev-harness-kit, intended to namespace consumer hook install under `.dev-kit/hooks/`. Review surfaced a critical regression in `tests/test_hooks_single_source.py` plus a fabricated justification in the original PR body (claimed `role-frontend` rule collision that does not exist in this repo).
**Supersedes:** nothing — no prior ADR covered the consumer install namespace.

## Context

dev-kit-lite moved its consumer hook payload from project-root `hooks/` to `.dev-kit/hooks/` (commit `45da476e`, PR #12) to avoid collision with the React/Next.js/Vue convention for custom hooks (`hooks/useAuth.ts`). The collision was structural in dev-kit-lite because its `role-frontend` rule assigns `hooks/` to the frontend role — the kit's own role system would fight the project's app code.

PR #785 mechanically mirrored that move into dev-harness-kit. Review surfaced four reasons the mirror is wrong for this repo:

1. **No `role-frontend` rule exists here.** The PR body and CHANGELOG cited the rule as a justification, but a repo-wide search for `role-frontend` returned only the line being added in the same PR. The structural collision dev-kit-lite solved is absent.
2. **`.dev-kit/` is reserved for runtime metadata.** Pre-PR it held only state: `.active-hooks.json`, `ci-config.json`, `hand-off/`, `trace/`, `logs/`, `babysit-*.json`, `harness-mode.session.json`. Mixing in executable scripts (`hooks/*.sh`) blurs the directory's purpose.
3. **Asymmetry.** dev-kit's own working tree keeps `hooks/` (plugin self-reference `${CLAUDE_PLUGIN_ROOT}/hooks/...`). Consumer install landing at a different path creates a cognitive fork: "where do hooks live? Depends on whether you're in dev-kit or in a consumer."
4. **Critical regression.** `tests/test_hooks_single_source.py` was not in the rename sweep; `_HOOK_RELPATHS` still hardcoded `hooks/...` while `_canonical_hook_paths` wrote `.dev-kit/hooks/...`. The SSOT-bytes regression test failed on first run, blocking the PR.

The collision dev-kit-lite fixed is real for them; it is theoretical for us. No consumer report of the React/Next.js conflict has surfaced in dev-harness-kit's history.

## Decision

`/dev-kit:ci-setup` keeps installing the hook payload at `hooks/` in consumer repos. Project-root `hooks/` is the consumer install location; the dev-kit plugin's own working-tree `hooks/` is unchanged; the marker `.dev-kit/ci-config.json` `hooks` field lists `hooks/...` entries.

### Why `.dev-kit/hooks/` was considered and rejected

| Option | Pro | Con | Verdict |
|---|---|---|---|
| Status quo (keep `hooks/`) | Symmetric with dev-kit working tree; no `.dev-kit/` semantic blur; no breaking change | Theoretically vulnerable to React/Next.js consumer collision (no report) | **Chosen** |
| Move to `.dev-kit/hooks/` (dev-kit-lite mirror) | Symmetric with dev-kit-lite; future-proofs an edge case | Wrong justification cited; `.dev-kit/` semantic blur; critical test regression; consumer breaking change | Rejected (PR #785 closed) |
| `--devkit-namespace` opt-in flag | Lets collision-prone consumers opt in; default keeps symmetry | New surface to test, document, and migrate behind; the protected population (frontend consumers) is small enough to manually move if hit | Deferred — add only if a real report surfaces |

### What does NOT change

- dev-kit's own working-tree `hooks/` (plugin self-reference stays at `${CLAUDE_PLUGIN_ROOT}/hooks/<x>.sh`).
- The marker `hooks` field shape: `[hooks/<x>.sh, ...]`.
- `templates/ci/scripts/validate.py` regex (`hooks/<helper>.sh`).
- `tests/test_hooks_single_source.py` (kept on `hooks/...`).
- `EXPECTED_PATHS` / `EXECUTABLE_PATHS` / `_canonical_hook_paths`.

## Consequences

### Positive
- Cognitive symmetry between dev-kit's own layout and consumer install.
- `.dev-kit/` keeps its single semantic (runtime metadata).
- No consumer breaking change.
- Review-grade drift: the closed PR's review comments (1 critical + 2 major + 8 minor) are now the source of record for "what a future namespace move would have to address" — treat them as a checklist.

### Negative
- A React/Next.js / Vue consumer who hits the convention collision resolves it manually (see Migration below).
- The mirror-with-dev-kit-lite argument loses; kit family symmetry now stops at "both ship `hooks/` content" rather than "both ship under the same namespace."

## Migration (when a consumer hits the React/Next.js collision)

This migration is **optional and consumer-initiated**. dev-kit does not ship a tool for it because the affected population is small enough that manual steps are cheaper than the testing surface an automated move would add.

```bash
# 1. Audit existing hook content before relocating. `git mv` preserves
#    bytes; a pre-existing malicious hook will survive the namespace
#    change unchanged.
ls -la hooks/ && git log --oneline -- hooks/
grep -rE 'curl[[:space:]]+[|$]|nc[[:space:]]+-|base64[[:space:]]+-d' hooks/

# 2. Relocate (git mv preserves history + executable bits).
mkdir -p .dev-kit/hooks && git mv hooks/* .dev-kit/hooks/ && rmdir hooks

# 3. Rewrite the marker so `ci-doctor` / `ci-update` don't see drift.
python3 - <<'PY'
import json, pathlib
m = pathlib.Path('.dev-kit/ci-config.json')
d = json.loads(m.read_text())
d['hooks'] = [p.replace('hooks/', '.dev-kit/hooks/', 1) for p in d['hooks']]
m.write_text(json.dumps(d, indent=2) + '\n')
PY

# 4. Patch the shipped validator's hook prefix. The shipped
#    `scripts/validate.py` regex hard-codes `hooks/`. After the move,
#    update it to `.dev-kit/hooks/` (and update `_HOOK_SCRIPT_REFERENCE`
#    inside the file):
#      sed -i '' 's#hooks/([A-Za-z0-9_.-]+\\.(?:sh|json))#.dev-kit/hooks/\\1#' \
#        scripts/validate.py
#      sed -i '' 's#"hooks"/.dev-kit/hooks/"#' scripts/validate.py

# 5. Re-run: bash scripts/validate.py
```

If a consumer hits this path more than once across the user base, the right move is to introduce a `--devkit-namespace` opt-in flag (see "Options" table above) and turn the manual steps into `ci-setup` itself. Until then, manual is correct.

## Triggers for revisiting this decision

- A consumer-reported incident of the React/Next.js `hooks/` collision (one ticket is enough to re-open).
- Addition of a `role-frontend` rule to dev-harness-kit (the original dev-kit-lite trigger becomes applicable here).
- Codex parity work that moves consumer hook install to `.codex/hooks/` (would surface a third namespace choice; revisit the table).

## References

- Closed PR #785 (6 files, +169/-58, commit `d32ad028`).
- dev-kit-lite commit `45da476e` (PR #12: `fix(install): namespace kit hook scripts under .dev-kit/hooks/`).
- ADR-0023 (preceding ADR; same author, same week, same theme of "what dev-kit-lite and dev-harness-kit do together vs separately").
- Review findings on PR #785: 1 critical (test regression), 2 major (fabricated `role-frontend` justification; missing ADR), 8 minor.
