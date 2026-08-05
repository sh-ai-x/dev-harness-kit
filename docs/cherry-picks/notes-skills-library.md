# Deep-read notes — skills-library reference category

> Source category: pure skills library. Multi-runtime packaging. Single
> SessionStart hook injects a strong Iron Law. Read once for pattern
> extraction only — do not vendor, do not import text.

## Surface observed

- ~14 SKILL.md files. Frontmatter schema: `name` + `description` only.
- `description:` field is a natural-language trigger phrase — model matches
  user prompts to descriptions for auto-invocation.
- One SessionStart hook that reads one specific skill file and injects it
  as `additionalContext` for every fresh session. Static text, no state.
- Multi-runtime packaging from a single source dir to 9+ harnesses via
  separate per-runtime manifests kept in lockstep.
- v5→v6 had 6 breaking renames in 12 months: slash commands removed,
  worktree path migrated, prompt files renamed, manifest format
  tightened. Strong signal that pinning is mandatory.
- Long-lived localhost HTTP+WS server accompanies one of the skills
  (visual companion); writes PID files for ownership tracking; **does not
  survive tmux detach cleanly** — server can orphan.
- No CI / worktree enforcement / version-bump opinion in the project.

## Patterns worth extracting (without copying text)

| Pattern | Notes for cherry-pick |
|---|---|
| `description:` as trigger phrase | Strong authoring principle: write descriptions that *fire* on the prompt pattern you want. Candidate for hardening `rules/skill-authoring.md`. |
| Subagent task-brief + review-package + 5-round circuit breaker | Mature pattern for parallel subagent dispatch with self-correction. Candidate for cherry-pick #2 — would tighten the path the dispatch classifier chose to parallelize. |
| Single hook that injects a static bootstrap | Idempotent design (no files, no sockets, no daemons). Useful as a template for the dev-kit reference-override hook. |
| Multi-runtime packaging | Out of mission scope for dev-kit. Not a cherry-pick. |
| Localhost visual-companion server | **Reject.** Conflicts with tmux + thin discipline (server orphan risk). |

## What NOT to cherry-pick

- The auto-invocation meta-skill itself (would inject every session, conflicts
  with iron law L8 — prose that duplicates state-machine behavior must be
  trimmed).
- The localhost HTTP/WS server.
- The 9-runtime packaging overhead.
- v5→v6-style frequent renames — implies pinning is mandatory.
