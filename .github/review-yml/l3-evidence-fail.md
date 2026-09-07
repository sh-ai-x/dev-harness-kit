**L3 evidence gate: FAIL**

PR body does not include a quoted pytest tail line (Iron Law L3). The
`::error::PR body lacks a quoted pytest tail line.` block in the
Actions log is the same diagnostic — this comment is the
PR-visible mirror of that log.

## What is required

A pytest summary line matching one of:

- `<N> passed in <Ns>s`                        (all-green, no skipped)
- `<N> passed, <N> skipped in <Ns>s`           (all-green, with skips)
- `<N> failed, <N> passed, <N> skipped in <Ns>s` (partial-red)
- `<N> failed in <Ns>s`                        (total-red)

The `skipped` / `xfailed` / `xpassed` segment is optional — pytest
omits it when the count is 0; do not fabricate a skip count just to
satisfy the gate.

## How to fix

1. Open `.github/pull_request_template.md` and find the **Iron Law L3**
   fenced block.
2. Run `python3 -m pytest -q` locally and copy the EXACT summary line
   from the tail (no paraphrase, no reformatted table).
3. Paste it inside the fenced block in the PR body.
4. Push the updated PR body — the gate will re-run on the next push.

Reference: https://github.com/sh-ai-x/dev-harness-kit/issues/803
