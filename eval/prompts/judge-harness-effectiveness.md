# Harness effectiveness evidence review

This prompt is a review aid for the deterministic report generated from
`.dev-kit/trace/events.jsonl`. Do not reconstruct events from prose, terminal
transcripts, or inferred intent.

For each component, inspect the listed evidence event IDs and verify:

1. the event schema is complete and the event is attributable to a run and workflow;
2. the subject and parent/timestamp ordering supports the claimed causal chain;
3. denominators include every eligible event, including failures and retries;
4. missing evidence is reported as `INSUFFICIENT_EVIDENCE`, not silently scored;
5. the displayed score matches the reducer's numerator, denominator, and formula.

Return findings only. The reducer is authoritative for numeric scores; an LLM
must not replace or average them.
