import { writeLog, writeFileLog } from "./logger.js";
import type { GuardrailConfig } from "./types.js";

const RESET = "\x1b[0m";
const DIM = "\x1b[2m";
const BOLD = "\x1b[1m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const CYAN = "\x1b[36m";
const MAGENTA = "\x1b[35m";
const ORANGE = "\x1b[38;5;208m";

function log(msg: string): void {
  writeLog(msg + "\n");
}

export function logInit(sessionId: string, model: string, taskId?: string): void {
  const taskStr = taskId ? `, task ${taskId}` : "";
  log(`${DIM}[init]${RESET} Session ${sessionId.slice(0, 8)}, model ${model}${taskStr}`);
}

export function logTool(
  toolName: string,
  detail: string,
  decision?: string,
): void {
  const decisionStr = decision
    ? ` → ${decision === "ALLOW" ? GREEN : decision === "DENY" ? RED : YELLOW}${decision}${RESET}`
    : "";
  log(`${DIM}[tool]${RESET} ${BOLD}${toolName}${RESET}: ${detail}${decisionStr}`);
}

export function logQuestion(
  question: string,
  answer?: string,
): void {
  const answerStr = answer ? ` → ${GREEN}"${answer}"${RESET}` : "";
  log(`${MAGENTA}[question]${RESET} "${question}"${answerStr}`);
}

export function logText(text: string): void {
  writeLog(`${DIM}${text}${RESET}`);
}

export function logDone(
  turns: number,
  costUsd: number,
  durationMs: number,
): void {
  const secs = (durationMs / 1000).toFixed(0);
  log(
    `\n${GREEN}[done]${RESET} Success | ${turns} turns | $${costUsd.toFixed(2)} | ${secs}s`,
  );
}

export function logError(subtype: string, errors: string[]): void {
  log(`\n${RED}[error]${RESET} ${subtype}: ${errors.join(", ")}`);
}

export function logDenied(toolName: string, detail: string): void {
  log(`${RED}[denied]${RESET} ${toolName}: ${detail}`);
}

export function logRetry(reason: string): void {
  log(`${YELLOW}[retry]${RESET} ${reason}`);
}

export function logFallback(reason: string): void {
  log(`${YELLOW}[fallback]${RESET} ${reason} — answering from claude-pilot`);
}

export function logConfig(cwd: string, configPath: string, found: boolean, relay: boolean): void {
  const status = found ? "found" : "NOT FOUND";
  const relayStr = relay ? "enabled" : "disabled";
  log(`${DIM}[config]${RESET} cwd=${cwd} config=${configPath} [${status}] relay=${relayStr}`);
}

export function logToolRequest(toolName: string, detail: string): void {
  log(`${DIM}[tool:request]${RESET} ${BOLD}${toolName}${RESET}: ${detail}`);
}

export function logRelaySend(toolName: string): void {
  log(`${DIM}[relay:send]${RESET} ${toolName} → agent`);
}

export function logRelayRecv(toolName: string, action: string, latencyMs: number): void {
  const color = action === "allow" ? GREEN : action === "deny" ? RED : YELLOW;
  log(`${DIM}[relay:recv]${RESET} ${toolName} ← ${color}${action}${RESET} (${latencyMs}ms)`);
}

export function logVerbose(msg: string): void {
  log(`${DIM}[debug] ${msg}${RESET}`);
}

export function logEscalate(toolName: string, detail: string): void {
  log(`${CYAN}[ESCALATE]${RESET} Claude wants to use: ${BOLD}${toolName}${RESET}`);
  log(`  ${detail}`);
}

export function logQuestionEscalate(question: string): void {
  log(`${CYAN}[QUESTION]${RESET} ${question}`);
}

export function logPrompt(prompt: string): void {
  writeFileLog(`[prompt] ${prompt}\n`);
}

export function logGuardrail(type: string, detail: string): void {
  log(
    `\n${ORANGE}[guardrail]${RESET} ${BOLD}${type}${RESET}: ${detail}`,
  );
}

export function logGuardrailConfig(config: Required<GuardrailConfig>): void {
  const parts = [
    `maxTurns=${config.maxTurns}`,
    config.stallThreshold > 0 ? `stallThreshold=${config.stallThreshold}` : null,
    config.emptyResponseThreshold > 0
      ? `emptyResponseThreshold=${config.emptyResponseThreshold}`
      : null,
    config.idleTimeoutMs > 0
      ? `idleTimeout=${config.idleTimeoutMs / 1000}s`
      : null,
    config.maxBudgetUsd > 0 ? `maxBudget=$${config.maxBudgetUsd}` : null,
  ].filter(Boolean);
  log(`${DIM}[guardrails]${RESET} ${parts.join(" ")}`);
}
