# Coding Guidelines (behavioral)

> Abbreviated from andrej-karpathy-skills. Merge with project Iron Laws.
> Tradeoff: bias toward caution over speed. Use judgment for trivial tasks.

- **G1**: State assumptions explicitly. Surface tradeoffs. Ask when uncertain. If multiple interpretations exist, present them. If a simpler approach exists, say so.
- **G2**: Minimum code that solves the problem. No speculative features. No abstractions for single-use code. No error handling for impossible scenarios. If 200 lines could be 50, rewrite.
- **G3**: Touch only what you must. Match existing style. Don't refactor unrelated code. Every changed line should trace to the user's request.
- **G4**: Define success criteria before coding. Loop until verified. Strong criteria enable independent iteration. Weak criteria require constant clarification.