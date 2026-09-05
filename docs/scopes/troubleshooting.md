# Troubleshooting — why is X firing in Y?

## "destructive-confirm.sh" fires in a repo that never ran ci-setup

**Cause:** user-scope `enabledPlugins` has the kit enabled, OR the kit's cache files are leaking from `extraKnownMarketplaces`.

**Fix:**

```bash
# 1. Check user-scope plugins
jq '.enabledPlugins // {}' ~/.claude/settings.json

# 2. If dev-kit is listed, remove it from user scope
# Edit ~/.claude/settings.json — drop the "dev-kit@dev-kit": true line

# 3. Then opt the project in explicitly
echo '{"enabledPlugins": {"dev-kit@dev-kit": true}}' > .claude/settings.json

# 4. (Optional) Prune stale cache versions
claude plugin uninstall dev-kit@dev-kit
claude plugin install dev-kit@dev-kit
```

## My teammate sees different hooks than I do

**Cause:** one of you has a user-scope enable, the other doesn't. Or one of you has a local-scope override.

**Fix:** run this on both machines, compare:

```bash
echo "=== USER ===" ; jq '.enabledPlugins // {}' ~/.claude/settings.json
echo "=== PROJECT ===" ; jq '.enabledPlugins // {}' .claude/settings.json
echo "=== LOCAL ===" ; jq '.enabledPlugins // {}' .claude/settings.local.json 2>/dev/null || echo "(missing)"
echo "=== MODE ===" ; jq -r '.env.DEV_KIT_MODE // "full (default)"' .claude/settings.json
```

Both should show the same `enabledPlugins` and `DEV_KIT_MODE`.

## `.claude/settings.local.json` was committed by mistake

**Cause:** `.gitignore` was missing the line.

**Fix:**

```bash
# 1. Add the line
echo '.claude/settings.local.json' >> .gitignore

# 2. Untrack the file
git rm --cached .claude/settings.local.json

# 3. Commit the .gitignore change only
git add .gitignore
git commit -m "chore: gitignore .claude/settings.local.json"
```

## I see the same hook firing twice per tool call

**Cause:** user-scope and project-scope both have the same plugin enabled. Each fires its own copy of every hook.

**Fix:**

```bash
# Check for duplication
for h in destructive-confirm worktree-guard tdd-guard; do
  n=$(find ~/.claude/plugins/cache -name "${h}.sh" 2>/dev/null | wc -l | tr -d ' ')
  echo "$h: $n instance(s) in cache"
done
# Expected: 1 (or 2 if dev-kit and dev-kit-lite are both installed)
```

Then disable the duplicate:

```bash
# If both dev-kit and dev-kit-lite are enabled at user scope, remove one
# Edit ~/.claude/settings.json — pick one and drop the other
```

## I switched mode but nothing changed

**Cause:** mode is checked at session start. Changing `DEV_KIT_MODE` mid-session requires a reload.

**Fix:** exit Claude Code and restart the session, or use the per-session env var override:

```bash
DEV_KIT_MODE=lite claude --plugin-dir /Users/sanghee/dev/dev-harness-kit
```

## `find ~/.claude/plugins/cache` shows 5+ versions of dev-kit

**Cause:** every plugin update leaves the previous version in cache; the cache is not auto-pruned.

**Fix:**

```bash
claude plugin uninstall dev-kit@dev-kit
claude plugin install dev-kit@dev-kit
# This re-installs only the active version; stale ones get pruned.
```

## Hook asks "DESTRUCTIVE CONFIRM" for a `git push` to a recipe repo

**Cause:** same as the first FAQ — user-scope leak.

**Fix:** see first FAQ. Recipe repos should be `undev`, not `full`.

## Audit script — run anytime

```bash
# Full audit in one go
echo "=== USER SCOPE ==="
jq '{enabledPlugins, extraKnownMarketplaces_keys: (.extraKnownMarketplaces // {} | keys)}' \
  ~/.claude/settings.json

echo "=== PROJECT SCOPE (cwd) ==="
jq '{enabledPlugins, mode: (.env.DEV_KIT_MODE // "full (default)")}' \
  .claude/settings.json 2>/dev/null || echo "(no .claude/settings.json)"

echo "=== LOCAL SCOPE (cwd) ==="
jq . .claude/settings.local.json 2>/dev/null || echo "(no .claude/settings.local.json)"

echo "=== STALE CACHE ==="
find ~/.claude/plugins/cache/dev-kit* -maxdepth 2 -mindepth 2 -type d | wc -l

echo "=== DUPLICATE HOOKS ==="
for h in destructive-confirm worktree-guard tdd-guard git-guard; do
  n=$(find ~/.claude/plugins/cache -name "${h}.sh" 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -gt 1 ] && echo "WARN $h: $n instances (deduplicate)"
done
```
