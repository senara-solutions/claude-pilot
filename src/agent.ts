import { query, AbortError } from "@anthropic-ai/claude-agent-sdk";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import type { PermissionHandler } from "./permissions.js";
import {
  logInit,
  logText,
  logDone,
  logError,
} from "./ui.js";

export interface AgentOptions {
  prompt: string;
  cwd: string;
  verbose: boolean;
  permissionHandler: PermissionHandler;
  abortController: AbortController;
}

export async function runAgent(opts: AgentOptions): Promise<void> {
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
      // Capture session ID from init message for the permission handler
      if (message.type === "system" && message.subtype === "init") {
        opts.permissionHandler.setSessionId(message.session_id);
      }
      handleMessage(message);
    }
  } catch (err) {
    if (err instanceof AbortError) {
      process.stderr.write("\n");
      return;
    }
    throw err;
  }
}

function handleMessage(message: SDKMessage): void {
  switch (message.type) {
    case "system":
      if (message.subtype === "init") {
        logInit(message.session_id, message.model);
      }
      break;

    case "stream_event": {
      const event = message.event;
      if (
        event.type === "content_block_delta" &&
        "delta" in event &&
        event.delta.type === "text_delta"
      ) {
        logText(event.delta.text);
      }
      break;
    }

    case "result": {
      // Write structured JSON result to stdout for agent consumption
      const resultJson: Record<string, unknown> = {
        status: message.subtype === "success" ? "success" : "error",
        subtype: message.subtype,
        turns: message.num_turns,
        cost_usd: message.total_cost_usd,
        duration_ms: message.duration_ms,
      };
      if (message.subtype !== "success" && "errors" in message) {
        resultJson.errors = (message as { errors: string[] }).errors;
      }
      process.stdout.write(JSON.stringify(resultJson) + "\n");

      if (message.subtype === "success") {
        logDone(
          message.num_turns,
          message.total_cost_usd,
          message.duration_ms,
        );
      } else {
        const errors =
          "errors" in message ? (message as { errors: string[] }).errors : [];
        logError(message.subtype, errors);
        process.exitCode = 1;
      }
      break;
    }
  }
}
