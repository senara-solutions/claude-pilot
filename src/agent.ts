import { query, AbortError } from "@anthropic-ai/claude-agent-sdk";

import type { PermissionHandler } from "./permissions.js";
import type { GuardrailConfig, ResultJson } from "./types.js";
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
import { resolveGuardrailDefaults } from "./guardrails.js";

export interface AgentOptions {
  prompt: string;
  cwd: string;
  verbose: boolean;
  taskId?: string;
  permissionHandler: PermissionHandler;
  abortController: AbortController;
  guardrails: SessionGuardrails;
  guardrailConfig?: GuardrailConfig;
}

export async function runAgent(opts: AgentOptions): Promise<void> {
  const startTime = Date.now();
  let sessionId: string | undefined;
  const guardrails = opts.guardrails;

  // Resolve config for SDK-native options (maxTurns, maxBudgetUsd)
  const resolvedConfig = resolveGuardrailDefaults(opts.guardrailConfig);
  logGuardrailConfig(resolvedConfig);

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
      ...(resolvedConfig.maxTurns > 0 && {
        maxTurns: resolvedConfig.maxTurns,
      }),
      ...(resolvedConfig.maxBudgetUsd > 0 && {
        maxBudgetUsd: resolvedConfig.maxBudgetUsd,
      }),
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
          guardrails.onStreamActivity();
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
