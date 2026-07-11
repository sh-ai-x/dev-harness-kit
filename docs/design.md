# design.md — Cross-agent skill harness (Claude Code · Codex CLI · MiniMax)

> Scope: how the 42 canonical `skills/<name>/SKILL.md` definitions run across Claude Code, OpenAI Codex CLI, and MiniMax — without forking the skill source. Design only; no implementation in this document.

## 1. Problem statement

The dev-kit skills are authored once as Claude Code plugin skills and invoked as `/dev-kit:<name>`. We want the same skill definitions usable from Codex CLI and "with MiniMax" too. The research below shows this is now mostly a packaging problem, not a rewrite: Codex CLI has converged on the **Agent Skills open standard** (`SKILL.md`, discovered under `.agents/skills/`), the same core format Claude Code uses, and MiniMax is not a harness at all — it is a model backend consumed through Claude Code's Anthropic-compatible endpoint or Codex's OpenAI-compatible endpoint. So "compatible with all three" concretely means: keep Claude Code as the canonical format, **mirror** the canonical skills into the `.agents/skills/` path Codex already discovers (symlink or plain copy, no transformation), add a one-field sidecar only where Codex's default invocation behavior disagrees with the canonical intent, and reach MiniMax by pointing either harness's base URL at MiniMax — no compiler, no MiniMax-specific artifact.

## 2. Current state per runtime

| Runtime | Skill system today | Cross-tool artifacts in this repo |
|---|---|---|
| **Claude Code** | Full: 42 `skills/<name>/SKILL.md`, `/dev-kit:<name>` slash commands, `hooks/hooks.json`, plugin marketplace | Canonical source (native) |
| **Codex CLI** | Native SKILL.md support via `.agents/skills/` + `/skills` command (2026); `config.toml` hooks mirror Claude events | `.codex/hooks.json` = `{"hooks":{"Stop":[]}}` only; `AGENTS.md` = 1-line pointer to `CLAUDE.md` |
| **MiniMax** | None — model backend, not a harness; consumed via base-URL override inside Claude Code or Codex | Zero references in repo (grep-confirmed) |

## 3. Research findings (sourced)

### 3.1 Codex CLI extensibility (2026)

- **Skills are native.** Codex CLI discovers `SKILL.md`-based skills and exposes them via the `/skills` command and `$skill` mention syntax. Discovery scans, in priority order: `.agents/skills/` (repo, walking cwd→repo root), `$HOME/.agents/skills/` (user), `/etc/codex/skills/` (admin), and bundled system skills. A skill is a directory with a `SKILL.md` plus optional `scripts/`, `references/`, `assets/`. [1][2]
- **Required frontmatter is only `name` + `description`.** `description` doubles as the model's trigger signal (when to invoke). Optional per-skill Codex metadata lives in a sibling `agents/openai.yaml`: `allow_implicit_invocation` (default `true`; `false` forces explicit `$skill`), UI fields, and declared tool/MCP dependencies. [2]
- **Custom prompts (`~/.codex/prompts/*.md`, invoked as `/name`) still exist but are deprecated** in favor of skills — so skills, not prompt files, are the right mirror target. [3]
- **AGENTS.md** is Codex's project-instruction file (with `AGENTS.override.md` and `project_doc_fallback_filenames` in `config.toml`). It is the analogue of `CLAUDE.md`, not of a skill. [4]
- **Hooks now mirror Claude Code.** Behind `features.hooks` (default `false`), `config.toml` supports `PreToolUse, PostToolUse, SessionStart, SubagentStart, SubagentStop, UserPromptSubmit, Stop, PreCompact, PostCompact, PermissionRequest`. Command hooks run; prompt/agent hook handlers are parsed but skipped. [5]
- **Permissions are session-level, not per-skill.** `sandbox_mode` (`read-only|workspace-write|danger-full-access`), `approval_policy`, and `default_permissions` profiles govern tool access for the whole session. There is **no per-skill `allowed-tools`** equivalent; `agents/openai.yaml` can declare tool *dependencies* but cannot *restrict* them. [1][5]

### 3.2 The Agent Skills open standard

