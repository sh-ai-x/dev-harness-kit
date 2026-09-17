> [← Skills index](README.md) · [Project README](../../README.md)

# `refactor`

**Category:** `build` · **Alpha:** `analysis` · **Invocation:** `/dev-kit:refactor` (human-invoked)

`refactor` is the whole-pipeline cleanup chain: one slash command wraps `/dev-kit:inspect` (baseline), a four-pass rewrite (dead → dup → naming → coverage), and `/dev-kit:review`. It exists as a distinct skill from `/dev-kit:prune` because this pipeline never deletes features — it only rewrites, extracts, and renames — and each of its 3 phases is a separate call gated on a quoted exit code plus test count before the next phase is allowed to run.

## When to use it

- The user types `/dev-kit:refactor`.
- The user's language is "clean up the codebase" / "refactor everything" / "simplify the whole project".
- The user wants a whole-pipeline cleanup after a refactor PR.
- For actually deleting slop or dead features, `/dev-kit:prune` is the correct skill instead.

## How it works

`refactor` runs three ordered phases, each a separate call:

1. `[1/3] INSPECT` → `/dev-kit:inspect`, producing `.dev-kit/inspect-report.md`. Quoted evidence: report path + verdict + finding count.
2. `[2/3] REFACTOR` → cleanup pass (dead → dup → naming → coverage). Quoted evidence: 4× (pass name + test count + exit 0).
3. `[3/3] REVIEW` → `/dev-kit:review` (correctness + security + architecture). Quoted evidence: per-dimension finding count + verdict.

Phase rules: MUST-L1 forbids phase 2 without a phase-1 report; MUST-L3 requires each phase to end with a quoted exit code and test count (or per-dimension finding count); MUST-L4 forbids commented-out code, `pass`-as-stub, or "we'll fix this later" leftovers; MUST-NO-LOOP treats phases as sequential gates, not a retried cycle — if any phase is RED, the pipeline stops. The `refactor` skill itself never edits source files: phase 2 owns the mutations, while phases 1 and 3 are read-only.

The hook matrix during this pipeline: `tdd-guard` ON (phase 2 mutates code), `bash-guard` ON, `secret-scan` ON, `slop-detector` ON, `stop-verify` ON — a quoted full-suite green run is required before declaring the pipeline done.

## Usage

```bash
/dev-kit:refactor [<path>] [--phase N]
```

| Flag | Effect |
|---|---|
| (0-arg) | Sweeps the whole project. |
| `<path>` | Narrows the scope to a subpath. |
| `--phase N` | Re-runs one phase only. |

The full suite must run in under 10 minutes. There are no version-gated preconditions — the skill is self-referential.

## Output

No standalone artifact of its own beyond the phase-by-phase evidence: `.dev-kit/inspect-report.md` from phase 1, quoted pass/test evidence from phase 2, and a per-dimension finding count and verdict from phase 3's review.

## Related

- [prune](prune.md) — the deletion counterpart; use it instead for project-wide slop or dead-feature removal, or `prune --target <feature>` for one named feature.
- cleanup pass — the phase 2 mutation step that performs the 4-pass rewrite (dead → dup → naming → coverage).
- `/dev-kit:inspect` — phase 1's baseline report producer.
- `/dev-kit:review` — phase 3's correctness/security/architecture review.
- `/dev-kit:ship`, `/dev-kit:status` — the next steps once all 3 phases are green; `/dev-kit:plan` is the fallback to scope a structured fix for HIGH findings if a phase is RED.

---
*Source: [`skills/refactor/SKILL.md`](../../skills/refactor/SKILL.md)*
