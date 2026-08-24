#!/usr/bin/env bash
# review-local-quiet.sh — helper for testing the docs gate.
# A trivial utility that prints the current timestamp. Created
# here only to exercise the maintenance gate's
# "bin/ changed without docs/ update" rule on PR #738.
date -u +"%Y-%m-%dT%H:%M:%SZ"