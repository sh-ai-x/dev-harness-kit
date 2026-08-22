# Codebase Map (lazy placeholder)

> Run `/dev-kit:bootstrap --full-claude-md` to (re)generate this
> file with the full 4-section map (Tree via `os.walk` depth 4,
> Manifest, Deps top-10, Conventions). Default bootstrap keeps
> this stub so CLAUDE.md's reference always resolves; the heavy
> content is opt-in.

## Pointer entries

- `bin/worktree-remove-safe.sh` + `tools/worktree_cleanup.py` — cleanup-safe
  archival wrapper for `git worktree remove`. See
  [`docs/skills/log.md`](skills/log.md) §"Cleanup-safe worktree removal".
