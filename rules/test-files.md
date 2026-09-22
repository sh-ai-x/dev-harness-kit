---
paths:
  - "**/*.test.ts"
  - "**/*.test.tsx"
  - "**/*.spec.ts"
  - "**/tests/**/*.test.ts"
  - "**/tests/**/*.test.tsx"
  - "**/__tests__/**/*.test.ts"
  - "**/__tests__/**/*.test.tsx"
stale_after: 2027-08-19
---

# Test file authoring rules

These rules apply when writing any test file (unit, integration, e2e).

## Framework (mandatory)

- **vitest only**. `jest` is forbidden.
- Use `vi.fn()`, `vi.mock()`, `vi.useFakeTimers()` — never `jest.fn()` etc.
- `describe` + `it` (or `test`) — no `xdescribe`, `xit`, `fit`, `fdescribe`.

## Pattern (mandatory)

```ts
import { describe, it, expect } from 'vitest';

describe('<unit or feature under test>', () => {
  it('<한글 동작 설명>', () => {
    // arrange
    // act
    // assert
  });
});
```

## Naming (mandatory)

- Test names: **Korean** for behavior, lowercase, no period. Example: `it('빈 배열에 push하면 길이가 1이 된다')`.
- `describe` block name: subject under test. Example: `describe('Array.prototype.push', ...)`.
- File name: `<source-file>.test.ts` next to the source (or co-located in `__tests__/`).
- No emoji in test names.
- No `should` prefix — start with the verb. (`it('동작한다')`, not `it('should 동작한다')`).

## Independence (mandatory)

- **Each `it` MUST run in isolation.** No shared mutable state across tests.
- Setup/teardown per test: `beforeEach` / `afterEach` — not module-level.
- No test should depend on the order of execution.
- No relying on global state (e.g. `Date.now()`, `Math.random()`) — inject or stub.

## Coverage (mandatory)

- Every `it` must contain ≥ 1 `expect(...)` assertion.
- Every exported function/class in the source MUST have ≥ 1 test.
- Test the **failure path** as well as the happy path. If the function throws on bad input, write a test that confirms the throw.

## Mocking

- Mock at the module boundary, not internal helpers.
- `vi.mock('./module')` at the top of the file, BEFORE imports of the SUT.
- Prefer real implementations over mocks when the real impl is fast and pure.
- Reset all mocks in `afterEach`: `vi.restoreAllMocks()`.

## Forbidden patterns

- ❌ `it.only`, `it.skip` left in committed code (use `it.todo` for unimplemented).
- ❌ `expect.anything()` or `expect.any(String)` as a catch-all — be specific.
- ❌ `setTimeout` / `await sleep(...)` in tests — use `vi.useFakeTimers()` or `vi.waitFor()`.
- ❌ Testing implementation details (private methods, internal state) — test behavior.
- ❌ One giant `it('does everything')` — split into focused tests.

## Reference example

```ts
import { describe, it, expect } from 'vitest';
import { parseEmail } from './email';

describe('parseEmail', () => {
  it('유효한 이메일을 그대로 정규화한다', () => {
    expect(parseEmail('User@Example.com')).toBe('user@example.com');
  });

  it('@가 없으면 null을 반환한다', () => {
    expect(parseEmail('not-an-email')).toBeNull();
  });

  it('빈 문자열은 null을 반환한다', () => {
    expect(parseEmail('')).toBeNull();
  });
});
```