`SKILL.md` is now a published open standard (agentskills.io, Dec 2025; governance under the Linux Foundation's Agentic AI Foundation) whereby the same file works "unchanged" across Codex CLI, Claude Code, Gemini CLI, Cursor, and 28+ other tools. Portable core: required `name` (≤64 chars, lowercase/hyphen) + `description` (≤1024 chars); optional `license`, `compatibility`, `metadata`. Tool-specific extensions are namespaced and **safely ignored** by tools that don't understand them — Claude Code's `when_to_use`, `user-invocable`, `disable-model-invocation`, `allowed-tools`, `category`, `model` are simply skipped by Codex, and Codex's `agents/openai.yaml` is skipped by Claude Code. Shared discovery directory: `.agents/skills/`. [6][2]

### 3.3 MiniMax

MiniMax ships **no skill/plugin harness of its own for consuming skills.** MiniMax-M2/M2.1/M2.5/M2.7 expose both an **OpenAI-compatible** (`/chat/completions`) and an **Anthropic-compatible** (`/messages`) endpoint, and integration is documented as a base-URL + API-key swap: Claude Code points `~/.claude/settings.json` at MiniMax's Anthropic-compatible endpoint; Codex points `.codex/config.toml` at the OpenAI-compatible endpoint with `MINIMAX_API_KEY`. [7][8][9] MiniMax's own `MiniMax-AI/skills` repo is a *consumer* of the same SKILL.md standard (installable into Claude Code, Cursor, Codex, OpenCode), which further confirms SKILL.md — not a MiniMax format — is the lingua franca. [10] **Conclusion: "MiniMax compatibility" = run the dev-kit skills inside Claude Code or Codex while that harness's model backend is MiniMax. It requires zero new skill artifact — it is a configuration recipe.**

## 4. Recommended architecture

**One canonical source, mirrored into the path Codex already scans — no transformation layer.**

Keep `skills/<name>/SKILL.md` (Claude Code format) as the single source of truth. Because Codex's portable core is just `name` + `description` and it safely ignores every other frontmatter key (§3.2), a canonical SKILL.md is **already a valid Codex skill as-is**. Compatibility is therefore achieved by mirroring — a symlink (or a plain copy, if symlinks prove awkward for Codex's directory walk in practice) from `.agents/skills/dev-kit-<name>` to the existing `skills/<name>/` — not by building a parser/rewriter. Claude Code keeps consuming `skills/` directly (unchanged). Codex discovers the mirrored path natively. MiniMax needs no output at all — it is reached by base-URL config in whichever of the two harnesses the user drives.

```
skills/<name>/SKILL.md            # CANONICAL (Claude Code native, superset frontmatter)
        |
        v   lib/skill_sync.py       (thin script: symlink/copy + 1-field check — no body parsing)
        |
.agents/skills/dev-kit-<name>      # symlink -> ../../../skills/<name>/  (or plain copy)
        `-- agents/openai.yaml     # written ONLY when disable-model-invocation: true (human-use)
```

### 4.1 Canonical schema: unchanged, one additive opt-out field

The existing frontmatter (`.claude/rules/skill-authoring.md`) already covers everything the standard needs — `name` and `description` are mandatory today. No breaking change. One **optional additive** field is introduced for the sync step only:

- `runtimes:` (optional, default `all`) — `all` | `claude-only` | list. Lets a Claude-Code-only skill (e.g. one that hard-depends on the `Agent` tool or a Claude-specific hook) opt out of mirroring instead of exposing a broken Codex skill. Absent = mirrored for all runtimes. This mirrors the `log` skill's `--claude-only`/`--codex-only` flags, promoted from a runtime flag to a declaration.

Nothing else changes. The mirrored `SKILL.md` is byte-identical to the canonical file — a symlink guarantees this by construction; a fallback copy would too, since there is no rewriting step. Claude-specific keys travel across unmodified because Codex ignores unknown keys (§3.2) — there is nothing to strip.

### 4.2 What gets mirrored for Codex

For all 42 skills (minus any `runtimes: claude-only` opt-outs), `lib/skill_sync.py` creates `.agents/skills/dev-kit-<name>` as a **symlink** to `../../../skills/<name>/` (falling back to a recursive copy only if symlinks prove awkward for Codex's directory walk in practice — not designed around speculatively). No rewriting: the `SKILL.md` Codex reads is the exact canonical file, `when_to_use` and all — Codex ignores what it doesn't recognize per §3.2.

A sidecar `agents/openai.yaml` is written **only where Codex's default diverges from canonical intent**. Codex defaults `allow_implicit_invocation: true` (§3.1) — which already matches every skill with `disable-model-invocation: false` or unset. Checking the actual frontmatter across all 42 skills: only **one** (`plan`) sets `disable-model-invocation: true` today — every other human-use skill (`ship`, `review`, `security`, `bootstrap`, `status`, `onboard`, `repair`, `log`, `config`, `ci-setup`, etc.) leaves it `false`/unset, same as the 14 model-use skills, so Codex's default already matches them too. That is the entire "generation" surface for this design — one sidecar file, for exactly 1 of 42 skills today (`plan`'s `agents/openai.yaml: allow_implicit_invocation: false`), not a per-skill artifact for all 42. The count grows only as more skills adopt `disable-model-invocation: true` in the future — it is not a fixed ~25-skill tax.

`AGENTS.md` is left as-is (a pointer to `CLAUDE.md`) — it is the project-instruction analogue, not a skill target, so the sync step does not touch it. Codex hooks are out of scope for this skill-mirroring step (see §5).

### 4.3 What gets exposed for MiniMax

Nothing is generated. The deliverable is a documented, copy-pasteable config recipe (added to `README.md`, not a generated artifact):

- **Via Claude Code:** point `~/.claude/settings.json` `env` (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`) at MiniMax's Anthropic-compatible `/messages` endpoint. The 42 canonical skills then run unchanged with MiniMax as the model. [7][8]
- **Via Codex:** set `.codex/config.toml` model/base-URL to MiniMax's OpenAI-compatible endpoint + `MINIMAX_API_KEY`; the mirrored `.agents/skills/` entries are what Codex loads. [7][9]

