# Slop Judge (judge-slop, v1.0.0)

Used by `hooks/slop-detector.sh` (PostToolUse) via
`lib/llm_judge.py:call_judge(dim="slop")` to score a freshly-written
file for LLM-tells. Replaces the v2 regex tier ladder
(`hooks/references/slop/{phrases,structures}.md` + inline Python
`re.finditer`) — the regex bank drifted silently with model
improvements; the LLM judge stays aligned to current prose norms
without per-bank curation.

## Inputs

- `${CONTENT}` — the full file body the assistant just wrote/edited.
  Multi-edit payloads are concatenated by `hooks/lib/payload-parse.sh::extract_content`
  before being passed in.

## Axis (0-10; higher = cleaner prose)

- `slop_score` — cleanliness of the prose. Penalize the same AI-tells
  the v2 regex bank used to catch, but judge them semantically rather
  than by exact phrase match:

    | Tell | Examples |
    |---|---|
    | Throat-clearing openers | "Certainly!", "Great question", "I'd be happy to" |
    | Marketing jargon | robust, comprehensive, leverage, unleash, empower, seamlessly, game-changer |
    | Lazy extremes | "always", "never", "every single", "absolutely critical" |
    | Three-item lists used as a crutch | "Fast. Cheap. Reliable." with no real claim |
    | Em-dash density | more than one em-dash per 200 chars |
    | False agency | "we strive to ensure", "the team endeavors" |
    | Wh-starter emphasis | "What makes this hard is…", "When in doubt, we ship" |
    | Binary-contrast framing | "It's not X. It's Y." as a paragraph opener |
    | KO crutches | 종합적인, 강력한, 다양한, 핵심적으로, 시시각각 |

  Score 10 if the prose has none of these. Score 7-9 for minor
  residual tells. Score 4-6 for moderate slop (multiple instances of
  2-3 categories). Score 0-3 for heavy slop that reads like
  unedited LLM output.

## Output format

Respond ONLY with a single JSON object:

```json
{
  "slop_score": <0-10>,
  "reason": "<one short sentence naming the worst tell>"
}
```

No prose before or after. The `reason` string surfaces in the hook's
stderr advisory — keep it under 100 chars.