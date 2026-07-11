# design.md — Cross-agent skill harness (Claude Code · Codex CLI · MiniMax)

> Scope: how the 42 canonical `skills/<name>/SKILL.md` definitions run across Claude Code, OpenAI Codex CLI, and MiniMax — without forking the skill source. Design only in this document; `.codex-plugin/plugin.json` itself ships alongside it.

## 1. Problem statement

The dev-kit skills are authored once as Claude Code plugin skills and invoked as `/dev-kit:<name>`. We want the same skill definitions usable from Codex CLI, and "compatible with MiniMax" too. Verified against two first-party sources — OpenAI's own [`openai/plugins`](https://github.com/openai/plugins) repo and the real, widely-adopted [`obra/superpowers`](https://github.com/obra/superpowers) skills framework, which ships to both Claude Code and Codex CLI from one repo — the answer is simpler than a mirror or a compiler: Codex CLI's official plugin manifest (`.codex-plugin/plugin.json`) has a `"skills"` field that **points at a skills directory by path**. It does not require a per-tool copy. So "compatible with all three" concretely means: keep `skills/` as the single directory, add one manifest file (`.codex-plugin/plugin.json`) that points `"skills"` at it, and reach MiniMax by pointing either harness's existing model config at MiniMax's endpoint — no generated artifacts, no symlink tree, no compiler.

## 2. Current state per runtime

| Runtime | Skill system today | Cross-tool artifacts in this repo |
|---|---|---|
| **Claude Code** | Full: 42 `skills/<name>/SKILL.md`, `/dev-kit:<name>` slash commands, `hooks/hooks.json`, `.claude-plugin/{plugin.json,marketplace.json}` | Canonical source (native) |
| **Codex CLI** | Official plugin format: `.codex-plugin/plugin.json` with a `"skills"` path field, installed via a plugin marketplace (`/plugins` in-CLI search, or `/plugin marketplace add <repo>`-style registration) | `.codex/hooks.json` = `{"hooks":{"Stop":[]}}` only; `AGENTS.md` = 1-line pointer to `CLAUDE.md`; no `.codex-plugin/` yet |
| **MiniMax** | None — model backend, not a harness; consumed via base-URL override inside Claude Code or Codex | Zero references in repo (grep-confirmed) |

## 3. Research findings (sourced)

### 3.1 Codex CLI's real plugin mechanism

Confirmed directly from OpenAI's own plugin repo, not a third-party source:

