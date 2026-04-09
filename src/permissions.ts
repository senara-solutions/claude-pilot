import * as readline from "node:readline/promises";
import type { CanUseTool, PermissionResult } from "@anthropic-ai/claude-agent-sdk";
import type { PilotConfig, PilotEvent, PilotResponse } from "./types.js";
import type { SessionGuardrails } from "./guardrails.js";
import { invokeCommand, TransportError } from "./transport.js";
import { isTier1AutoApprove } from "./tier1.js";
import {
  logRelaySend,
  logRelayRecv,
  logToolRequest,
  logTool,
  logQuestion,
  logDenied,
  logRetry,
  logFallback,
  logEscalate,
  logQuestionEscalate,
  logVerbose,
} from "./ui.js";

interface PermissionHandlerOptions {
  config?: PilotConfig;
  relay: boolean;
  verbose: boolean;
  cwd: string;
  guardrails?: SessionGuardrails;
  taskId?: string;
}

export type PermissionHandler = CanUseTool;

export function createPermissionHandler(
  opts: PermissionHandlerOptions,
): PermissionHandler {
  const handler: CanUseTool = async (toolName, input, sdkOptions) => {
    // Log every tool request before decision logic
    logToolRequest(toolName, summarizeInput(toolName, input));

    // Tier 1 auto-approval: skip relay for safe operations
    if (isTier1AutoApprove(toolName, input, opts.cwd)) {
      logTool(toolName, summarizeInput(toolName, input), "AUTO");
      return { behavior: "allow", updatedInput: input };
    }

    // Relay disabled or no config: go straight to interactive
    if (!opts.relay || !opts.config) {
      return interactiveFallback(toolName, input);
    }

    // Build event payload
    const event: PilotEvent = {
      type: toolName === "AskUserQuestion" ? "question" : "permission",
      tool_name: toolName,
      tool_input: input,
      tool_use_id: sdkOptions.toolUseID,
      agent_id: sdkOptions.agentID,
      decision_reason: sdkOptions.decisionReason,
      blocked_path: sdkOptions.blockedPath,
    };

    logRelaySend(toolName);

    // Pause idle timer during relay — relay agent may take 60-120s (human escalation)
    opts.guardrails?.pauseIdleTimer();

    // Attempt 1
    let start = Date.now();
    try {
      const response = await invokeCommand(
        opts.config,
        event,
        sdkOptions.signal,
        opts.verbose,
        opts.taskId,
      );
      logRelayRecv(toolName, response.action, Date.now() - start);
      opts.guardrails?.resumeIdleTimer();
      return mapResponse(toolName, input, response);
    } catch (err) {
      if (err instanceof TransportError) {
        logRelayRecv(toolName, "error", Date.now() - start);
        // Retry once with error feedback
        logRetry(`${err.message} — retrying with error feedback`);

        const retryEvent: PilotEvent = {
          ...event,
          error: `Previous response was malformed: ${err.message}. Expected JSON: {"action": "allow"} or {"action": "deny"} or {"action": "answer", "answers": {"question": "answer"}}`,
        };

        start = Date.now();
        try {
          const response = await invokeCommand(
            opts.config,
            retryEvent,
            sdkOptions.signal,
            opts.verbose,
            opts.taskId,
          );
          logRelayRecv(toolName, response.action, Date.now() - start);
          opts.guardrails?.resumeIdleTimer();
          return mapResponse(toolName, input, response);
        } catch (retryErr) {
          opts.guardrails?.resumeIdleTimer();
          // Propagate abort errors — don't fall back to interactive on shutdown
          if (retryErr instanceof Error && retryErr.name === "AbortError") {
            throw retryErr;
          }
          logRelayRecv(toolName, "error", Date.now() - start);
          // Second failure: fall back to user
          const reason =
            retryErr instanceof Error ? retryErr.message : String(retryErr);
          logFallback(reason);
          return interactiveFallback(toolName, input);
        }
      }
      opts.guardrails?.resumeIdleTimer();
      // AbortError or unexpected: rethrow
      throw err;
    }
  };

  return handler;
}

