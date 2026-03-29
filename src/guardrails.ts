import type { SDKAssistantMessage } from "@anthropic-ai/claude-agent-sdk";

import type { GuardrailConfig, GuardrailAbortReason } from "./types.js";
import { GUARDRAIL_DEFAULTS } from "./types.js";

export function resolveGuardrailDefaults(
  config?: GuardrailConfig,
): Required<GuardrailConfig> {
  return {
    ...GUARDRAIL_DEFAULTS,
    ...Object.fromEntries(
      Object.entries(config ?? {}).filter(([, v]) => v !== undefined),
    ),
  } as Required<GuardrailConfig>;
}

interface GuardrailState {
  turnCount: number;
  consecutiveStallTurns: number;
  consecutiveEmptyTurns: number;
  idleTimer: ReturnType<typeof setTimeout> | null;
}

export class SessionGuardrails {
  private state: GuardrailState;
  private readonly resolvedConfig: Required<GuardrailConfig>;
  private abortController: AbortController;

  constructor(
    config: Required<GuardrailConfig>,
    abortController: AbortController,
  ) {
    this.resolvedConfig = config;
    this.abortController = abortController;
    this.state = {
      turnCount: 0,
      consecutiveStallTurns: 0,
      consecutiveEmptyTurns: 0,
      idleTimer: null,
    };

    // Start the initial idle timer
    this.resetIdleTimer();
  }

  /** Resolved guardrail config (for SDK-native options like maxTurns). */
  get config(): Required<GuardrailConfig> {
    return this.resolvedConfig;
  }

  /**
   * Called on each SDKAssistantMessage to evaluate turn-level guardrails.
   * This is the turn boundary — a complete assistant response.
   */
  onAssistantMessage(message: SDKAssistantMessage): void {
    this.state.turnCount++;

    // Reset idle timer on every turn boundary (the agent is active)
    this.resetIdleTimer();

    // Skip detection during warm-up period
    if (this.state.turnCount < this.resolvedConfig.minTurnsBeforeDetection) {
      return;
    }

    const content = message.message.content;

    const hasToolUse = Array.isArray(content) &&
      content.some((block) => block.type === "tool_use");

    if (hasToolUse) {
      // Tool call = meaningful progress — reset all counters
      this.state.consecutiveStallTurns = 0;
      this.state.consecutiveEmptyTurns = 0;
      return;
    }

    // No tool use — check stall threshold
    this.state.consecutiveStallTurns++;
    if (
      this.resolvedConfig.stallThreshold > 0 &&
      this.state.consecutiveStallTurns >= this.resolvedConfig.stallThreshold
    ) {
      this.abort(
        "stall_detected",
        `${this.state.consecutiveStallTurns} consecutive turns with no tool calls`,
      );
      return;
    }

    // Check for empty/trivial text response
    const totalTextLength = Array.isArray(content)
      ? content
          .filter((block): block is Extract<typeof block, { type: "text" }> =>
            block.type === "text",
          )
          .reduce((sum, block) => sum + (block.text?.trim().length ?? 0), 0)
      : 0;

    if (totalTextLength < 10) {
      this.state.consecutiveEmptyTurns++;
      if (
        this.resolvedConfig.emptyResponseThreshold > 0 &&
        this.state.consecutiveEmptyTurns >= this.resolvedConfig.emptyResponseThreshold
      ) {
        this.abort(
          "empty_response",
          `${this.state.consecutiveEmptyTurns} consecutive trivial responses (<10 chars)`,
        );
        return;
      }
    } else {
      this.state.consecutiveEmptyTurns = 0;
    }
  }

  /** Pause idle timer during canUseTool execution (relay may take 60-120s). */
  pauseIdleTimer(): void {
    if (this.state.idleTimer) {
      clearTimeout(this.state.idleTimer);
      this.state.idleTimer = null;
    }
  }

  /** Resume idle timer after canUseTool returns (fresh full-duration timer). */
  resumeIdleTimer(): void {
    this.resetIdleTimer();
  }

  /** Clean up timers to prevent Node.js process hang. */
  dispose(): void {
    if (this.state.idleTimer) {
      clearTimeout(this.state.idleTimer);
      this.state.idleTimer = null;
    }
  }

  get turns(): number {
    return this.state.turnCount;
  }

  private resetIdleTimer(): void {
    if (this.state.idleTimer) clearTimeout(this.state.idleTimer);
    if (this.resolvedConfig.idleTimeoutMs <= 0) return;

    this.state.idleTimer = setTimeout(() => {
      this.abort(
        "idle_timeout",
        `No meaningful progress for ${Math.round(this.resolvedConfig.idleTimeoutMs / 1000)}s`,
      );
    }, this.resolvedConfig.idleTimeoutMs);
  }

  private abort(
    guardrail: GuardrailAbortReason["guardrail"],
    detail: string,
  ): void {
    // Prevent double-abort
    if (this.abortController.signal.aborted) return;

    const reason: GuardrailAbortReason = {
      type: "guardrail",
      guardrail,
      turns: this.state.turnCount,
      detail,
    };

    this.dispose();
    this.abortController.abort(reason);
  }
}
