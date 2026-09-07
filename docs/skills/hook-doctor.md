> [← Skills index](README.md) · [Project README](../../README.md)

# `hook-doctor`

**Category:** `audit` · **Alpha:** `enforcement` · **Invocation:** Model-invoked — auto-fires on visible hook failure text; not exposed in slash autocomplete, never typed directly

`hook-doctor` exists to stop the model from guessing when a hook has actually failed. Its job is to run the deterministic checker, identify which provider's cache or registration drifted, run only the matching safe updater, and report the exact restart step — without editing project files or disabling any failed enforcement hook.

## When it fires

- A hook reports `exited with code 127` or another non-zero status.
- `SessionStart`, `UserPromptSubmit`, `PostToolUse`, or `Stop` hooks fail.
- A plugin reload leaves hook manifests and cached files out of sync.
- The user asks why a dev-kit hook is failing.
- The user reports a slash command resolves the wrapper but the skill body is missing — usually a same-version marketplace update that the cache hasn't picked up (the SessionStart hook `hooks/plugin-cache-refresh.sh` runs `bin/devkit-refresh.sh`'s rsync automatically on every session start).

## How it works

The model invokes this skill the moment the conversation contains hook failure text (`hook exited with code`, `hook failed`, etc.). The skill runs a deterministic triage:

1. **Capture the event and exit code.** Treat `127` as a missing command or path until the doctor proves otherwise.
2. **Run the bundled checker** from the repository root:

   ```bash
   bash skills/hook-doctor/scripts/doctor.sh
   ```
3. **If the report identifies a stale cache, run only the matching safe updater:**

   - Codex: `bash skills/codex-cache-update/scripts/update.sh`
   - Claude Code: `bash bin/devkit-refresh.sh`

   Do not run both unless both providers are reported stale.

   > Both updaters are now also wired as SessionStart hooks
   > (`hooks/plugin-cache-refresh.sh` and `.codex-plugin/hooks/plugin-cache-refresh.sh`)
   > that detect marketplace → cache drift via short-SHA marker comparison
   > and rsync only when the SHA differs. Manual invocation is only
   > needed for explicit verification or out-of-band triggers.
4. **Re-run the checker.** If all checks pass, tell the user to restart the affected client because plugin hooks are loaded at session start. Do not claim the live session is repaired before the restart.

## Failure handling

- Missing `PLUGIN_ROOT` / `CLAUDE_PLUGIN_ROOT`: report the client and reload or restart requirement; do not guess a cache path.
- Missing hook file or manifest path: identify the exact path and refresh the provider cache if the matching updater is available.
- Missing `bash`, `jq`, `python3`, or `rsync`: report the install prerequisite; do not silently bypass the hook.
- Any non-zero hook result other than infrastructure failure: preserve the hook's denial or warning and hand the event to the relevant skill.

## Output contract

Return one compact report containing:

- provider and event
- failing exit code and evidence
- doctor result (`PASS`, `REPAIRABLE`, or `BLOCKED`)
- repair command run, if any
- exact restart or missing-prerequisite instruction

## Safety

- Never edits project files.
- Never disables a failing enforcement hook.
- May run a safe provider cache updater when the doctor flags it as stale.

## Next step

After a successful restart, run `/dev-kit:status` if the failure interrupted a stage, or return to the skill that originally triggered the failed hook.

---
*Source: [`skills/hook-doctor/SKILL.md`](../../skills/hook-doctor/SKILL.md)*
