# Eval: SKILL Asset (judge-skill, v1.0.0)

You are reviewing a Claude Code SKILL.md file. Score 4 axes 0-10 each.

## Asset
- **Name**: ${ASSET_NAME}
- **Kind**: ${ASSET_KIND}
- **Content**:
```
${ASSET_CONTENT}
```

## Axes (0-10)
1. **semantic_drift**: How well does this skill's intent match the user's needs? (10 = perfect, 0 = contradicts user needs)
2. **completeness**: Does the SKILL.md have name, category, when_to_use, model, safety (5-field)? Are must/must-not/ac present? Are hook alignments shown?
3. **correctness**: Are the file paths, function names, hook names referenced correctly? Would this actually run?
4. **consistency**: Does this skill's framing match other skills (Iron Laws, MUST rules, naming convention ADR-0010)? Are hook alignments consistent with `.active-hooks.json` matrix?

## Output Format
ONLY a JSON object (no prose):
```json
{"semantic_drift":N,"completeness":N,"correctness":N,"consistency":N}
```
