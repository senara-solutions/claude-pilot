import type { SDKAssistantMessage } from "@anthropic-ai/claude-agent-sdk";

import type { GuardrailConfig, GuardrailAbortReason } from "./types.js";
import { GUARDRAIL_DEFAULTS } from "./types.js";
import { logGuardrail } from "./ui.js";

export type ResolvedGuardrailConfig = Required<GuardrailConfig>;

export function resolveGuardrailDefaults(
  config?: GuardrailConfig,
): ResolvedGuardrailConfig {
  return {
    maxTurns: config?.maxTurns ?? GUARDRAIL_DEFAULTS.maxTurns,
    maxBudgetUsd: config?.maxBudgetUsd ?? GUARDRAIL_DEFAULTS.maxBudgetUsd,
    stallThreshold: config?.stallThreshold ?? GUARDRAIL_DEFAULTS.stallThreshold,
    emptyResponseThreshold:
      config?.emptyResponseThreshold ??
      GUARDRAIL_DEFAULTS.emptyResponseThreshold,
    idleTimeoutMs: config?.idleTimeoutMs ?? GUARDRAIL_DEFAULTS.idleTimeoutMs,
    minTurnsBeforeDetection:
      config?.minTurnsBeforeDetection ??
      GUARDRAIL_DEFAULTS.minTurnsBeforeDetection,
  };
}

interface GuardrailState {
  turnCount: number;
  consecutiveStallTurns: number;
  consecutiveEmptyTurns: number;
  lastProgressTime: number;
  idleTimer: ReturnType<typeof setTimeout> | null;
  idleTimerPaused: boolean;
  idleRemainingMs: number;
}

export class SessionGuardrails {
  private state: GuardrailState;
  private config: ResolvedGuardrailConfig;
  private abortController: AbortController;

  constructor(
    config: ResolvedGuardrailConfig,
    abortController: AbortController,
  ) {
    this.config = config;
    this.abortController = abortController;
    this.state = {
      turnCount: 0,
      consecutiveStallTurns: 0,
      consecutiveEmptyTurns: 0,
      lastProgressTime: Date.now(),
      idleTimer: null,
      idleTimerPaused: false,
      idleRemainingMs: 0,
    };

    // Start the initial idle timer
    this.resetIdleTimer();
  }

  /**
   * Called on each SDKAssistantMessage to evaluate turn-level guardrails.
   * This is the turn boundary — a complete assistant response.
   */
  onAssistantMessage(message: SDKAssistantMessage): void {
    this.state.turnCount++;

    // Skip detection during warm-up period
    if (this.state.turnCount < this.config.minTurnsBeforeDetection) {
      this.resetIdleTimer();
      return;
    }

    const content = message.message.content as Array<{ type: string; text?: string }>;

    const hasToolUse = content.some(
      (block: { type: string }) => block.type === "tool_use",
    );

    if (hasToolUse) {
      // Tool call = meaningful progress — reset all counters
      this.state.consecutiveStallTurns = 0;
      this.state.consecutiveEmptyTurns = 0;
      this.resetIdleTimer();
      return;
    }

    // No tool use — check stall threshold
    this.state.consecutiveStallTurns++;
    if (
      this.config.stallThreshold > 0 &&
      this.state.consecutiveStallTurns >= this.config.stallThreshold
    ) {
      this.abort(
        "stall_detected",
        `${this.state.consecutiveStallTurns} consecutive turns with no tool calls`,
      );
      return;
    }

    // Check for empty/trivial text response
    const totalTextLength = content
      .filter((block: { type: string }) => block.type === "text")
      .reduce((sum: number, block: { type: string; text?: string }) => sum + (block.text?.trim().length ?? 0), 0);

    if (totalTextLength < 10) {
      this.state.consecutiveEmptyTurns++;
      if (
        this.config.emptyResponseThreshold > 0 &&
        this.state.consecutiveEmptyTurns >= this.config.emptyResponseThreshold
      ) {
        this.abort(
          "empty_response",
          `${this.state.consecutiveEmptyTurns} consecutive trivial responses (<10 chars)`,
        );
        return;
      }
    } else {
      this.state.consecutiveEmptyTurns = 0;
      this.resetIdleTimer();
    }
  }

  /**
   * Called on stream_event to track activity for idle timeout.
   * Does not reset counters — only resets the idle timer on substantial text.
   */
  onStreamActivity(): void {
    // Only reset idle timer — turn-level counters are managed by onAssistantMessage
    this.resetIdleTimer();
  }

  /** Pause idle timer during canUseTool execution (relay may take 60-120s). */
  pauseIdleTimer(): void {
    if (this.state.idleTimer && !this.state.idleTimerPaused) {
      const elapsed = Date.now() - this.state.lastProgressTime;
      this.state.idleRemainingMs = Math.max(
        0,
        this.config.idleTimeoutMs - elapsed,
      );
      clearTimeout(this.state.idleTimer);
      this.state.idleTimer = null;
      this.state.idleTimerPaused = true;
    }
  }

  /** Resume idle timer after canUseTool returns. */
  resumeIdleTimer(): void {
    if (this.state.idleTimerPaused) {
      this.state.idleTimerPaused = false;
      if (this.state.idleRemainingMs > 0 && this.config.idleTimeoutMs > 0) {
        this.state.lastProgressTime =
          Date.now() - (this.config.idleTimeoutMs - this.state.idleRemainingMs);
        this.state.idleTimer = setTimeout(() => {
          this.abort(
            "idle_timeout",
            `No meaningful progress for ${Math.round(this.config.idleTimeoutMs / 1000)}s`,
          );
        }, this.state.idleRemainingMs);
      }
    }
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
    if (this.config.idleTimeoutMs <= 0) return;
    if (this.state.idleTimerPaused) return;

    this.state.lastProgressTime = Date.now();
    this.state.idleTimer = setTimeout(() => {
      this.abort(
        "idle_timeout",
        `No meaningful progress for ${Math.round(this.config.idleTimeoutMs / 1000)}s`,
      );
    }, this.config.idleTimeoutMs);
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

    logGuardrail(guardrail, detail);
    this.dispose();
    this.abortController.abort(reason);
  }
}
