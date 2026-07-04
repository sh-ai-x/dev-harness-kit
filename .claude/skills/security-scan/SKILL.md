---
name: security-scan
description: Full OWASP Top 10 2025 fan-out (A01–A10) with verifier pass. Per-category breakdown table. Trigger via /security-scan in /dev-kit.
when_to_use: |
  - User types /dev-kit:security OR /security-scan
  - Pre-release / quarterly / before major refactor
allowed-tools: Read Grep Glob Bash Agent
disable-model-invocation: false
model: opus
---

# security-scan — OWASP Top 10 audit (10 dimensions, parallel fan-out)

Spawns 10 subagents in parallel — one per OWASP A01-A10 category.

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

Each subagent uses the same shared contract as `/review-code` (failure_scenario + confidence).

## Verifier

Single pass: REFUTE each candidate. CONFIRMED | PLAUSIBLE | REJECTED.

## Output

```
## Security summary

**Verdict:** <Blocked | Changes Requested | Approve>

| Category | Findings | Severity |
|---|---|---|
| A01 | n | (🔴 critical, ...) |
...
```

CONFIRMED ≥ 5 → Approve. 0-2 → Blocked.

Inline comments per finding (same Layer 1 format as review-code).
