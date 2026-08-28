---
name: security
category: security
description: Security fan-out — OWASP Top 10 2025 (A01–A10) plus a separate LLM01 Prompt Injection dimension. Eleven parallel subagents, one per category, return evidence-backed findings; a verification pass confirms or rejects each before a per-category breakdown table + verdict.
alpha: enforcement
when_to_use:
  - User types /dev-kit:security
  - Pre-release / quarterly / before major refactor
  - User asks for a security audit, OWASP review, or vulnerability scan
allowed-tools: Read Grep Glob Bash Agent
model: opus
disable-model-invocation: false
---
> [← Skills index](../../README.md)

OWASP Top 10 audit (10 dimensions, parallel fan-out). Delegates to `lib.analysis_core.run_analysis(dimensions=group("security"), mode="read-only", paths=...)`. Separate from `/dev-kit:review` (different dims, deeper security focus).

## Categories

- **A01** Broken Access Control — IDOR, path traversal, force browse, CORS, missing function-level checks, privilege escalation.
- **A02** Security Misconfiguration — default creds, debug-in-prod, stack traces, missing headers, cloud metadata SSRF, verbose errors.
- **A03** Software Supply Chain Failures — vulnerable deps, unpinned versions, untrusted registries, build injection, postinstall, typosquats.
- **A04** Cryptographic Failures — weak hashes (MD5/SHA1), non-constant-time compare, hardcoded keys, insecure RNG, ECB, small keys, TLS verify off.
- **A05** Injection — SQL, command, template, XSS, NoSQL, header, XXE, format string.
- **A06** Insecure Design — no rate limit, client-side-only trust, predictable IDs, TOCTOU, missing CSRF, missing business rules.
- **A07** Authentication Failures — weak passwords, credential stuffing, session fixation, plaintext storage, password-in-URL, JWT alg none.
- **A08** Software/Data Integrity Failures — unsafe deserialization, auto-update w/o integrity, insecure plugin load, missing checksum, cookie flags.
- **A09** Security Logging and Alerting Failures — missing auth logs, PII in logs, no alerting, mutable logs, insufficient detail.
- **A10** Mishandling Exceptional Conditions — bare except pass, fail-open auth/validation, missing timeout, unhandled rejections, missing cleanup, panic in critical path.

> OWASP Top 10 stays at A01–A10. Prompt Injection (LLM01) is a **separate dimension** (not A11) — see the per-dim charter in `lib.analysis_core.dimensions._PROMPT_injection`. The static pre-filter at `tools/prompt_injection_scan.py` covers the regex-shaped families; the fan-out handles semantic / context-shaped variants.

## Fan-out + verify

Issue all 11 Agent calls (10 OWASP + 1 prompt-injection) inside ONE assistant message so they run concurrently. Each: `subagent_type: "general-purpose"`, `model: "sonnet"`. Pass each expert its charter from `lib.analysis_core.dimensions` + the shared contract (same shape as `/dev-kit:review`: `file, line, severity, confidence, failure_scenario, title, tldr`).

One verifier Agent returns `[{id, verdict: CONFIRMED|PLAUSIBLE|REJECTED, reason}]`. Drop REJECTED; keep CONFIRMED + PLAUSIBLE.

The skill body owns the dedupe (on `file,line,theme`) + verifier + synthesize pipeline inline — the Agent calls return raw findings inside one assistant message, and the body collapses duplicates, applies the verifier verdict, and synthesizes the per-category breakdown table.

## Output

```
## Security summary
**Verdict:** <Blocked | Changes Requested | Approve>
| Category | Findings | Severity |
|---|---|---|
| A01 | n | (🔴 critical, ...) |
...
```

CONFIRMED >= 5 -> Approve. 0-2 -> Blocked. (For the prompt-injection dimension, a single Critical hit flips to Blocked — the LLM01 surface is fail-closed by default.) Inline comments per finding use the same Layer 1 format as `/dev-kit:review`.

## Hooks

Same as Review (`slop-detector, secret-scan, stop-verify` ON).

Next: `/dev-kit:review` or `/dev-kit:ship`.
