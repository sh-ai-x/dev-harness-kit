#!/usr/bin/env python3
"""review-local-broken.py — ruff tripwire for PR #738.

Intentionally broken to trip ruff's lint gate so the babysit-pr
loop has something deterministic to fix. The fix is to delete
this file (it's not load-bearing for the PR's doc work).
"""
import os
import sys


def greet(name):
    unused_local = "this variable is never read"  # F841
    print("hi")
    return "hello, " + name


if __name__ == "__main__":
    greet("world")
