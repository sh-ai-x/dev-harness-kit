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
from collections import namedtuple
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
IT_DOT_ONLY_OR_SKIP_RE = re.compile(r"\bit\.(?:only|skip)\s*\(")
SKIPPED_OR_ONLY_DESC_RE = re.compile(
    r"\b(xdescribe|xit|fit|fdescribe)\s*\("
)

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
# ESM `import ... from 'pkg'` — single source for the import-name match.
ESM_IMPORT_RE = re.compile(
    r"""(?<![A-Za-z0-9_])from\s+['"]([^'"]+)['"]"""
)
# CJS `require('pkg')` — closes the VM1 gap.
CJS_REQUIRE_RE = re.compile(
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""
)

# Recognise the `it('...', ...)` header — single source, reused by the
# walker + the per-test-name lints. The opening quote is captured into
# group 1 and back-referenced via `\1`, so we only match the
# call-header form (matching quotes), not bare `it` type references.
# `it.only` / `it.skip` are filtered out by `_walk_test_blocks` via the
# preceding-`.` check (the regex itself uses a non-word lookbehind so
# `it.only` would otherwise also match).
IT_HEADER_RE = re.compile(
    r"""(?<![A-Za-z0-9_])it\s*\(\s*(['"])([^'"\\]*(?:\\.[^'"\\]*)*)\1"""
)

# Rule: no emoji in test names (rules/test-files.md — Naming).
# Covers Misc Symbols & Pictographs, Emoticons, Transport & Map,
# Supplemental Symbols, Symbols & Pictographs Extended-A, Misc Symbols
# (dingbats). Catches 🎉 (U+1F389), 🔥 (U+1F525), etc.
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F6FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]"
)
# Rule: no `should` word in test names (rules/test-files.md — Naming).
# Project uses Korean; English `should ...` style is forbidden.
SHOULD_WORD_RE = re.compile(r"\bshould\b", re.IGNORECASE)

# Rule: no `setTimeout` / `sleep()` in test files.
# (rules/test-files.md — Forbidden patterns.)
SET_TIMEOUT_RE = re.compile(r"\bsetTimeout\s*\(")
SLEEP_RE = re.compile(r"(?<![\w$])(?:await\s+)?\bsleep\s*\(")

# Rule: no `expect.anything()` / `expect.any(...)` (rules/test-files.md —
# Forbidden patterns). Forces specific assertions.
EXPECT_ANYTHING_RE = re.compile(r"\bexpect\s*\.\s*anything\s*\(")
EXPECT_ANY_RE = re.compile(r"\bexpect\s*\.\s*any\s*\(")


# A single `it('name', () => { ... })` callback block.
# Named `ItBlock` (not `Test*`) so pytest doesn't try to auto-collect it.
ItBlock = namedtuple(
    "ItBlock", ("name", "body_start", "body_end", "header_offset")
)
ItBlock.__test__ = False  # tell pytest this is a value object, not a test class


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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _read_lines(path: Path) -> list[str]:
    return _read_text(path).splitlines()


def _line_for_offset(text: str, offset: int) -> int:
    """Return 1-based line number for a 0-based character offset."""
    return text.count("\n", 0, offset) + 1


def _scan_to_open_brace(text: str, start: int) -> int:
    """From `start`, scan forward through balanced parens / brackets /
    string literals to find the first `{` at depth 0. Returns the
    offset of that `{`, or -1 if none exists.
    """
    idx = start
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
                return idx
        elif ch == "(" or ch == "[":
            depth += 1
        elif ch == ")" or ch == "]":
            depth -= 1
        idx += 1
    return -1


def _match_body(text: str, open_idx: int) -> int:
    """Given the index of an opening `{`, return the index of its
    matching `}`. Tracks string literals + nested braces + parens +
    brackets so braces inside string literals don't trip it.
    """
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 1
    idx = open_idx + 1
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
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
        elif ch == "(" or ch == "[":
            depth += 1
        elif ch == ")" or ch == "]":
            depth -= 1
        idx += 1
    return -1


