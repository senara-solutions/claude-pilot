import { query, AbortError } from "@anthropic-ai/claude-agent-sdk";

import type { PermissionHandler } from "./permissions.js";
import type { ResultJson } from "./types.js";
import { isGuardrailAbortReason } from "./types.js";
import type { SessionGuardrails } from "./guardrails.js";
import {
  logInit,
  logPrompt,
  logText,
  logDone,
  logError,
  logGuardrail,
  logGuardrailConfig,
} from "./ui.js";

export interface AgentOptions {
  prompt: string;
  cwd: string;
  verbose: boolean;
  taskId?: string;
  permissionHandler: PermissionHandler;
  abortController: AbortController;
  guardrails: SessionGuardrails;
}

/** SDK result subtypes that indicate a guardrail-like termination. */
const SDK_TERMINATION_SUBTYPES = new Set([
  "error_max_turns",
  "error_max_budget_usd",
]);

export async function runAgent(opts: AgentOptions): Promise<void> {
  const startTime = Date.now();
  let sessionId: string | undefined;
  const guardrails = opts.guardrails;
  const config = guardrails.config;

  logGuardrailConfig(config);

  const q = query({
    prompt: opts.prompt,
    options: {
      permissionMode: "default",
      includePartialMessages: true,
      cwd: opts.cwd,
      abortController: opts.abortController,
      settingSources: ["user", "project", "local"],
      canUseTool: opts.permissionHandler,
      // SDK-native guardrails
      ...(config.maxTurns > 0 && { maxTurns: config.maxTurns }),
      ...(config.maxBudgetUsd > 0 && { maxBudgetUsd: config.maxBudgetUsd }),
    },
  });

  try {
    for await (const message of q) {
      if (message.type === "system" && message.subtype === "init") {
        sessionId = message.session_id;
        logInit(sessionId, message.model, opts.taskId);
        logPrompt(opts.prompt);
        continue;
      }

      // Turn boundary: complete assistant response with content blocks
      if (message.type === "assistant") {
        guardrails.onAssistantMessage(message);
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
        const rawErrors =
          message.subtype !== "success" && "errors" in message
            ? (message as Record<string, unknown>).errors
            : undefined;
        const errors =
          Array.isArray(rawErrors) &&
          rawErrors.every((e): e is string => typeof e === "string")
            ? rawErrors
            : undefined;

        // Normalize SDK-native guardrail results to "terminated" status
        const isSdkTermination = SDK_TERMINATION_SUBTYPES.has(message.subtype);

        const resultJson: ResultJson = {
          status: message.subtype === "success"
            ? "success"
            : isSdkTermination
              ? "terminated"
              : "error",
          subtype: message.subtype,
          ...(opts.taskId && { task_id: opts.taskId }),
          ...(sessionId && { session_id: sessionId }),
          turns: message.num_turns,
          cost_usd: message.total_cost_usd,
          duration_ms: message.duration_ms,
          ...(errors && { errors }),
          ...(isSdkTermination && {
            termination_reason: `SDK limit reached: ${message.subtype}`,
          }),
        };
        process.stdout.write(JSON.stringify(resultJson) + "\n");

        if (message.subtype === "success") {
          logDone(
            message.num_turns,
            message.total_cost_usd,
            message.duration_ms,
          );
        } else if (isSdkTermination) {
          logGuardrail(message.subtype, `SDK limit reached after ${message.num_turns} turns`);
          process.exitCode = 1;
        } else {
          logError(message.subtype, errors ?? []);
          process.exitCode = 1;
        }
      }
    }
  } catch (err) {
    if (err instanceof AbortError) {
      const reason = opts.abortController.signal.reason;
      if (isGuardrailAbortReason(reason)) {
        // Guardrail-initiated abort: emit structured ResultJson
        const resultJson: ResultJson = {
          status: "terminated",
          subtype: reason.guardrail,
          ...(opts.taskId && { task_id: opts.taskId }),
          ...(sessionId && { session_id: sessionId }),
          turns: guardrails.turns,
          cost_usd: 0, // not available on abort
          duration_ms: Date.now() - startTime,
          termination_reason: reason.detail,
        };
        process.stdout.write(JSON.stringify(resultJson) + "\n");
        logGuardrail(reason.guardrail, reason.detail);
        process.exitCode = 1;
      } else {
        // User-initiated abort (SIGINT/SIGTERM)
        process.stderr.write("\n");
      }
      return;
    }
    throw err;
  } finally {
    guardrails.dispose();
  }
}
