import { execFile } from "node:child_process";
import type { PilotConfig, PilotEvent, PilotResponse } from "./types.js";
import { PilotResponseSchema } from "./types.js";
import { logVerbose } from "./ui.js";

const SCRUB_PATTERNS = [/KEY$/i, /SECRET/i, /TOKEN$/i, /PASSWORD/i, /CREDENTIAL/i];

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
  taskId?: string,
  sessionId?: string,
): Promise<PilotResponse> {
  const timeout = config.timeout ?? 120_000;

  const args = [...(config.args ?? [])];
  if (taskId) args.push("--task-id", taskId);
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
            const parsed = JSON.parse(stdout.trim());
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

export class TransportError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TransportError";
  }
}
