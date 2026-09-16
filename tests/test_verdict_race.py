#!/usr/bin/env python3
"""test_verdict_race.py — retired: verdict-extraction bash moved to the gates repo.

Phase 4 gates-distribution migration (issue #12): the verdict-
extraction and gate-time-race logic this file used to pin
(dev-harness-kit issues #104, #244, #253, #638) lived in the CONSUMER
TEMPLATE (templates/ci/.github/workflows/{review,security}.yml). Those
templates are now thin `uses:` wrappers around
sh-ai-x/dev-harness-kit-gates' reusable workflows — the extraction
bash moved there too.

Ported verbatim (path-adjusted) to sh-ai-x/dev-harness-kit-gates as
tests/test_verdict_race.py — see that repo for the live coverage.

This file replaces the pre-migration
tests/test_template_review_verdict_race.py (same rename applied on
the gates-repo side for clarity, since "template" no longer describes
where the logic lives).

This stub stays as a discoverability breadcrumb; it intentionally
contains no test cases of its own.
"""