function mapResponse(
  toolName: string,
  originalInput: Record<string, unknown>,
  response: PilotResponse,
): PermissionResult {
  switch (response.action) {
    case "allow":
      logTool(toolName, summarizeInput(toolName, originalInput), "ALLOW");
      return { behavior: "allow", updatedInput: originalInput };

    case "deny":
      logDenied(toolName, summarizeInput(toolName, originalInput));
      return {
        behavior: "deny",
        message: response.message ?? "Denied by external agent",
      };

    case "answer": {
      const firstQuestion = Object.keys(response.answers)[0] ?? "";
      const firstAnswer = Object.values(response.answers)[0] ?? "";
      logQuestion(firstQuestion, firstAnswer);

      return {
        behavior: "allow",
        updatedInput: {
          questions: originalInput.questions,
          answers: response.answers,
        },
      };
    }
  }
}

async function interactiveFallback(
  toolName: string,
  input: Record<string, unknown>,
): Promise<PermissionResult> {
  // Non-interactive: auto-deny
  if (!process.stdin.isTTY) {
    logDenied(toolName, "non-interactive mode — auto-denied");
    return { behavior: "deny", message: "Non-interactive mode: auto-denied" };
  }

  if (toolName === "AskUserQuestion") {
    return interactiveQuestion(input);
  }

  return interactivePermission(toolName, input);
}

async function interactivePermission(
  toolName: string,
  input: Record<string, unknown>,
): Promise<PermissionResult> {
  const detail = summarizeInput(toolName, input);
  logEscalate(toolName, detail);

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stderr,
  });

  try {
    const answer = await rl.question("  Allow? (y/n): ");
    const allowed = answer.trim().toLowerCase().startsWith("y");

    if (allowed) {
      logTool(toolName, detail, "ALLOW");
      return { behavior: "allow", updatedInput: input };
    } else {
      logDenied(toolName, detail);
      return { behavior: "deny", message: "Denied by user" };
    }
  } finally {
    rl.close();
  }
}

async function interactiveQuestion(
  input: Record<string, unknown>,
): Promise<PermissionResult> {
  const questions = input.questions;
  if (!Array.isArray(questions)) {
    return { behavior: "deny", message: "Malformed AskUserQuestion: missing questions array" };
  }

  const answers: Record<string, string> = {};

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stderr,
  });

  try {
    for (const q of questions) {
      const qObj = q as { question: string; options?: Array<{ label: string }> };
      logQuestionEscalate(qObj.question);

      if (qObj.options && qObj.options.length > 0) {
        for (let i = 0; i < qObj.options.length; i++) {
          process.stderr.write(`  ${i + 1}. ${qObj.options[i].label}\n`);
        }
        const answer = await rl.question("\n  Your answer: ");
        // Try to parse as number (option index)
        const num = parseInt(answer.trim(), 10);
        if (num >= 1 && num <= qObj.options.length) {
          answers[qObj.question] = qObj.options[num - 1].label;
        } else {
          answers[qObj.question] = answer.trim();
        }
      } else {
        const answer = await rl.question("\n  Your answer: ");
        answers[qObj.question] = answer.trim();
      }
    }
  } finally {
    rl.close();
  }

  logQuestion(questions[0]?.question ?? "", Object.values(answers)[0] ?? "");

  return {
    behavior: "allow",
    updatedInput: {
      questions: input.questions,
      answers,
    },
  };
}

function scrubSecrets(text: string): string {
  return text
    .replace(/(Bearer\s+)\S+/gi, "$1[REDACTED]")
    .replace(/(sk-ant-\S{0,6})\S*/g, "$1...[REDACTED]")
    .replace(/(ghp_\S{0,4})\S*/g, "$1...[REDACTED]")
    .replace(/(xoxb-\S{0,4})\S*/g, "$1...[REDACTED]")
    .replace(/(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|API_KEY)=\S+/gi, "$1=[REDACTED]");
}

function summarizeInput(
  toolName: string,
  input: Record<string, unknown>,
): string {
  switch (toolName) {
    case "Bash":
      return scrubSecrets(String(input.command ?? "").slice(0, 200));
    case "Write":
    case "Edit":
    case "Read":
      return String(input.file_path ?? "");
    case "Glob":
    case "Grep":
      return String(input.pattern ?? "");
    case "Skill": {
      const skill = String(input.skill ?? "unknown");
      const args = input.args ? ` ${String(input.args).slice(0, 100)}` : "";
      return `${skill}${args}`;
    }
    default:
      return scrubSecrets(JSON.stringify(input).slice(0, 150));
  }
}
