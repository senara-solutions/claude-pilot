const RESET = "\x1b[0m";
const DIM = "\x1b[2m";
const BOLD = "\x1b[1m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const CYAN = "\x1b[36m";
const MAGENTA = "\x1b[35m";

function log(msg: string): void {
  process.stderr.write(msg + "\n");
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
  process.stderr.write(`${DIM}${text}${RESET}`);
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

export function logForwarded(toolName: string): void {
  log(`${DIM}[relay]${RESET} ${toolName} → forwarded to agent`);
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
