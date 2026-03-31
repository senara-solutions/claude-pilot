#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";
import type { PilotConfig, GuardrailConfig } from "./types.js";
import { PilotConfigSchema } from "./types.js";
import { createPermissionHandler } from "./permissions.js";
import { runAgent } from "./agent.js";
import { SessionGuardrails, resolveGuardrailDefaults } from "./guardrails.js";
import { initFileLog, closeFileLog } from "./logger.js";
import { logConfig, logEnv } from "./ui.js";

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

Guardrail options:
  --max-turns <n>      Maximum agentic turns (default: 200)
  --max-budget <usd>   Maximum cost in USD (default: disabled)
  --stall-threshold <n> Consecutive no-tool turns before termination (0=off, default: 5)
  --empty-threshold <n> Consecutive trivial responses before termination (0=off, default: 5)
  --idle-timeout <ms>  Idle timeout in ms (0=off, max 3600000, default: 300000)
  --min-detection-turns <n> Turns before stall/empty detection activates (default: 10)
  --no-guardrails      Disable stall/empty/idle detection (maxTurns still applies)
`,
  );
  process.exit(1);
}

interface ParsedArgs {
  prompt: string;
  relay: boolean;
  cwd: string;
  verbose: boolean;
  taskId?: string;
  logDir?: string;
  relayConfig?: string;
  command?: string;
  guardrailOverrides: Partial<GuardrailConfig>;
  noGuardrails: boolean;
}

function parseArgs(argv: string[]): ParsedArgs {
  const args = argv.slice(2);
  let relay = true;
  let cwd = process.cwd();
  let verbose = false;
  let taskId: string | undefined;
  let logDir: string | undefined;
  let relayConfig: string | undefined;
  let command: string | undefined;
  let noGuardrails = false;
  const guardrailOverrides: Partial<GuardrailConfig> = {};
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
      case "--max-turns": {
        const v = parseInt(args[++i], 10);
        if (isNaN(v) || v < 1) {
          process.stderr.write("Error: --max-turns requires a positive integer\n");
          usage();
        }
        guardrailOverrides.maxTurns = v;
        break;
      }
      case "--max-budget": {
        const v = parseFloat(args[++i]);
        if (isNaN(v) || v < 0.01) {
          process.stderr.write("Error: --max-budget requires a positive number\n");
          usage();
        }
        guardrailOverrides.maxBudgetUsd = v;
        break;
      }
      case "--stall-threshold": {
        const v = parseInt(args[++i], 10);
        if (isNaN(v) || v < 0) {
          process.stderr.write("Error: --stall-threshold requires a non-negative integer (0 = disabled)\n");
          usage();
        }
        guardrailOverrides.stallThreshold = v;
        break;
      }
      case "--empty-threshold": {
        const v = parseInt(args[++i], 10);
        if (isNaN(v) || v < 0) {
          process.stderr.write("Error: --empty-threshold requires a non-negative integer (0 = disabled)\n");
          usage();
        }
        guardrailOverrides.emptyResponseThreshold = v;
        break;
      }
      case "--idle-timeout": {
        const v = parseInt(args[++i], 10);
        if (isNaN(v) || (v !== 0 && v < 1000) || v > 3_600_000) {
          process.stderr.write("Error: --idle-timeout must be 0 (disabled) or 1000-3600000 (ms)\n");
          usage();
        }
        guardrailOverrides.idleTimeoutMs = v;
        break;
      }
      case "--min-detection-turns": {
        const v = parseInt(args[++i], 10);
        if (isNaN(v) || v < 0) {
          process.stderr.write("Error: --min-detection-turns requires a non-negative integer\n");
          usage();
        }
        guardrailOverrides.minTurnsBeforeDetection = v;
        break;
      }
      case "--no-guardrails":
        noGuardrails = true;
        break;
      case "--verbose":
        verbose = true;
        break;
      case "--help":
      case "-h":
        usage();
        break;
      case "--":
        positional.push(...args.slice(i + 1));
        i = args.length;
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
    guardrailOverrides,
    noGuardrails,
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

/**
 * Merge guardrail config: config file < CLI overrides.
 * --no-guardrails disables application-level guardrails (stall, empty, idle)
 * but preserves SDK-native maxTurns.
 */
function mergeGuardrailConfig(
  fileConfig?: GuardrailConfig,
  cliOverrides?: Partial<GuardrailConfig>,
  noGuardrails?: boolean,
): GuardrailConfig {
  const merged: GuardrailConfig = {
    ...fileConfig,
    ...cliOverrides,
  };

  if (noGuardrails) {
    merged.stallThreshold = 0;
    merged.emptyResponseThreshold = 0;
    merged.idleTimeoutMs = 0;
  }

  return merged;
}

async function main(): Promise<void> {
  // Load .env from package root (does not override existing env vars)
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = dirname(__filename);
  const envPath = resolve(__dirname, "..", ".env");
  const envResult = dotenv.config({ path: envPath, override: false });

  const opts = parseArgs(process.argv);

  // Log .env discovery result (verbose only — fires after parseArgs to know --verbose)
  if (opts.verbose) {
    logEnv(envPath, envResult);
  }

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

  // Merge guardrail config: file < CLI overrides
  const guardrailConfig = mergeGuardrailConfig(
    config?.guardrails,
    opts.guardrailOverrides,
    opts.noGuardrails,
  );

  // Wire up graceful shutdown
  const abortController = new AbortController();
  const shutdown = () => {
    process.stderr.write("\nShutting down...\n");
    abortController.abort();
    closeFileLog();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  // Create shared guardrails instance — used by both agent loop and permission handler
  const resolvedGuardrails = resolveGuardrailDefaults(guardrailConfig);
  const guardrails = new SessionGuardrails(resolvedGuardrails, abortController);

  const permissionHandler = createPermissionHandler({
    ...(config && { config }),
    relay: opts.relay,
    verbose: opts.verbose,
    cwd: opts.cwd,
    guardrails,
  });

  const prompt = opts.command ? `${opts.command} ${opts.prompt}` : opts.prompt;

  await runAgent({
    prompt,
    cwd: opts.cwd,
    verbose: opts.verbose,
    taskId: opts.taskId,
    permissionHandler,
    abortController,
    guardrails,
  });

  closeFileLog();
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