def _walk_test_blocks(text: str) -> Iterable[ItBlock]:
    """Yield one ItBlock per `it('name', () => { ... })` in `text`.

    Skips `it.only` / `it.skip` (covered by separate rules). Tracks
    string literals + balanced parens / brackets so braces inside names
    or comments don't fool the walker.
    """
    for m in IT_HEADER_RE.finditer(text):
        name = m.group(2)
        prefix_start = m.start()
        if prefix_start > 0 and text[prefix_start - 1] == ".":
            continue
        open_brace = _scan_to_open_brace(text, m.end())
        if open_brace < 0:
            continue
        body_end = _match_body(text, open_brace)
        if body_end < 0:
            continue
        yield ItBlock(
            name=name,
            body_start=open_brace + 1,
            body_end=body_end,
            header_offset=m.start(),
        )


def _walk_aftereach_bodies(text: str) -> Iterable[str]:
    """Yield the body slice (between `{` and matching `}`) for each
    `afterEach(() => { ... })` callback. Replaces the old 2000-char
    tail heuristic — the new walker is bound by the actual `}`.
    """
    for m in AFTER_EACH_RE.finditer(text):
        open_brace = _scan_to_open_brace(text, m.end())
        if open_brace < 0:
            continue
        body_end = _match_body(text, open_brace)
        if body_end < 0:
            continue
        yield text[open_brace + 1:body_end]


def _format_violation(path: Path, lineno: int, msg: str) -> str:
    rel = path.relative_to(REPO_ROOT)
    return f"{rel}:{lineno}: {msg}"


# ---------------------------------------------------------------------------
# Repo-wide rule checks
# ---------------------------------------------------------------------------


