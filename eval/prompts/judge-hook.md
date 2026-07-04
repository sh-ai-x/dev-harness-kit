# Eval: Hook Script Asset (judge-hook, v1.0.0)

You are reviewing a Claude Code hook shell script.

## Asset
- **Name**: ${ASSET_NAME}
- **Kind**: ${ASSET_KIND}
- **Content**:
```
${ASSET_CONTENT}
```

## Axes (0-10)
1. **semantic_drift**: Does the hook's purpose match its name? (e.g., tdd-guard enforces TDD, slop-detector detects slop)
2. **completeness**: Has shebang, set -eo, JSON input parsing (jq), conditional fail/warn mode, exit 0 default? Has both --strict and default paths?
3. **correctness**: jq usage syntactically correct? Matches `hooks.json` event/match? Works on macOS + Linux (BSD vs GNU grep)?
4. **consistency**: Error messages reference Iron Law #N when applicable? Default exit 0 (MUST-12)? Uses `${CLAUDE_PLUGIN_ROOT}` portable path?

## Output Format
ONLY a JSON object (no prose):
```json
{"semantic_drift":N,"completeness":N,"correctness":N,"consistency":N}
```