This is honest: there is no MiniMax plugin format to target, and inventing one would be dead weight.

### 4.4 Permission and classification mapping

| Canonical (Claude Code) | Codex CLI | MiniMax |
|---|---|---|
| `disable-model-invocation: true` (only `plan` today) | `agents/openai.yaml: allow_implicit_invocation: false` (explicit `$skill`) | inherits the harness it runs in |
| `disable-model-invocation: false`/unset (41 of 42 skills, human-use and model-use alike) | Codex default (`allow_implicit_invocation: true`) already matches — no sidecar needed | inherits |
| `allowed-tools` / `disallowed-tools` | **No per-skill equivalent.** Degrades to session `sandbox_mode` + `approval_policy`; MCP tools surfaced as `tool_dependencies` only | inherits |
| `model:` (opus/sonnet/haiku) | Ignored — Codex session picks the model | With MiniMax, the model is whatever endpoint is configured |
| `category`, `when_to_use` | Ignored by Codex reader (mirrored file still carries them, harmlessly) | n/a |

The one real semantic gap is per-skill tool restriction: Claude enforces `allowed-tools` per skill; Codex has only a session-wide sandbox with no comparable mechanism. Where a sidecar exists (human-use skills), it can carry the intended restriction as an advisory comment for operator visibility; skills without a sidecar (model-use skills, or human-use skills where the Codex default already matches) get no such note in Codex at all. Stated plainly, not hidden (see §6).

### 4.5 File / module layout (follows existing repo conventions)

| Path | Role |
|---|---|
| `lib/skill_sync.py` | Thin stdlib-only script — same spirit as `skills/log/scripts/*.sh`, not a build pipeline. No SKILL.md body parsing, no frontmatter rewriting. Reads exactly one field per skill (`disable-model-invocation`) plus the optional `runtimes:` opt-out. Creates/refreshes the `.agents/skills/dev-kit-<name>` symlink (or copy) and writes `agents/openai.yaml` only when that one field says so. |
| `skills/skills-sync/SKILL.md` | New **human-use** skill (`user-invocable: true`, `disable-model-invocation: true`). Subcommands `sync` \| `check` \| `clean`, mirroring the `log` skill's script-driven shape. `check` is the CI-safe dry-run. |
| `.agents/skills/dev-kit-<name>` | Symlink (or, only as a fallback, a copy) to `skills/<name>/`, committed so a fresh clone works in Codex with no build step. Open-standard discovery dir — also picked up by any other Agent-Skills-conformant tool for free. |
| `tests/test_skill_sync.py` | Asserts: every canonical skill (minus `runtimes: claude-only`) has a mirrored entry; the mirrored `SKILL.md` content is identical to canonical (symlink resolves, or copy diff is empty); `agents/openai.yaml` exists if and only if `disable-model-invocation: true`; `skills-sync check` exits 0 (mirror in sync with source). |

Adding `skills-sync` bumps the skill count 42 → 43: update `tests/test_smoke.py` `SKILL_COUNT` and the count in `.claude/rules/skill-authoring.md` together (the rule already mandates this paired bump).

## 5. Non-goals (explicit)

- **No rewrite of the 42 skills.** Canonical format is unchanged except the optional `runtimes:` field.
- **No change to Claude Code distribution.** `.claude-plugin/plugin.json` + `marketplace.json` stay; Claude Code keeps loading `skills/` directly, never the mirrored copy.
- **No MiniMax-specific plugin/skill format.** It does not exist; the recipe in §4.3 is the whole MiniMax story.
- **Codex hook projection is not part of this design.** Codex's `features.hooks` now mirror Claude events [5], so projecting `hooks/hooks.json` into `.codex/config.toml` is feasible and would reuse the `log` skill's sentinel-tagged merge pattern — but it is a *hooks* concern, orthogonal to running skills, and folding it in here would overreach the task. It is a separate, well-scoped follow-up, not a vague "later."
- **No custom-prompt (`~/.codex/prompts`) output.** That surface is deprecated in favor of skills [3]; targeting it would ship a dead format.

