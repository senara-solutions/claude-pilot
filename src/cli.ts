#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { PilotConfig } from "./types.js";
import { PilotConfigSchema } from "./types.js";
import { createPermissionHandler } from "./permissions.js";
import { runAgent } from "./agent.js";

function usage(): never {
  process.stderr.write(
    `Usage: claude-pilot [options] <prompt>

Options:
  --no-relay    Disable agent forwarding (answer all prompts locally)
  --cwd <dir>   Working directory for Claude Code (default: current)
  --verbose     Show debug output
  --help        Show this help
`,
  );
  process.exit(1);
}

function parseArgs(argv: string[]): {
  prompt: string;
  relay: boolean;
  cwd: string;
  verbose: boolean;
} {
  const args = argv.slice(2);
  let relay = true;
  let cwd = process.cwd();
  let verbose = false;
  const positional: string[] = [];

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case "--no-relay":
        relay = false;
        break;
      case "--cwd":
        cwd = args[++i] ?? cwd;
        break;
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
  };
}

function loadConfig(cwd: string): PilotConfig | undefined {
  const configPath = resolve(cwd, ".claude-pilot.json");
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
      `Error: Invalid .claude-pilot.json: ${result.error.issues.map((i) => i.message).join(", ")}\n`,
    );
    process.exit(1);
  }

  return result.data;
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv);
  const config = loadConfig(opts.cwd);

  if (opts.relay && !config) {
    process.stderr.write(
      "Warning: No .claude-pilot.json found — running in no-relay mode.\n" +
        "Create a .claude-pilot.json with {\"command\": \"...\", \"args\": [...]} to enable agent forwarding.\n\n",
    );
    opts.relay = false;
  }

  // Wire up graceful shutdown
  const abortController = new AbortController();
  const shutdown = () => {
    process.stderr.write("\nShutting down...\n");
    abortController.abort();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  const permissionHandler = createPermissionHandler({
    config: config ?? { command: "" },
    relay: opts.relay,
    verbose: opts.verbose,
    cwd: opts.cwd,
  });

  await runAgent({
    prompt: opts.prompt,
    cwd: opts.cwd,
    verbose: opts.verbose,
    permissionHandler,
    abortController,
  });
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
  process.exit(1);
});
