# Ralph build evidence

`bin/ralph-promote-phases.sh` copies a validated Ralph runtime bundle from
`.dev-kit/round-1/` into this tracked directory. Promotion is explicit in the
first version; the Ralph DONE transition does not invoke it automatically.

## Usage

```bash
bin/ralph-promote-phases.sh \
  --plan-id <plan-id> \
  --phase <phase-name> \
  --session <session-name>
```

Use `--dry-run` to validate the source and list the files without writing.
`--round` selects another `.dev-kit/round-<round>/` runtime when needed, and
`--project-root` selects the repository root when invoked from elsewhere.

The command fails closed unless all of these source artifacts exist and are
well formed:

- `.dev-kit/round-<round>/PRD.md`;
- `phases/<phase>/index.json` with a non-empty, unique step list;
- `phases/<phase>/step<N>.md` and `step<N>-output.json` for every indexed step;
- `.dev-kit/ralph/<session>.json` with a matching terminal `current_stage`.

The destination is `docs/build-evidence/<plan-id>/` and contains the plan,
phase index, step instructions, step output JSON, and `SUMMARY.md`. The
summary records the Ralph state, last/next action, blockers, duration and
exit-code table, recorded cost/check metadata, changed files when supplied by
the source output, and failed-step stderr tails. It labels source-reported
verification metadata and never treats an agent exit code as an independent
check.

Repeated promotion of byte-identical source is safe and does not append
duplicate records. A differing existing destination is rejected rather than
overwritten. Round, plan ID, phase, session, and artifact paths are validated
so promotion cannot escape the repository or write through a symlink.