## 6. Open risks / known gaps (real)

1. **Per-skill tool restriction is not enforceable on Codex.** `allowed-tools`/`disallowed-tools` degrade to a session-wide `sandbox_mode`. A skill that is safe on Claude Code because it is tool-boxed can, on Codex, do whatever the session's sandbox allows. Where a sidecar exists it documents the intent; it cannot enforce it. Mitigation is operational: run destructive skills under a stricter Codex `sandbox_mode`.
2. **Standard drift.** The Agent Skills spec is young (published Dec 2025, AAIF governance). If required frontmatter or the `.agents/skills/` discovery path changes, `lib/skill_sync.py` and `tests/test_skill_sync.py` are the single choke point to update — a real maintenance cost, not a one-time write.
3. **Symlinks remove most staleness risk, but the sidecar can still drift, and the copy fallback reintroduces it.** Because `.agents/skills/dev-kit-<name>` is a symlink to `skills/<name>/`, the mirrored `SKILL.md` is always byte-identical to canonical — there is no compiled copy of the body to go stale. The remaining drift surface is narrower: whether a skill's `agents/openai.yaml` sidecar (present/absent, and its `allow_implicit_invocation` value) still matches that skill's current `disable-model-invocation`. `skills-sync check` (`tests/test_skill_sync.py`) is the guard for that narrower surface; it must be wired into CI or the guarantee is only advisory. If symlinks prove unworkable for Codex's directory walk and the fallback copy path is used instead, the original full staleness risk returns and `check` becomes load-bearing again, not merely a safety net.
4. **MiniMax endpoint parity is the model's problem, not ours.** Some skills lean on Claude-specific tool-use fidelity (e.g. the `Agent` sub-agent tool in `build`). Whether MiniMax's Anthropic-compatible endpoint reproduces that behavior is outside this harness's control; such skills should carry `runtimes: claude-only` if they prove non-portable in practice, rather than exposing a Codex mirror that fails at runtime.

## Decision

Adopt the mirror/sync architecture: canonical Claude Code `skills/<name>/SKILL.md` stays authoritative and untouched; `lib/skill_sync.py` (driven by a new human-use `skills-sync` skill) symlinks — or, only as a fallback, copies — each one into the open-standard `.agents/skills/dev-kit-<name>` path that Codex CLI already scans. There is no parser, no rewriter, no build pipeline; a one-field sidecar (`agents/openai.yaml`) is written only for the skills where `disable-model-invocation: true` makes Codex's default invocation behavior diverge from canonical intent — exactly one skill (`plan`) today, growing only as more skills adopt that field. MiniMax is served by a documented base-URL recipe with no artifact at all. Compatibility comes from mirroring the existing skills into a path Codex already discovers, not from a transformation layer — which is exactly the minimal-diff shape Codex's own convergence on the SKILL.md standard makes possible.

## References

- [1] Codex config reference — MCP, sandbox_mode, approval_policy, default_permissions: https://learn.chatgpt.com/docs/config-file/config-reference
- [2] Codex "Build skills" (SKILL.md format, `.agents/skills` discovery, `agents/openai.yaml`, `allow_implicit_invocation`): https://learn.chatgpt.com/docs/build-skills
- [3] Codex custom prompts (`~/.codex/prompts/*.md`, deprecated in favor of skills): https://developers.openai.com/codex/custom-prompts
- [4] Codex custom instructions with AGENTS.md: https://developers.openai.com/codex/guides/agents-md
- [5] Codex hooks + developer commands (`features.hooks`, event list, `/skills`): https://learn.chatgpt.com/docs/config-file/config-reference and https://learn.chatgpt.com/docs/developer-commands
- [6] Agent Skills open standard (portable SKILL.md, agentskills.io / AAIF, shared `.agents/skills`): https://codex.danielvaughan.com/2026/05/05/agent-skills-open-standard-portable-skills-codex-cli-cross-agent/
- [7] MiniMax M2 for AI coding tools (base-URL swap; Anthropic- and OpenAI-compatible): https://minimax-m2.com/docs/for-ai-coding-tools
- [8] MiniMax M2 API — Anthropic-compatible `/messages` + OpenAI-compatible `/chat/completions`: https://platform.minimax.io/docs/token-plan/other-tools
- [9] MiniMax M2/M2.1/M2.5/M2.7 endpoints and coding-tool integration: https://minimax-m2.com/docs/for-ai-coding-tools
- [10] MiniMax-AI/skills (SKILL.md consumer across Claude Code, Cursor, Codex, OpenCode): https://github.com/MiniMax-AI/skills
