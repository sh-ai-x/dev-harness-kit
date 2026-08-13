# Security Metrics

- Repository: `/Users/sanghee/dev/dev-harness-kit`
- Overall score: **83/100**

| OWASP area | Score | Status | Evidence / deductions |
|---|---:|---|---|
| A01 Broken Access Control | 100/100 | PASS | No deterministic findings |
| A02 Security Misconfiguration | 80/100 | REVIEW | -20: possible hardcoded credential pattern |
| A03 Software Supply Chain Failures | 75/100 | REVIEW | -15: GitHub Action is not pinned to a commit SHA<br>-10: no recognized dependency lockfile |
| A04 Cryptographic Failures | 80/100 | REVIEW | -20: weak hash call detected |
| A05 Injection | 35/100 | REVIEW | -25: dynamic code evaluation pattern detected<br>-20: shell=True detected<br>-20: SQL-like interpolated query detected |
| A06 Insecure Design | 100/100 | PASS | No deterministic findings |
| A07 Authentication Failures | 100/100 | PASS | No deterministic findings |
| A08 Software/Data Integrity Failures | 75/100 | REVIEW | -25: network content piped to a shell |
| A09 Security Logging and Alerting Failures | 100/100 | PASS | No deterministic findings |
| A10 Mishandling Exceptional Conditions | 85/100 | REVIEW | -15: bare exception handler detected |

> This is a deterministic triage metric, not a security certification. Run `/dev-kit:security` for the full OWASP evidence review.
