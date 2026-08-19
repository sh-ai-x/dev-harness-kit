#!/usr/bin/env python3
"""test_test_files_lint.py — enforce rules/test-files.md across .test.ts files.

The vitest-only + KO test names lint (issue #664, sub-issue 2 of 4).

Walks:
  - tests/**/*.ts and tests/**/*.tsx (all TS under tests/)
  - co-located *.test.ts anywhere in the repo (per the rule's
    "next to the source" co-location requirement)

Allowed paths (mirrors the slop-detector allowlist — `tests/fixtures/**`
and `docs/adoption/**` are never scanned):
  - tests/fixtures/**
  - docs/adoption/**

Enforcement-by-test: the tests report violations as `file:line:reason`
diagnostics on failure. A violation does NOT make the linter itself fail
(the linter is supposed to surface them). The test passes when the linter
returns the correct violation report — no missed checks, no false alarms.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).parent.parent

# Skip lists — mirrors the slop-detector allowlist + VCS / vendor noise.
SKIP_PATH_PARTS: tuple[str, ...] = (
    "tests/fixtures",
    "docs/adoption",
    ".worktrees",
    ".git",
    "node_modules",
    "__pycache__",
    ".dev-kit",
    "logs",
    "out",
)

# Issue #664 spec, rule #1: jest-forbidden patterns.
JEST_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"from\s+['\"]jest['\"]", "import from 'jest'"),
    (r"import\s+jest\s+from\s+['\"]jest['\"]", "default import from 'jest'"),
    (r"jest\.fn\(", "jest.fn() is forbidden (use vi.fn())"),
    (r"jest\.mock\(", "jest.mock() is forbidden (use vi.mock())"),
)

# Rule: test names must contain at least one Hangul syllable.
KOREAN_NAME_RE = re.compile(r"[가-힣]")

# Rule: every `it(...)` must have at least one `expect(...)` in its body.
# (Coverage rule from rules/test-files.md.)
EXPECT_RE = re.compile(r"\bexpect\s*\(")

# Rule: `vi.restoreAllMocks()` must be called when mocks exist.
VI_FN_RE = re.compile(r"\bvi\.fn\s*\(")
VI_MOCK_RE = re.compile(r"\bvi\.mock\s*\(")
AFTER_EACH_RE = re.compile(r"\bafterEach\s*\(")
RESTORE_ALL_MOCKS_RE = re.compile(r"\bvi\.restoreAllMocks\s*\(")

# Rule: no `it.only` / `it.skip` in committed code.
IT_ONLY_RE = re.compile(r"\bit\.only\s*\(")
IT_SKIP_RE = re.compile(r"\bit\.skip\s*\(")

# Rule #6: imports must be from `vitest`, not from jest-family packages.
NON_VITEST_RUNNERS: tuple[str, ...] = (
    "jest",
    "jest-circus",
    "jest-each",
    "jest-mock",
    "jest-environment-jsdom",
    "mocha",
    "jasmine",
    "@jest/globals",
    "@jest/core",
    "@jest/test-result",
    "ava",
)
VITEST_IMPORT_RE = re.compile(
    r"""(?<![A-Za-z0-9_])from\s+['"]([^'"]+)['"]"""
)
# Recognise the `it('...', ...) | it("...", ...)` header line.
IT_HEADER_RE = re.compile(
    r"""(?<![A-Za-z0-9_])it\s*\(\s*(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1"""
)


def _is_skipped(path: Path) -> bool:
    """Return True if `path` falls under any allowed-path allowlist."""
    parts = path.parts
    for skip in SKIP_PATH_PARTS:
        if skip in parts:
            return True
    return False


def _iter_target_files() -> Iterable[Path]:
    """Yield every TS/TSX file the linter should scan.

    - All `tests/**/*.ts` and `tests/**/*.tsx` (full subtree under tests/).
    - All `*.test.ts` co-located anywhere else in the repo (per the
      "next to the source" rule).
    - All `*.spec.ts` (rule YAML frontmatter lists it as a valid pattern).
    """
    seen: set[Path] = set()

    # (a) tests/**/*.ts and tests/**/*.tsx
    tests_dir = REPO_ROOT / "tests"
    if tests_dir.exists():
        for ext in ("*.ts", "*.tsx"):
            for p in tests_dir.rglob(ext):
                if not _is_skipped(p):
                    seen.add(p)
                    yield p

    # (b) co-located *.test.ts / *.test.tsx / *.spec.ts anywhere in the repo
    # (excludes tests/ which is already covered by (a)).
    for pattern in ("*.test.ts", "*.test.tsx", "*.spec.ts"):
        for p in REPO_ROOT.rglob(pattern):
            if _is_skipped(p):
                continue
            if p in seen:
                continue
            # The tests/ subtree was already covered by (a); skip re-emission.
            try:
                p.relative_to(tests_dir)
            except ValueError:
                yield p


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()


class TestNoJestImports(unittest.TestCase):
    """Rule #1: no `jest` import / `jest.fn()` / `jest.mock()` in TS files."""

    def test_no_jest_imports(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                for pat, reason in JEST_PATTERNS:
                    if re.search(pat, line):
                        rel = path.relative_to(REPO_ROOT)
                        violations.append(
                            f"{rel}:{lineno}: {reason}  (line: {line.strip()})"
                        )
        self.assertEqual(
            violations, [],
            "Jest is forbidden (rules/test-files.md — Framework section). "
            "Replace with `vi.fn()` / `vi.mock()` / `import from 'vitest'`.\n"
            + "\n".join(violations),
        )


class TestKoreanTestNames(unittest.TestCase):
    """Rule #2: every `it('...')` name must contain a Hangul syllable."""

    def test_korean_test_names(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                for m in IT_HEADER_RE.finditer(line):
                    name = m.group(2)
                    if not KOREAN_NAME_RE.search(name):
                        rel = path.relative_to(REPO_ROOT)
                        violations.append(
                            f"{rel}:{lineno}: non-Korean test name: {name!r}"
                        )
        self.assertEqual(
            violations, [],
            "Test names must be in Korean (rules/test-files.md — Naming). "
            "At least one Hangul syllable (가-힣) required.\n"
            + "\n".join(violations),
        )


def _find_it_bodies(text: str) -> list[tuple[str, int]]:
    """Return (name, body_start_offset) for every `it('name', ...)` call.

    `body_start_offset` is the index of the first character of the arrow
    function body (the `{` after the `=>`).
    """
    out: list[tuple[str, int]] = []
    for m in re.finditer(
        r"""(?<![A-Za-z0-9_])it\s*\(\s*(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1\s*,""",
        text,
    ):
        name = m.group(2)
        # Skip dotted variants: it.only / it.skip — those are violations
        # separately under rule #5.
        prefix_start = m.start()
        if prefix_start > 0 and text[prefix_start - 1] == ".":
            continue
        # Find the start of the arrow function body `{` after the matching `}`.
        idx = m.end()
        depth = 0
        in_str: str | None = None
        while idx < len(text):
            ch = text[idx]
            if in_str:
                if ch == "\\":
                    idx += 2
                    continue
                if ch == in_str:
                    in_str = None
                idx += 1
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
                idx += 1
                continue
            if ch == "{":
                if depth == 0:
                    out.append((name, idx + 1))
                    break
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "(" or ch == "[":
                depth += 1
            elif ch == ")" or ch == "]":
                depth -= 1
            idx += 1
    return out


def _line_for_offset(text: str, offset: int) -> int:
    """Return 1-based line number for a 0-based character offset."""
    return text.count("\n", 0, offset) + 1


class TestItHasExpect(unittest.TestCase):
    """Rule #3: every `it(...)` block must contain at least one `expect(...)`."""

    def test_it_has_expect(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, body_start in _find_it_bodies(text):
                # Match the body braces to find the closing `}`.
                depth = 1
                i = body_start
                in_str: str | None = None
                while i < len(text) and depth > 0:
                    ch = text[i]
                    if in_str:
                        if ch == "\\":
                            i += 2
                            continue
                        if ch == in_str:
                            in_str = None
                        i += 1
                        continue
                    if ch in ("'", '"', "`"):
                        in_str = ch
                        i += 1
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    i += 1
                body_end = i - 1  # index of the closing `}`
                body = text[body_start:body_end]
                if not EXPECT_RE.search(body):
                    lineno = _line_for_offset(text, body_start)
                    rel = path.relative_to(REPO_ROOT)
                    violations.append(
                        f"{rel}:{lineno}: it({name!r}) has no expect(...)"
                    )
        self.assertEqual(
            violations, [],
            "Every `it(...)` must contain at least one expect(...) "
            "(rules/test-files.md — Coverage). Add at least one assertion, "
            "or convert to `it.todo(...)` for placeholder work.\n"
            + "\n".join(violations),
        )


class TestAfterEachRestoresMocks(unittest.TestCase):
    """Rule #4: if mocks exist, `afterEach` must call `vi.restoreAllMocks()`."""

    def test_aftereach_restores_mocks(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            has_mock = bool(VI_FN_RE.search(text) or VI_MOCK_RE.search(text))
            if not has_mock:
                continue
            # Walk every afterEach(...) body and check for restoreAllMocks().
            aftereach_calls = list(AFTER_EACH_RE.finditer(text))
            if not aftereach_calls:
                rel = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}:1: uses vi.fn()/vi.mock() but no afterEach(...)"
                )
                continue
            any_restores = False
            for m in aftereach_calls:
                # Walk from m.end() to find the matching closing of afterEach's
                # body, then test that body for restoreAllMocks().
                idx = m.end()
                depth = 0
                in_str: str | None = None
                while idx < len(text):
                    ch = text[idx]
                    if in_str:
                        if ch == "\\":
                            idx += 2
                            continue
                        if ch == in_str:
                            in_str = None
                        idx += 1
                        continue
                    if ch in ("'", '"', "`"):
                        in_str = ch
                        idx += 1
                        continue
                    if ch == "(" or ch == "{" or ch == "[":
                        depth += 1
                    elif ch == ")" or ch == "}" or ch == "]":
                        depth -= 1
                        if ch == ")" and depth == 0:
                            # End of afterEach(...)
                            idx += 1
                            break
                    idx += 1
                # Now scan from `idx` until the next `=>` then `{` and the
                # matching `}` for the body. Simpler: just check the next
                # 2000 chars for the restoreAllMocks() call.
                tail = text[idx:idx + 2000]
                if RESTORE_ALL_MOCKS_RE.search(tail):
                    any_restores = True
                    break
            if not any_restores:
                rel = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}:1: uses vi.fn()/vi.mock() but no "
                    "afterEach(...) calls vi.restoreAllMocks()"
                )
        self.assertEqual(
            violations, [],
            "When vi.fn()/vi.mock() is used, afterEach must call "
            "vi.restoreAllMocks() (rules/test-files.md — Mocking).\n"
            + "\n".join(violations),
        )


class TestNoItOnlyOrSkip(unittest.TestCase):
    """Rule #5: no `it.only` / `it.skip` in committed code."""

    def test_no_it_only_or_skip(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                if IT_ONLY_RE.search(line):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: it.only("
                    )
                if IT_SKIP_RE.search(line):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: it.skip("
                    )
        self.assertEqual(
            violations, [],
            "Forbidden patterns (rules/test-files.md — Forbidden patterns): "
            "no `it.only` or `it.skip` in committed code. Use `it.todo` "
            "for unimplemented work.\n"
            + "\n".join(violations),
        )


class TestVitestOnlyImports(unittest.TestCase):
    """Rule #6: test files must import from `vitest`, not from jest-family."""

    def test_vitest_only_imports(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                for m in VITEST_IMPORT_RE.finditer(line):
                    module = m.group(1)
                    head = module.split("/", 1)[0]
                    if head.startswith("@"):
                        # Scoped package: @jest/globals, @jest/core, @ava/...
                        if head in NON_VITEST_RUNNERS or any(
                            module.startswith(r + "/") for r in NON_VITEST_RUNNERS
                        ):
                            violations.append(
                                f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                                f"non-vitest test runner import: {module!r}"
                            )
                    elif head in NON_VITEST_RUNNERS:
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                            f"non-vitest test runner import: {module!r}"
                        )
        self.assertEqual(
            violations, [],
            "Test files must import only from `vitest` "
            "(rules/test-files.md — Framework). Replace jest-family / "
            "mocha / jasmine / ava imports with the `vitest` equivalents.\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
