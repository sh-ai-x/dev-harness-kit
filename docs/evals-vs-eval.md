# Eval directories

This repository has two evaluation surfaces with different owners and schemas.

## `eval/`

`eval/` is the repository's existing harness. Its cases, prompts, rubrics, and
goldens are consumed by the Python evaluation modules and the evaluation-related
skills. Add fixtures here when the test should run through the repository's
`lib/eval_runner.py` or related tooling.

## `evals/`

`evals/` is the Claude plugin evaluation surface. The `claude plugin eval`
command discovers case directories containing `prompt.md` and declarative
graders under `graders/`. These files are intentionally not imported by
`lib/eval_runner.py`; they exercise the installed plugin boundary and its tool
usage/output contract. Local result artifacts are written under `evals/results/`
and are ignored by Git.

Keep the two trees separate when a case targets the Claude plugin runner rather
than the repository Python harness. Name new cases after the boundary they pin,
and state in the PR body which runner owns the fixture.
