#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { PilotConfig } from "./types.js";
import { PilotConfigSchema } from "./types.js";
import { createPermissionHandler } from "./permissions.js";
import { runAgent } from "./agent.js";
import { initFileLog, closeFileLog } from "./logger.js";
import { logConfig } from "./ui.js";

function usage(): never {
  process.stderr.write(
    `Usage: claude-pilot [options] <prompt>

Options:
  --task-id <id>       Task identifier for external agent tracking
  --no-relay           Disable agent forwarding (answer all prompts locally)
  --relay-config <path> Explicit path to config JSON (overrides CWD discovery)
  --cwd <dir>          Working directory for Claude Code (default: current)
  --log-dir [path]     Enable file logging (default: /var/log/claude-pilot)
  --command <cmd>      Slash command to prepend to the prompt (e.g., /mika)
  --verbose            Show debug output
  --help               Show this help
`,
  );
  process.exit(1);
}

function parseArgs(argv: string[]): {
  prompt: string;
  relay: boolean;
  cwd: string;
  verbose: boolean;
  taskId?: string;
  logDir?: string;
  relayConfig?: string;
  command?: string;
} {
  const args = argv.slice(2);
  let relay = true;
  let cwd = process.cwd();
  let verbose = false;
  let taskId: string | undefined;
  let logDir: string | undefined;
  let relayConfig: string | undefined;
  let command: string | undefined;
  const positional: string[] = [];

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case "--no-relay":
        relay = false;
        break;
      case "--task-id": {
        const value = args[++i];
        if (!value || value.startsWith("-")) {
          process.stderr.write("Error: --task-id requires a value\n");
          usage();
        }
        taskId = value;
        break;
      }
      case "--relay-config": {
        const rcValue = args[++i];
        if (!rcValue || rcValue.startsWith("-")) {
          process.stderr.write("Error: --relay-config requires a path\n");
          usage();
        }
        relayConfig = resolve(rcValue);
        break;
      }
      case "--cwd":
        cwd = args[++i] ?? cwd;
        break;
      case "--log-dir": {
        const next = args[i + 1];
        if (next && !next.startsWith("-")) {
          logDir = args[++i];
        } else {
          logDir = "/var/log/claude-pilot";
        }
        break;
      }
      case "--command": {
        const cmdValue = args[++i];
        if (!cmdValue || cmdValue.startsWith("-")) {
          process.stderr.write("Error: --command requires a value\n");
          usage();
        }
        if (!cmdValue.startsWith("/")) {
          process.stderr.write("Error: --command must start with / (e.g., /mika)\n");
          usage();
        }
        command = cmdValue;
        break;
      }
      case "--verbose":
        verbose = true;
        break;
      case "--help":
      case "-h":
        usage();
        break;
      default:
        if (args[i].startsWith("-")) {
          process.stderr.write(`Unknown option: ${args[i]}\n`);
          usage();
        }
        positional.push(args[i]);
    }
  }

  if (positional.length === 0) {
    process.stderr.write("Error: prompt is required\n");
    usage();
  }

  return {
    prompt: positional.join(" "),
    relay,
    cwd: resolve(cwd),
    verbose,
    taskId: taskId || undefined, // treat empty string as absent
    logDir,
    relayConfig,
    command,
  };
}

function loadConfig(cwd: string, explicitPath?: string): PilotConfig | undefined {
  const configPath = explicitPath ?? resolve(cwd, ".claude", "claude-pilot.json");
  let raw: string;
  try {
    raw = readFileSync(configPath, "utf-8");
  } catch {
    return undefined; // file not found
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    process.stderr.write(
      `Error: Invalid JSON in ${configPath}: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    process.exit(1);
  }

  const result = PilotConfigSchema.safeParse(parsed);
  if (!result.success) {
    process.stderr.write(
      `Error: Invalid .claude/claude-pilot.json: ${result.error.issues.map((i) => i.message).join(", ")}\n`,
    );
    process.exit(1);
  }

  return result.data;
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv);
  const config = loadConfig(opts.cwd, opts.relayConfig);
  const configPath = opts.relayConfig ?? resolve(opts.cwd, ".claude", "claude-pilot.json");

  // Initialize file logging when --log-dir is present
  if (opts.logDir) {
    const sanitized = opts.taskId?.replace(/[^a-zA-Z0-9_-]/g, "_");
    const logName = sanitized ? `${sanitized}.log` : "session.log";
    const logPath = join(opts.logDir, logName);
    initFileLog(logPath);
  }

  // Log config discovery result (fires unconditionally for diagnostics)
  logConfig(opts.cwd, configPath, !!config, opts.relay && !!config);

  if (opts.relay && !config) {
    if (opts.relayConfig) {
      // Explicit path: hard error — user asked for this config specifically
      process.stderr.write(`Error: Config file not found: ${opts.relayConfig}\n`);
      process.exit(1);
    }
    // CWD discovery: warning + disable
    process.stderr.write(
      "Warning: No .claude/claude-pilot.json found — running in no-relay mode.\n" +
        "Create .claude/claude-pilot.json with {\"command\": \"...\", \"args\": [...]} to enable agent forwarding.\n\n",
    );
    opts.relay = false;
  }

  if (!opts.relay && opts.relayConfig) {
    process.stderr.write("Warning: --relay-config is ignored when --no-relay is active\n");
  }

  // Wire up graceful shutdown
  const abortController = new AbortController();
  const shutdown = () => {
    process.stderr.write("\nShutting down...\n");
    abortController.abort();
    closeFileLog();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  const permissionHandler = createPermissionHandler({
    ...(config && { config }),
    relay: opts.relay,
    verbose: opts.verbose,
  });

  const prompt = opts.command ? `${opts.command} ${opts.prompt}` : opts.prompt;

  await runAgent({
    prompt,
    cwd: opts.cwd,
    verbose: opts.verbose,
    taskId: opts.taskId,
    permissionHandler,
    abortController,
  });

  closeFileLog();
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