class TestNoJestImports(unittest.TestCase):
    """Rule #1: no `jest` import / `jest.fn()` / `jest.mock()` in TS files."""

    def test_no_jest_imports(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                for pat, reason in JEST_PATTERNS:
                    if re.search(pat, line):
                        violations.append(_format_violation(path, lineno, reason))
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
            text = _read_text(path)
            for blk in _walk_test_blocks(text):
                if not KOREAN_NAME_RE.search(blk.name):
                    lineno = _line_for_offset(text, blk.header_offset)
                    violations.append(_format_violation(
                        path, lineno,
                        f"non-Korean test name: {blk.name!r}",
                    ))
        self.assertEqual(
            violations, [],
            "Test names must be in Korean (rules/test-files.md — Naming). "
            "At least one Hangul syllable (가-힣) required.\n"
            + "\n".join(violations),
        )


class TestItHasExpect(unittest.TestCase):
    """Rule #3: every `it(...)` block must contain at least one `expect(...)`."""

    def test_it_has_expect(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            text = _read_text(path)
            for blk in _walk_test_blocks(text):
                body = text[blk.body_start:blk.body_end]
                if not EXPECT_RE.search(body):
                    lineno = _line_for_offset(text, blk.header_offset)
                    violations.append(_format_violation(
                        path, lineno,
                        f"it({blk.name!r}) has no expect(...)",
                    ))
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
            text = _read_text(path)
            has_mock = bool(VI_FN_RE.search(text) or VI_MOCK_RE.search(text))
            if not has_mock:
                continue
            bodies = list(_walk_aftereach_bodies(text))
            if not bodies:
                violations.append(_format_violation(
                    path, 1,
                    "uses vi.fn()/vi.mock() but no afterEach(...)",
                ))
                continue
            if not any(RESTORE_ALL_MOCKS_RE.search(b) for b in bodies):
                violations.append(_format_violation(
                    path, 1,
                    "uses vi.fn()/vi.mock() but no afterEach(...) calls "
                    "vi.restoreAllMocks()",
                ))
        self.assertEqual(
            violations, [],
            "When vi.fn()/vi.mock() is used, afterEach must call "
            "vi.restoreAllMocks() (rules/test-files.md — Mocking).\n"
            + "\n".join(violations),
        )


class TestNoItOnlyOrSkip(unittest.TestCase):
    """Rule #5: no `it.only` / `it.skip` / `fit` / `xit` / `fdescribe` /
    `xdescribe` in committed code.
    """

    def test_no_it_only_or_skip(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                m = IT_DOT_ONLY_OR_SKIP_RE.search(line)
                if m:
                    kind = "it.only" if m.group(0).startswith("it.only") else "it.skip"
                    violations.append(_format_violation(path, lineno, f"{kind}("))
        self.assertEqual(
            violations, [],
            "Forbidden patterns (rules/test-files.md — Forbidden patterns): "
            "no `it.only` or `it.skip` in committed code. Use `it.todo` "
            "for unimplemented work.\n"
            + "\n".join(violations),
        )

    def test_no_skipped_or_only_blocks(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                m = SKIPPED_OR_ONLY_DESC_RE.search(line)
                if m:
                    violations.append(_format_violation(
                        path, lineno, f"{m.group(1)}(",
                    ))
        self.assertEqual(
            violations, [],
            "Forbidden patterns (rules/test-files.md — Forbidden patterns): "
            "no `xdescribe` / `xit` / `fit` / `fdescribe` in committed "
            "code. Use `describe.skip` / `it.skip` ONLY during local "
            "debugging; never commit them.\n"
            + "\n".join(violations),
        )


class TestVitestOnlyImports(unittest.TestCase):
    """Rule #6: test files must import from `vitest`, not from jest-family."""

    @staticmethod
    def _is_non_vitest(module: str) -> bool:
        head = module.split("/", 1)[0]
        if head.startswith("@"):
            # Scoped package: @jest/globals, @jest/core, @ava/...
            return head in NON_VITEST_RUNNERS or any(
                module.startswith(r + "/") for r in NON_VITEST_RUNNERS
            )
        return head in NON_VITEST_RUNNERS

    def test_vitest_only_imports(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                # ESM imports: `import ... from 'pkg'`.
                for m in ESM_IMPORT_RE.finditer(line):
                    if self._is_non_vitest(m.group(1)):
                        violations.append(_format_violation(
                            path, lineno,
                            f"non-vitest test runner import: {m.group(1)!r}",
                        ))
                # CJS requires: `require('pkg')` (closes the VM1 gap).
                for m in CJS_REQUIRE_RE.finditer(line):
                    if self._is_non_vitest(m.group(1)):
                        violations.append(_format_violation(
                            path, lineno,
                            f"non-vitest test runner import: {m.group(1)!r}",
                        ))
        self.assertEqual(
            violations, [],
            "Test files must import only from `vitest` "
            "(rules/test-files.md — Framework). Replace jest-family / "
            "mocha / jasmine / ava imports (ESM `from` and CJS "
            "`require()`) with the `vitest` equivalents.\n"
            + "\n".join(violations),
        )


class TestNoEmojiInTestNames(unittest.TestCase):
    """Rule: no emoji in test names (rules/test-files.md — Naming)."""

    def test_no_emoji_in_test_names(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            text = _read_text(path)
            for blk in _walk_test_blocks(text):
                if EMOJI_RE.search(blk.name):
                    lineno = _line_for_offset(text, blk.header_offset)
                    violations.append(_format_violation(
                        path, lineno,
                        f"emoji in test name: {blk.name!r}",
                    ))
        self.assertEqual(
            violations, [],
            "No emoji in test names (rules/test-files.md — Naming). "
            "Emoji in test names is rejected across the project.\n"
            + "\n".join(violations),
        )


class TestNoShouldPrefixInItNames(unittest.TestCase):
    """Rule: no `should` word in test names (rules/test-files.md — Naming)."""

    def test_no_should_prefix_in_it_names(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            text = _read_text(path)
            for blk in _walk_test_blocks(text):
                if SHOULD_WORD_RE.search(blk.name):
                    lineno = _line_for_offset(text, blk.header_offset)
                    violations.append(_format_violation(
                        path, lineno,
                        f"'should' in test name: {blk.name!r}",
                    ))
        self.assertEqual(
            violations, [],
            "No 'should' word in test names (rules/test-files.md — Naming). "
            "The project uses Korean verb-first names "
            "(e.g. `it('빈 배열에 push하면 길이가 1이 된다')`); "
            "English `should ...` style is forbidden.\n"
            + "\n".join(violations),
        )


class TestNoSleepOrSettimeoutInTests(unittest.TestCase):
    """Rule: no `setTimeout` / `sleep()` in test files
    (rules/test-files.md — Forbidden patterns).
    """

    def test_no_sleep_or_settimeout_in_tests(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                if SET_TIMEOUT_RE.search(line):
                    violations.append(_format_violation(
                        path, lineno,
                        "setTimeout(...) forbidden in test files — use "
                        "vi.useFakeTimers() / vi.waitFor()",
                    ))
                if SLEEP_RE.search(line):
                    violations.append(_format_violation(
                        path, lineno,
                        "sleep(...) forbidden in test files — use "
                        "vi.waitFor() / vi.advanceTimersByTime()",
                    ))
        self.assertEqual(
            violations, [],
            "Forbidden patterns (rules/test-files.md — Forbidden patterns): "
            "no `setTimeout` or `sleep()` in test files. Use "
            "`vi.useFakeTimers()` / `vi.waitFor()` instead.\n"
            + "\n".join(violations),
        )


class TestNoExpectAnything(unittest.TestCase):
    """Rule: no `expect.anything()` / `expect.any(...)` in test files
    (rules/test-files.md — Forbidden patterns).
    """

    def test_no_expect_anything(self) -> None:
        violations: list[str] = []
        for path in _iter_target_files():
            for lineno, line in enumerate(_read_lines(path), start=1):
                if EXPECT_ANYTHING_RE.search(line):
                    violations.append(_format_violation(
                        path, lineno,
                        "expect.anything() is forbidden — be specific",
                    ))
                if EXPECT_ANY_RE.search(line):
                    violations.append(_format_violation(
                        path, lineno,
                        "expect.any(...) is forbidden — be specific",
                    ))
        self.assertEqual(
            violations, [],
            "Forbidden patterns (rules/test-files.md — Forbidden patterns): "
            "no `expect.anything()` or `expect.any(...)`. Be specific.\n"
            + "\n".join(violations),
        )


# ---------------------------------------------------------------------------
# Primitive-level unit tests for the helpers themselves (CC7).
# These tests guard the walker from future refactors — they assert on
# minimal in-memory fixtures, NOT on repo-wide scans (which would be
# tautological: the linter only reports what the walker finds).
# ---------------------------------------------------------------------------


class TestHelperWalkTestBlocks(unittest.TestCase):
    """`_walk_test_blocks(text)` returns one ItBlock per
    `it('name', () => { ... })`, with body offsets spanning the arrow
    body exactly.
    """

    def test_single_it_block_yields_one_block(self) -> None:
        text = (
            "import { it } from 'vitest';\n"
            "it('한글 동작', () => {\n"
            "  expect(true).toBe(true);\n"
            "});\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].name, "한글 동작")
        body = text[blocks[0].body_start:blocks[0].body_end]
        self.assertIn("expect(true).toBe(true);", body)

    def test_multiple_it_blocks_yielded_in_order(self) -> None:
        text = (
            "it('첫 번째', () => { expect(1).toBe(1); });\n"
            "it('두 번째', () => { expect(2).toBe(2); });\n"
            "it('세 번째', () => { expect(3).toBe(3); });\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 3)
        self.assertEqual(
            [b.name for b in blocks],
            ["첫 번째", "두 번째", "세 번째"],
        )

    def test_it_only_and_it_skip_are_skipped(self) -> None:
        text = (
            "it.only('only', () => { expect(1).toBe(1); });\n"
            "it.skip('skip', () => { expect(2).toBe(2); });\n"
            "it('keep', () => { expect(3).toBe(3); });\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual([b.name for b in blocks], ["keep"])

    def test_nested_braces_are_balanced(self) -> None:
        text = (
            "it('중첩', () => {\n"
            "  const obj = { a: { b: 1 } };\n"
            "  expect(obj.a.b).toBe(1);\n"
            "});\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 1)
        body = text[blocks[0].body_start:blocks[0].body_end]
        self.assertIn("obj.a.b", body)

    def test_braces_inside_string_literals_are_ignored(self) -> None:
        text = (
            "it('이름에 {중괄호} 있음', () => {\n"
            "  expect('hello {world}').toBe('hello {world}');\n"
            "});\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].name, "이름에 {중괄호} 있음")
        body = text[blocks[0].body_start:blocks[0].body_end]
        self.assertIn("expect('hello {world}')", body)

    def test_backtick_template_literals_respected(self) -> None:
        text = (
            "it('템플릿', () => {\n"
            "  const s = `hello ${1 + 2} world`;\n"
            "  expect(s).toBe('hello 3 world');\n"
            "});\n"
        )
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 1)
        body = text[blocks[0].body_start:blocks[0].body_end]
        self.assertIn("const s = `hello ${1 + 2} world`;", body)

    def test_header_offset_points_at_it_keyword(self) -> None:
        text = "  it('시작', () => { expect(1).toBe(1); });\n"
        blocks = list(_walk_test_blocks(text))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(text[blocks[0].header_offset:blocks[0].header_offset + 2], "it")


class TestHelperMatchBody(unittest.TestCase):
    """`_match_body(text, open_idx)` returns the offset of the matching
    `}` for a given opening `{`.
    """

    def test_simple_body(self) -> None:
        text = "{ a; b; }"
        self.assertEqual(_match_body(text, 0), text.index("}"))

    def test_nested_body(self) -> None:
        text = "{ outer { inner } outer }"
        # Outer `{` is at index 0, matching `}` is the last char.
        self.assertEqual(_match_body(text, 0), len(text) - 1)

    def test_unmatched_returns_minus_one(self) -> None:
        text = "{ unterminated"
        self.assertEqual(_match_body(text, 0), -1)

    def test_non_brace_open_idx_returns_minus_one(self) -> None:
        text = "not a brace"
        self.assertEqual(_match_body(text, 0), -1)

    def test_braces_inside_string_are_ignored(self) -> None:
        text = "{ a; 'has } inside'; b; }"
        # Opening `{` at index 0, matching `}` at the very end.
        self.assertEqual(_match_body(text, 0), len(text) - 1)


class TestHelperScanToOpenBrace(unittest.TestCase):
    """`_scan_to_open_brace(text, start)` walks balanced parens /
    brackets / string literals and returns the first `{` at depth 0.
    """

    def test_finds_brace_through_arrow_arg_list(self) -> None:
        # Starting just after the comma in `it('name',` we walk through
        # the arrow's `(...)` arg list and find the body `{`.
        text = "it('foo', (a, b) => { expect(a).toBe(b); });"
        open_brace = _scan_to_open_brace(text, text.index(",") + 1)
        self.assertEqual(open_brace, text.index("{"))

    def test_skips_braces_inside_string_literals(self) -> None:
        text = "('not a { brace', () => { body })"
        # Start at index 1 (just past the outer `(`).
        # The first `{` at depth 0 should be the body's, not the
        # one inside the string.
        open_brace = _scan_to_open_brace(text, 1)
        self.assertEqual(open_brace, text.rindex("{"))

    def test_no_open_brace_returns_minus_one(self) -> None:
        text = "(a, b) => no_body_arrow"
        self.assertEqual(_scan_to_open_brace(text, 1), -1)


class TestHelperLineForOffset(unittest.TestCase):
    """`_line_for_offset(text, offset)` is 1-based."""

    def test_first_line(self) -> None:
        self.assertEqual(_line_for_offset("abc\ndef", 0), 1)

    def test_second_line(self) -> None:
        self.assertEqual(_line_for_offset("abc\ndef", 4), 2)

    def test_third_line(self) -> None:
        self.assertEqual(_line_for_offset("a\nb\nc", 4), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
