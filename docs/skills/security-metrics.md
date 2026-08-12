# `security-metrics`

Generate a deterministic 0-100 scorecard for OWASP Top 10 areas.

```bash
python3 skills/security-metrics/scripts/score_security.py . \
  --output security-metrics.md
```

This is a lightweight local metric for Claude Code and Codex. It does not
replace the evidence-backed `/dev-kit:security` audit, install external
scanners, or run project commands.
