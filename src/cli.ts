#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { PilotConfig } from "./types.js";
import { PilotConfigSchema } from "./types.js";
import { createPermissionHandler } from "./permissions.js";
import { runAgent } from "./agent.js";
import { initFileLog, closeFileLog } from "./logger.js";

function usage(): never {
  process.stderr.write(
    `Usage: claude-pilot [options] <prompt>

Options:
  --task-id <id>  Task identifier for external agent tracking
  --no-relay      Disable agent forwarding (answer all prompts locally)
  --cwd <dir>     Working directory for Claude Code (default: current)
  --log-dir [path] Enable file logging (default: /var/log/claude-pilot)
  --verbose       Show debug output
  --help          Show this help
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
} {
  const args = argv.slice(2);
  let relay = true;
  let cwd = process.cwd();
  let verbose = false;
  let taskId: string | undefined;
  let logDir: string | undefined;
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
  };
}

function loadConfig(cwd: string): PilotConfig | undefined {
  const configPath = resolve(cwd, ".claude", "claude-pilot.json");
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
  const config = loadConfig(opts.cwd);

  // Initialize file logging when --log-dir is present
  if (opts.logDir) {
    const logName = opts.taskId ? `${opts.taskId}.log` : "session.log";
    const logPath = join(opts.logDir, logName);
    initFileLog(logPath);
  }

  if (opts.relay && !config) {
    process.stderr.write(
      "Warning: No .claude/claude-pilot.json found — running in no-relay mode.\n" +
        "Create .claude/claude-pilot.json with {\"command\": \"...\", \"args\": [...]} to enable agent forwarding.\n\n",
    );
    opts.relay = false;
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
    config: config ?? { command: "" },
    relay: opts.relay,
    verbose: opts.verbose,
    taskId: opts.taskId,
  });

  await runAgent({
    prompt: opts.prompt,
    cwd: opts.cwd,
    verbose: opts.verbose,
    taskId: opts.taskId,
    permissionHandler,
    abortController,
  });

  closeFileLog();
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
  process.exit(1);
});
