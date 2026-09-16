#!/usr/bin/env python3
"""test_security_gate.py — retired: verdict-gate bash moved to the gates repo.

Phase 4 gates-distribution migration (issue #12): the "Security
verdict gate" step this file used to extract and execute lived in the
CONSUMER TEMPLATE (templates/ci/.github/workflows/security.yml). That
template is now a thin `uses:` wrapper around
sh-ai-x/dev-harness-kit-gates' reusable security.yml — the gate bash,
along with all the historical-bug regression coverage this file used
to provide (#212-C1, #244, #397, #625, #726, #732), moved there too.

Ported verbatim (path-adjusted) to sh-ai-x/dev-harness-kit-gates as
tests/test_security_gate.py — see that repo for the live coverage.

This stub stays as a discoverability breadcrumb; it intentionally
contains no test cases of its own.
"""
