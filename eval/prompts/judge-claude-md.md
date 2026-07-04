# Eval: CLAUDE.md Asset (judge-claude-md, v1.0.0)

You are reviewing a CLAUDE.md file (dev-harness-kit codebase map + Iron Laws SSOT).

## Asset
- **Name**: ${ASSET_NAME}
- **Kind**: ${ASSET_KIND}
- **Content**:
```
${ASSET_CONTENT}
```

## Axes (0-10)
1. **semantic_drift**: Does §1 (Iron Laws) match the canonical 5 laws? L1=No-Test-No-Code, L2=Root-Cause-First, L3=Evidence-Before-Done, L4=No-Stub, L5=Lean-Output. Any deviation = lower score.
2. **completeness**: Does it have §1 (Iron Laws), §2 (Active Stage), §3 (Codebase Map), §4 (Hook Matrix), §5 (Hand-off)? AUTO-GENERATED marker present?
3. **correctness**: §4 Hook Matrix matches `.dev-kit/.active-hooks.json`? §2 stage matches `.dev-kit/state.json`?
4. **consistency**: §1 wording matches `lib/write_claude_md.py:IRON_LAWS`? Path conventions match NAMING.md (ADR-0010)?

## Output Format
ONLY a JSON object (no prose):
```json
{"semantic_drift":N,"completeness":N,"correctness":N,"consistency":N}
```