- **`openai/plugins`** ([repo](https://github.com/openai/plugins)) — "Each plugin lives under `plugins/<name>/` with a required `.codex-plugin/plugin.json` manifest and optional companion surfaces such as `skills/`, `.app.json`, `.mcp.json`, plugin-level `agents/`, `commands/`, `hooks.json`, `assets/`." The marketplace itself is `.agents/plugins/marketplace.json`, a list of `{name, source: {source: "local", path: "./plugins/<name>"}, policy, category}` entries — i.e. Codex's plugin *marketplace* format, parallel in spirit to Claude Code's own `.claude-plugin/marketplace.json`.
- **`obra/superpowers`** ([repo](https://github.com/obra/superpowers)) is the concrete proof this pattern works in production: one repo, one shared top-level `skills/` directory (14 skills, e.g. `skills/brainstorming/`, `skills/test-driven-development/`), and a **separate thin manifest per runtime** — `.claude-plugin/{plugin.json,marketplace.json}`, `.codex-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.kimi-plugin/plugin.json` — each just metadata plus `"skills": "./skills/"`. No per-tool copy of any skill file exists anywhere in that repo. Its README documents real installs for Claude Code, Codex App, **Codex CLI** (`/plugins` → search "superpowers" → install), Cursor, Kimi Code, and others, confirming this manifest shape is what actually gets consumed by each CLI's real install flow, not a theoretical schema.
- The `.codex-plugin/plugin.json` schema in practice: `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `skills` (path string), `hooks` (object, can be empty), and an optional `interface` block (`displayName`, `shortDescription`, `longDescription`, `developerName`, `category`, `capabilities`, plus optional UI/branding fields this repo has no assets for and so omits). No transformation of the pointed-to `SKILL.md` files is implied or required.
- Per-runtime tool-name mapping, where it's needed at all, lives in the *manifest*, not in per-skill files: Kimi's `.kimi-plugin/plugin.json` carries a `skillInstructions` string that tells the model "when a skill says `Task tool`, use Kimi's `Agent` tool; when it says `TodoWrite`, use `TodoList`" etc. Codex's own `.codex-plugin/plugin.json` in superpowers carries **no such field** — Codex needs no translation layer for the tool names dev-kit's skills already use.

### 3.2 Superseded finding (kept for record, not acted on)

An earlier pass of this document, based on a single blog post about a proposed "Agent Skills open standard" (`.agents/skills/` discovery, `agents/openai.yaml` sidecar), recommended generating a `.agents/skills/dev-kit-<name>/` symlink per skill plus a sidecar file for invocation-policy overrides. That mechanism may exist as a supplementary discovery path, but it is not what real, shipping Codex-compatible plugins (superpowers) or OpenAI's own plugin repo document as the install-and-use path. It added a sync script, a new skill, a test file, and 42 generated directories to solve a problem that a single static manifest already solves. It has been reverted from this repo in favor of §4 below — evidence from a first-party repo and OpenAI's own plugin registry outranks a single secondary source.

### 3.3 MiniMax

MiniMax ships **no skill/plugin harness of its own for consuming skills.** MiniMax-M2/M2.1/M2.5/M2.7 expose both an **OpenAI-compatible** (`/chat/completions`) and an **Anthropic-compatible** (`/messages`) endpoint, and integration is documented as a base-URL + API-key swap: Claude Code points `~/.claude/settings.json` at MiniMax's Anthropic-compatible endpoint; Codex points `.codex/config.toml` at the OpenAI-compatible endpoint with `MINIMAX_API_KEY`. MiniMax's own `MiniMax-AI/skills` repo is itself a *consumer* of the SKILL.md format (installable into Claude Code, Cursor, Codex, OpenCode), further confirming SKILL.md — not a MiniMax-specific format — is the shared language. **"MiniMax compatibility" = run the existing dev-kit skills inside Claude Code or Codex while that harness's model backend is MiniMax. Zero new artifact.**

## 4. Recommended architecture

**One canonical `skills/` directory. One new static manifest file. No copies, no generation, no build step.**

```
skills/<name>/SKILL.md          # CANONICAL, unchanged (Claude Code native format)
.claude-plugin/plugin.json      # existing — Claude Code manifest (no "skills" pointer needed;
                                 #   Claude Code discovers skills/ by convention)
.codex-plugin/plugin.json       # NEW — Codex CLI manifest: "skills": "./skills/"
```

### 4.1 What changed

- **`.codex-plugin/plugin.json`** (new file, repo root): mirrors the fields already present in `.claude-plugin/plugin.json` (name, version, description, author, homepage, repository, license, keywords) plus `"skills": "./skills/"` and a minimal `interface` block. Codex CLI reads the pointed-to directory directly — the exact same 42 `SKILL.md` files Claude Code reads, byte-for-byte, with no transformation step.
- **Nothing under `skills/` changes.** No new frontmatter field, no per-skill classification needed. Codex needs no `allow_implicit_invocation` override and no tool-name mapping for dev-kit's skills (unlike Kimi, which needed a `skillInstructions` block — Codex's own tool names already line up closely enough that superpowers ships no such field in `.codex-plugin/plugin.json`, and dev-kit's skills follow the same Claude-native tool vocabulary superpowers' skills do).
- **No new skill, no new test-suite surface, no new library module.** `SKILL_COUNT` in `tests/test_smoke.py` stays 42.

### 4.2 Distribution gap (stated honestly, not hidden)

Being schema-correct is necessary but not sufficient for end-user installability. Superpowers' Codex CLI installs work because it is either registered in OpenAI's curated `openai/plugins` marketplace or reachable through one obra maintains; dev-kit is not currently listed there. Concretely:

- Adding `.codex-plugin/plugin.json` makes dev-kit **plugin-schema-correct** for Codex today. Anyone who clones the repo and points a Codex CLI plugin-dir/marketplace mechanism at it (mirroring how Claude Code's `--plugin-dir` or a self-hosted `.claude-plugin/marketplace.json` already work for this repo) gets the same 42 skills Codex-side.
- Whether Codex CLI supports registering an arbitrary GitHub repo as a marketplace source the way Claude Code's `/plugin marketplace add owner/repo` does, versus requiring submission to the curated `openai/plugins` list, was not confirmed in this pass — the README for `openai/plugins` documents the curated list and superpowers' own install instructions route through it, but does not document a self-serve arbitrary-repo path. This is a real open question for actual Codex-side installability, tracked in §6, not papered over.

### 4.3 What gets exposed for MiniMax

Nothing is generated. The deliverable is a documented, copy-pasteable config recipe (README, not an artifact):

- **Via Claude Code:** point `~/.claude/settings.json` `env` (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`) at MiniMax's Anthropic-compatible `/messages` endpoint. The 42 canonical skills run unchanged with MiniMax as the model.
- **Via Codex:** set `.codex/config.toml` model/base-URL to MiniMax's OpenAI-compatible endpoint + `MINIMAX_API_KEY`; `.codex-plugin/plugin.json`'s skills are what Codex loads regardless of which model backend is configured.

There is no MiniMax plugin format to target — inventing one would be dead weight.

### 4.4 Permission and classification mapping

| Canonical (Claude Code) | Codex CLI | MiniMax |
|---|---|---|
| `disable-model-invocation`, `user-invocable` | No equivalent field in `.codex-plugin/plugin.json`; not needed — superpowers ships no per-skill Codex override either | inherits the harness it runs in |
| `allowed-tools` / `disallowed-tools` | No per-skill equivalent found in the Codex plugin schema; permissioning is a Codex session/sandbox concern, not a manifest field | inherits |
| `model:` (opus/sonnet/haiku) | Ignored — Codex session picks the model | Whatever endpoint is configured |
| Claude-specific tool names in skill bodies (`Task tool`, `TodoWrite`, `Skill` tool) | No mapping shipped (matches superpowers' own Codex manifest, which also ships none) — if a dev-kit skill body's tool references prove not to resolve sensibly under Codex in practice, the fix is a manifest-level instruction block (Kimi's `skillInstructions` field is the precedent), not a per-skill rewrite | n/a |

## 5. Non-goals (explicit)

- **No rewrite of the 42 skills.** Canonical format is fully unchanged.
- **No change to Claude Code distribution.** `.claude-plugin/plugin.json` + `marketplace.json` stay exactly as they are.
- **No MiniMax-specific plugin/skill format.** It does not exist; the recipe in §4.3 is the whole MiniMax story.
- **No `.agents/skills/` symlink mirror, no `lib/skill_sync.py`, no `skills-sync` skill.** Superseded per §3.2 — a static manifest field makes all of that unnecessary.
- **No submission to the curated `openai/plugins` marketplace in this change.** That is a separate, deliberate decision (public listing, review process) out of scope here; §4.2 states the resulting distribution gap plainly.
- **No Codex hook projection.** `.codex/hooks.json` stays as the pre-existing loghooks integration; wiring `hooks/hooks.json`'s other events into Codex's hook config is an unrelated, separately-scoped concern.

## 6. Open risks / known gaps (real)

1. **Self-serve installability into Codex CLI is unconfirmed.** `.codex-plugin/plugin.json` makes the repo schema-correct, but whether an end user can point Codex CLI at an arbitrary non-curated GitHub repo (analogous to Claude Code's `/plugin marketplace add owner/repo`) was not verified in this pass. If Codex CLI turns out to only install from the curated `openai/plugins` list, dev-kit would need a submission there for real end-user installability — a decision this document does not make.
2. **No tool-name translation layer for Codex.** If, in practice, a dev-kit skill's references to Claude-native tools (`Task`, `TodoWrite`, `Skill`) don't resolve sensibly for a Codex agent, the fix is a `skillInstructions`-style field in `.codex-plugin/plugin.json` (Kimi's manifest is the working precedent) — not yet needed speculatively, since Codex's own manifest in superpowers ships none.
3. **`.codex-plugin/plugin.json`'s `version` field is maintained by hand**, separately from `.claude-plugin/plugin.json`'s automated `version-bump.yml`. Superpowers keeps its multiple manifests in lockstep at the same version by convention; dev-kit does the same today (both start at `0.3.3`) but nothing enforces they stay in sync on future bumps. Wiring `.codex-plugin/plugin.json` into `version-bump.yml` is a reasonable, well-scoped follow-up — not done here to avoid touching that workflow's already-extensive test coverage (`tests/test_bump_workflow.py`) as a side effect of an unrelated change.
4. **MiniMax endpoint parity is the model's problem, not ours.** Some skills lean on Claude-specific tool-use fidelity (e.g. the `Agent` sub-agent tool in `build`). Whether MiniMax's Anthropic-compatible endpoint reproduces that behavior is outside this harness's control.

## Decision

Add `.codex-plugin/plugin.json` at the repo root with `"skills": "./skills/"`, matching the schema OpenAI's own `openai/plugins` repo documents and the pattern `obra/superpowers` ships in production to both Claude Code and Codex CLI from a single shared `skills/` directory. No skill file changes, no generated artifacts, no sync script, no new test surface. MiniMax is served by a documented base-URL config recipe with no artifact at all. This replaces an earlier draft of this document that recommended a symlink-mirror-plus-sidecar scheme based on a single secondary source — superseded once first-party evidence (OpenAI's own plugin repo, and a real production multi-runtime skills repo) showed the actual mechanism needs one file, not one directory per skill.

## References

- [1] `obra/superpowers` — production skills framework shipping to Claude Code, Codex CLI/App, Cursor, Kimi Code, OpenCode, Pi, Antigravity, Factory Droid, GitHub Copilot CLI from one shared `skills/` directory: https://github.com/obra/superpowers
- [2] `openai/plugins` — OpenAI's own curated Codex plugin repo; documents the required `.codex-plugin/plugin.json` manifest, the optional `skills/` companion surface, and the `.agents/plugins/marketplace.json` marketplace format: https://github.com/openai/plugins
- [3] MiniMax M2 for AI coding tools (base-URL swap; Anthropic- and OpenAI-compatible): https://minimax-m2.com/docs/for-ai-coding-tools
- [4] MiniMax M2 API — Anthropic-compatible `/messages` + OpenAI-compatible `/chat/completions`: https://platform.minimax.io/docs/token-plan/other-tools
- [5] MiniMax-AI/skills (SKILL.md consumer across Claude Code, Cursor, Codex, OpenCode): https://github.com/MiniMax-AI/skills
