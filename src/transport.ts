import { execFile } from "node:child_process";
import type { PilotConfig, PilotEvent, PilotResponse } from "./types.js";
import { PilotResponseSchema } from "./types.js";
import { writeFileLog } from "./logger.js";
import { logVerbose } from "./ui.js";

const SCRUB_PATTERNS = [/KEY/i, /SECRET/i, /TOKEN/i, /PASSWORD/i, /CREDENTIAL/i, /^DATABASE_URL$/i, /DSN$/i, /AUTH/i, /PRIVATE/i];

function scrubEnv(env: NodeJS.ProcessEnv): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter(
      ([key, value]) => value !== undefined && !SCRUB_PATTERNS.some((p) => p.test(key)),
    ),
  ) as Record<string, string>;
}

export async function invokeCommand(
  config: PilotConfig,
  event: PilotEvent,
  signal: AbortSignal,
  verbose: boolean,
  sessionId?: string,
): Promise<PilotResponse> {
  const timeout = config.timeout ?? 120_000;

  const args = [...(config.args ?? []), "-"];
  if (config.model) args.push("--model", config.model);
  if (sessionId) args.push("--session-id", sessionId);

  if (verbose) {
    logVerbose(`invoking: ${config.command} ${args.join(" ")}`);
  }

  return new Promise<PilotResponse>((resolve, reject) => {
    const child = execFile(
      config.command,
      args,
      {
        timeout,
        env: scrubEnv(process.env),
        maxBuffer: 1024 * 1024,
        signal,
      },
      (error, stdout, stderr) => {
        // Always try to parse stdout first, even on non-zero exit
        // (institutional learning: capture first, interpret second)
        if (stdout.trim()) {
          try {
            const parsed = extractJson(stdout);
            if (verbose && stdout.trim() !== JSON.stringify(parsed)) {
              logVerbose(
                `extracted JSON from noisy stdout (${stdout.length} bytes)`,
              );
            }
            const result = PilotResponseSchema.safeParse(parsed);
            if (result.success) {
              resolve(result.data);
              return;
            }
            reject(
              new TransportError(
                `Invalid response schema: ${JSON.stringify(result.error.issues)}`,
              ),
            );
            return;
          } catch {
            reject(
              new TransportError(
                `Invalid JSON from command: ${stdout.trim().slice(0, 200)}`,
              ),
            );
            return;
          }
        }

        if (error) {
          if (error.name === "AbortError") {
            reject(error);
            return;
          }
          reject(
            new TransportError(
              `Command failed: ${error.message}${stderr ? ` — stderr: ${stderr.trim().slice(0, 200)}` : ""}`,
            ),
          );
          return;
        }

        reject(new TransportError("Command produced no output"));
      },
    );

    // Log relay metadata to file for debugging (content redacted to avoid secret leakage)
    if (verbose) {
      writeFileLog(
        `[relay:payload] type=${event.type} tool=${event.tool_name} id=${event.tool_use_id}\n`,
      );
    }

    // Write event payload to stdin
    if (child.stdin) {
      child.stdin.on("error", () => {
        // Ignore broken pipe — child may have exited before we finished writing
      });
      child.stdin.write(JSON.stringify(event));
      child.stdin.end();
    }
  });
}

/**
 * Extract the first valid JSON object from a string that may contain
 * surrounding text (preamble, markdown fences, trailing commentary).
 * Uses bracket-matching to handle nested objects correctly.
 */
function extractJson(raw: string): unknown {
  // Fast path: entire string is valid JSON
  const trimmed = raw.trim();
  try {
    return JSON.parse(trimmed);
  } catch {
    // Continue to extraction
  }

  const start = raw.indexOf("{");
  if (start === -1) {
    throw new Error("no JSON object found in output");
  }

  // Bracket-match to find the first complete object
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < raw.length; i++) {
    const ch = raw[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === "\\" && inString) {
      escape = true;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (ch === "{") depth++;
    if (ch === "}") {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(raw.slice(start, i + 1));
        } catch {
          // Malformed segment — stop scanning
          break;
        }
      }
    }
  }

  // Last resort: first '{' to last '}'
  const end = raw.lastIndexOf("}");
  if (end > start) {
    return JSON.parse(raw.slice(start, end + 1));
  }

  throw new Error("no JSON object found in output");
}

export class TransportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TransportError";
  }
}
