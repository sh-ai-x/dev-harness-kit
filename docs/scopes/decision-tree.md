# Decision Tree — which scope do I edit?

Answer in order. Stop at the first "yes".

## Q1. Am I in the dev-harness-kit source repo itself?

- **YES** → use `claude --plugin-dir <dev-harness-kit-repo>`. No settings file needed; the source code IS the plugin definition.
- **NO** → continue.

## Q2. Does this project have code I want to ship?

- **NO** (recipe, scratchpad, docs-only, README-only) → **undev**. Do nothing. The plugin is not enabled by default.
- **YES** → continue.

## Q3. Will this be a 4-hour sprint or a multi-session project?

- **4-hour sprint** → **lite**. Edit `<proj>/.claude/settings.json`. See [`project-scope.md`](project-scope.md) and copy [`templates/settings.project.lite.json`](templates/settings.project.lite.json).
- **Multi-session / multi-agent / autonomous** → **full**. Edit `<proj>/.claude/settings.json`. See [`project-scope.md`](project-scope.md) and copy [`templates/settings.project.full.json`](templates/settings.project.full.json).
- **Multi-role team / dependency-aware plan** → **team**. Edit `<proj>/.claude/settings.json`. See [`project-scope.md`](project-scope.md) and copy [`templates/settings.project.team.json`](templates/settings.project.team.json). Roles are user-defined (no shipped defaults); `/dev-kit:mode team` prompts for active role + member roles at activation time.

## Q4. Should `.dev-kit/` be tracked in git for this project? (independent of Q3)

- **NO** (default) → do nothing. `.dev-kit/` stays gitignored; bootstrap will not strip it from `.gitignore`.
- **YES** → run `/dev-kit:team on` (or set `DEV_KIT_TEAM=1`). Bootstrap sub-stage 8.5 will strip `^\.dev-kit` from `.gitignore` on the next run, and the team toggle persists in `env.DEV_KIT_TEAM`. Independent of the mode you picked in Q3 — full+team, full+no-team, lite+team, lite+no-team are all valid. See [`modes.md` § Team toggle](modes.md#team-toggle-dev_kit_team--orthogonal-to-mode).

---

## Q1'. Where do I put a setting that affects *every* project on my machine?

- Edit `~/.claude/settings.json`. See [`user-scope.md`](user-scope.md) and copy [`templates/settings.user.json`](templates/settings.user.json).

## Q2'. Where do I put a setting that affects only *this* project?

- **Team-shared** (committed to git) → edit `<proj>/.claude/settings.json`. See [`project-scope.md`](project-scope.md).
- **Personal override** within this project (gitignored) → edit `<proj>/.claude/settings.local.json`. See [`local-scope.md`](local-scope.md) and copy [`templates/settings.local.json`](templates/settings.local.json).

## Q3'. My teammate sees different hooks than I do in the same project. Why?

- One of you has a project-scope override in `settings.json`, the other has a local-scope override in `settings.local.json`. Or one of you has the user-scope enabled. See [`troubleshooting.md`](troubleshooting.md) §"My teammate sees different hooks than I do".

---

## Scope matrix at a glance

| Setting                              | User | Project | Local |
|--------------------------------------|:----:|:-------:|:-----:|
| Plugin choice (which kit)            |  ❌  |   ✅    |  ❌   |
| Personal preferences (theme, etc.)  |  ✅  |   ❌    |  ✅   |
| Team-shared hooks                    |  ❌  |   ✅    |  ❌   |
| Personal debug flags                 |  ❌  |   ❌    |  ✅   |
| Keybindings                          |  ✅  |   ❌    |  ❌   |
| Personal skill overrides             |  ❌  |   ❌    |  ✅   |
| Mode (`DEV_KIT_MODE`)                |  ❌  |   ✅    |  ✅   |
| Universal `allow` permissions        |  ✅  |   ✅    |  ❌   |
| Project-specific `allow` permissions |  ❌  |   ✅    |  ❌   |

**Default rule:** smallest scope that satisfies the need wins.
