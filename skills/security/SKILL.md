---
name: security
category: security
description: Full OWASP Top 10 2025 fan-out (A01–A10) with a verifier pass. Ten parallel subagents, one per category, return evidence-backed findings; a verification pass confirms or rejects each before a per-category breakdown table + verdict. Use when the user types /dev-kit:security, or asks for a security audit / OWASP scan / pre-release security check.
when_to_use: |
  - User types /dev-kit:security
  - Pre-release / quarterly / before major refactor
  - User asks for a security audit, OWASP review, or vulnerability scan
allowed-tools: Read Grep Glob Bash Agent
model: opus
disable-model-invocation: false
---

## Provider (defaults to MiniMax, Anthropic-compatible)

This skill follows whatever provider the GitHub Action configures via env vars.
Default is **MiniMax-M3[1m]** at `ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`.
Opt-in to real Claude Code via `REVIEW_PROVIDER=anthropic`.

# /dev-kit:security — OWASP Top 10 audit (10 dimensions, parallel fan-out)

Spawns 10 subagents in parallel — one per OWASP A01-A10 category. This is a **separate
fan-out** from `/dev-kit:review` (different dimensions, deeper security focus).

Categories:
- **A01** Broken Access Control — IDOR / path traversal / force browse / CORS / missing function-level / privilege escalation
- **A02** Security Misconfiguration — default creds / debug-in-prod / stack traces / missing headers / cloud metadata SSRF / verbose errors
- **A03** Software Supply Chain Failures — vulnerable deps / unpinned versions / untrusted registries / build injection / postinstall / typosquats
- **A04** Cryptographic Failures — weak hashes (MD5/SHA1) / non-constant-time compare / hardcoded keys / insecure RNG / ECB / small keys / TLS verify off
- **A05** Injection — SQL / command / template / XSS / NoSQL / header / XXE / format string
- **A06** Insecure Design — no rate limit / client-side-only trust / predictable IDs / TOCTOU / missing CSRF / missing business rules
- **A07** Authentication Failures — weak passwords / credential stuffing / session fixation / plaintext storage / password-in-URL / JWT alg none
- **A08** Software/Data Integrity Failures — unsafe deserialization / auto-update w/o integrity / insecure plugin load / missing checksum / cookie flags
- **A09** Security Logging and Alerting Failures — missing auth logs / PII in logs / no alerting / mutable logs / insufficient detail
- **A10** Mishandling Exceptional Conditions — bare except pass / fail-open auth / fail-open validation / missing timeout / unhandled rejections / missing cleanup / panic in critical path

## Fan-out

> **Issue all 10 `Agent` calls inside ONE assistant message** so they run concurrently.

Each subagent uses the **same shared contract as `/dev-kit:review`** (per finding:
`failure_scenario` + `confidence`; precision over recall; return a fenced ```json array).

## Verifier

Single pass: RE-READ cited code and try hard to REFUTE each candidate.
CONFIRMED | PLAUSIBLE | REJECTED. Drop every REJECTED.

## Output

```
## Security summary

**Verdict:** <Blocked | Changes Requested | Approve>

| Category | Findings | Severity |
|---|---|---|
| A01 | n | (🔴 critical, ...) |
...
```

CONFIRMED ≥ 5 → Approve. 0-2 → Blocked. Inline comments per finding use the
**same Layer 1 format as `/dev-kit:review`**.

## Hook 정렬

Review와 동일 (`slop-detector, secret-scan, stop-verify` ON).

## 다음 단계

`/dev-kit:review` 또는 `/dev-kit:ship`.
