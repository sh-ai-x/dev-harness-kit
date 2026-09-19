import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(HERE, "../../..");
const HOOKS_JSON = resolve(PROJECT_ROOT, "hooks/hooks.json");
const DEFAULT_TIMEOUT_MS = 30_000;

const TOOL_NAME_MAP = new Map([
  ["bash", "Bash"],
  ["write", "Write"],
  ["apply_patch", "Edit"],
  ["edit", "Edit"],
  ["ask", "AskUserQuestion"],
  ["task", "Agent"],
  ["read", "Read"],
  ["search", "Grep"],
  ["find", "Glob"],
  ["browser", "WebFetch"],
]);

function loadHookManifest() {
  if (!existsSync(HOOKS_JSON)) return { hooks: {} };
  return JSON.parse(readFileSync(HOOKS_JSON, "utf8"));
}

function asClaudeToolName(toolName) {
  if (!toolName) return "";
  return TOOL_NAME_MAP.get(toolName) ?? TOOL_NAME_MAP.get(String(toolName).toLowerCase()) ?? String(toolName);
}

function matcherMatches(matcher, toolName) {
  if (!matcher || matcher === "*") return true;
  return String(matcher)
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean)
    .includes(toolName);
}

function commandsFor(eventName, toolName) {
  const manifest = loadHookManifest();
  const groups = manifest?.hooks?.[eventName] ?? [];
  const commands = [];
  for (const group of groups) {
    if (!matcherMatches(group.matcher, toolName)) continue;
    for (const hook of group.hooks ?? []) {
      if (hook?.type !== "command" || !hook.command) continue;
      commands.push({
        command: hook.command,
        failClosed: hook.fail_closed === true,
        timeoutMs: Number.isFinite(hook.timeout) ? hook.timeout * 1000 : DEFAULT_TIMEOUT_MS,
      });
    }
  }
  return commands;
}

function payloadFor(eventName, event, toolName) {
  const cwd = process.cwd();
  const input = event?.input ?? {};
  const payload = {
    hook_event_name: eventName,
    tool_name: toolName,
    tool_input: input,
    cwd,
    session_id: process.env.GJC_SESSION_ID ?? process.env.GJC_SESSION ?? "",
    transcript_path: process.env.GJC_SESSION_FILE ?? "",
    transcript: "",
    _gjc: {
      event_type: event?.type ?? eventName,
      tool_call_id: event?.toolCallId ?? "",
    },
  };
  if (event?.content !== undefined) payload.tool_response = event.content;
  if (event?.details !== undefined) payload.tool_response_details = event.details;
  if (event?.isError !== undefined) payload.tool_response_is_error = event.isError;
  return JSON.stringify(payload);
}

function askReason(stdout) {
  const text = String(stdout ?? "").trim();
  if (!text.startsWith("{")) return "";
  try {
    const parsed = JSON.parse(text);
    if (parsed?.permissionDecision === "ask") {
      return parsed?.permissionDecisionReason || parsed?.reason || "hook requested confirmation";
    }
  } catch {
    return "";
  }
  return "";
}

function failureResult(eventName, reason, eventType) {
  const message = `dev-kit ${eventName} hook failed: ${reason}`;
  if (eventType === "tool_result") {
    return {
      isError: true,
      content: [{ type: "text", text: message }],
      details: { devKitHookFailure: true, eventName, reason },
    };
  }
  return { block: true, reason: message };
}

function confirmationResult(eventName, reason, eventType) {
  const message = `dev-kit ${eventName} hook requested confirmation: ${reason}`;
  if (eventType === "tool_result") {
    return {
      isError: true,
      content: [{ type: "text", text: message }],
      details: { devKitHookConfirmationRequired: true, eventName, reason },
    };
  }
  return { block: true, reason: message };
}

function runOne(entry, payload) {
  if (process.env.DEV_KIT_GJC_HOOKS_DRY_RUN === "1") {
    return { status: 0, stdout: "", stderr: "", dryRun: true };
  }
  const result = spawnSync(entry.command, {
    cwd: PROJECT_ROOT,
    env: {
      ...process.env,
      DEV_KIT_AGENT: "gjc",
      CLAUDE_PLUGIN_ROOT: PROJECT_ROOT,
      CLAUDE_PROJECT_DIR: process.cwd(),
      CODEX_PLUGIN_ROOT: PROJECT_ROOT,
    },
    input: payload,
    encoding: "utf8",
    shell: true,
    timeout: entry.timeoutMs,
  });
  return {
    status: result.status,
    signal: result.signal,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    error: result.error,
  };
}

function runHookCommands(eventName, event, toolName) {
  const payload = payloadFor(eventName, event, toolName);
  const commands = commandsFor(eventName, toolName);
  for (const entry of commands) {
    const result = runOne(entry, payload);
    const confirmation = askReason(result.stdout);
    if (confirmation) {
      return confirmationResult(eventName, confirmation, event?.type);
    }
    if (result.status === 0) continue;
    const reason = [result.stderr, result.stdout, result.error?.message, result.signal ? `signal ${result.signal}` : ""]
      .filter(Boolean)
      .join("\n")
      .trim() || `hook exited with status ${result.status}`;
    if (entry.failClosed) {
      return failureResult(eventName, reason, event?.type);
    }
    console.warn(`dev-kit ${eventName} hook warning: ${reason}`);
  }
  return undefined;
}

export default function registerDevKitHooks(api) {
  api.registerFunctionHook(
    "tool_call",
    async (event) => {
      const toolName = asClaudeToolName(event?.toolName);
      return runHookCommands("PreToolUse", event, toolName);
    },
    { target: "*", capabilities: ["tool"] },
  );

  api.registerFunctionHook(
    "tool_result",
    async (event) => {
      const toolName = asClaudeToolName(event?.toolName);
      return runHookCommands("PostToolUse", event, toolName);
    },
    { target: "*", capabilities: ["tool"] },
  );

  api.on("session_start", async (event) => {
    runHookCommands("SessionStart", event, "");
  });

  api.on("agent_end", async (event) => {
    runHookCommands("Stop", event, "");
  });

  api.on("session_shutdown", async (event) => {
    runHookCommands("SessionEnd", event, "");
  });
}

export const devKitGjcHookAdapter = {
  PROJECT_ROOT,
  HOOKS_JSON,
  asClaudeToolName,
  commandsFor,
  confirmationResult,
  failureResult,
  matcherMatches,
};
