import { query, AbortError } from "@anthropic-ai/claude-agent-sdk";

import type { PermissionHandler } from "./permissions.js";
import type { ResultJson } from "./types.js";
import {
  logInit,
  logPrompt,
  logText,
  logDone,
  logError,
} from "./ui.js";

export interface AgentOptions {
  prompt: string;
  cwd: string;
  verbose: boolean;
  taskId?: string;
  permissionHandler: PermissionHandler;
  abortController: AbortController;
}

export async function runAgent(opts: AgentOptions): Promise<void> {
  let sessionId: string | undefined;

  const q = query({
    prompt: opts.prompt,
    options: {
      permissionMode: "default",
      includePartialMessages: true,
      cwd: opts.cwd,
      abortController: opts.abortController,
      settingSources: ["user", "project", "local"],
      canUseTool: opts.permissionHandler,
    },
  });

  try {
    for await (const message of q) {
      if (message.type === "system" && message.subtype === "init") {
        sessionId = message.session_id;
        opts.permissionHandler.setSessionId(sessionId);
        logInit(sessionId, message.model, opts.taskId);
        logPrompt(opts.prompt);
        continue;
      }

      if (message.type === "stream_event") {
        const event = message.event;
        if (
          event.type === "content_block_delta" &&
          "delta" in event &&
          event.delta.type === "text_delta"
        ) {
          logText(event.delta.text);
        }
        continue;
      }

      if (message.type === "result") {
        const errors =
          message.subtype !== "success" && "errors" in message
            ? (message as { errors: string[] }).errors
            : undefined;

        const resultJson: ResultJson = {
          status: message.subtype === "success" ? "success" : "error",
          subtype: message.subtype,
          ...(opts.taskId && { task_id: opts.taskId }),
          ...(sessionId && { session_id: sessionId }),
          turns: message.num_turns,
          cost_usd: message.total_cost_usd,
          duration_ms: message.duration_ms,
          ...(errors && { errors }),
        };
        process.stdout.write(JSON.stringify(resultJson) + "\n");

        if (message.subtype === "success") {
          logDone(
            message.num_turns,
            message.total_cost_usd,
            message.duration_ms,
          );
        } else {
          logError(message.subtype, errors ?? []);
          process.exitCode = 1;
        }
      }
    }
  } catch (err) {
    if (err instanceof AbortError) {
      process.stderr.write("\n");
      return;
    }
    throw err;
  }
}
