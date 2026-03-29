import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { SessionGuardrails, resolveGuardrailDefaults } from "../src/guardrails.js";
import { GUARDRAIL_DEFAULTS, isGuardrailAbortReason } from "../src/types.js";
import type { GuardrailConfig, GuardrailAbortReason } from "../src/types.js";
import type { SDKAssistantMessage } from "@anthropic-ai/claude-agent-sdk";

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeConfig(overrides?: Partial<GuardrailConfig>): Required<GuardrailConfig> {
  return resolveGuardrailDefaults({
    ...overrides,
    // Disable idle timer by default in tests to avoid timer leaks
    idleTimeoutMs: overrides?.idleTimeoutMs ?? 0,
    // Disable warm-up by default so tests don't need to pump extra turns
    minTurnsBeforeDetection: overrides?.minTurnsBeforeDetection ?? 0,
  });
}

/** Build a minimal SDKAssistantMessage with tool_use content blocks. */
function toolUseMessage(): SDKAssistantMessage {
  return {
    type: "assistant",
    message: {
      content: [
        { type: "tool_use", id: "tu_1", name: "Bash", input: { command: "ls" } },
      ],
    },
  } as unknown as SDKAssistantMessage;
}

/** Build a minimal SDKAssistantMessage with only text content. */
function textMessage(text: string): SDKAssistantMessage {
  return {
    type: "assistant",
    message: {
      content: [{ type: "text", text }],
    },
  } as unknown as SDKAssistantMessage;
}

/** Build a minimal SDKAssistantMessage with empty content. */
function emptyMessage(): SDKAssistantMessage {
  return {
    type: "assistant",
    message: { content: [] },
  } as unknown as SDKAssistantMessage;
}

function getAbortReason(controller: AbortController): GuardrailAbortReason | undefined {
  if (!controller.signal.aborted) return undefined;
  const reason = controller.signal.reason;
  return isGuardrailAbortReason(reason) ? reason : undefined;
}

// ── resolveGuardrailDefaults ─────────────────────────────────────────────────

describe("resolveGuardrailDefaults", () => {
  it("returns all defaults when no config provided", () => {
    const result = resolveGuardrailDefaults();
    expect(result).toEqual(GUARDRAIL_DEFAULTS);
  });

  it("returns all defaults when empty config provided", () => {
    const result = resolveGuardrailDefaults({});
    expect(result).toEqual(GUARDRAIL_DEFAULTS);
  });

  it("overrides individual fields while keeping other defaults", () => {
    const result = resolveGuardrailDefaults({ maxTurns: 50, stallThreshold: 3 });
    expect(result.maxTurns).toBe(50);
    expect(result.stallThreshold).toBe(3);
    expect(result.emptyResponseThreshold).toBe(GUARDRAIL_DEFAULTS.emptyResponseThreshold);
    expect(result.idleTimeoutMs).toBe(GUARDRAIL_DEFAULTS.idleTimeoutMs);
    expect(result.minTurnsBeforeDetection).toBe(GUARDRAIL_DEFAULTS.minTurnsBeforeDetection);
  });

  it("does not use undefined overrides", () => {
    const result = resolveGuardrailDefaults({ maxTurns: undefined });
    expect(result.maxTurns).toBe(GUARDRAIL_DEFAULTS.maxTurns);
  });

  it("allows 0 values for disabling guardrails", () => {
    const result = resolveGuardrailDefaults({
      stallThreshold: 0,
      emptyResponseThreshold: 0,
      idleTimeoutMs: 0,
    });
    expect(result.stallThreshold).toBe(0);
    expect(result.emptyResponseThreshold).toBe(0);
    expect(result.idleTimeoutMs).toBe(0);
  });
});

// ── isGuardrailAbortReason ───────────────────────────────────────────────────

describe("isGuardrailAbortReason", () => {
  it("returns true for valid GuardrailAbortReason", () => {
    const reason: GuardrailAbortReason = {
      type: "guardrail",
      guardrail: "stall_detected",
      turns: 15,
      detail: "5 consecutive turns with no tool calls",
    };
    expect(isGuardrailAbortReason(reason)).toBe(true);
  });

  it("returns false for null", () => {
    expect(isGuardrailAbortReason(null)).toBe(false);
  });

  it("returns false for non-object", () => {
    expect(isGuardrailAbortReason("guardrail")).toBe(false);
    expect(isGuardrailAbortReason(42)).toBe(false);
  });

  it("returns false when type is not 'guardrail'", () => {
    expect(isGuardrailAbortReason({ type: "error", guardrail: "stall", turns: 1, detail: "x" })).toBe(false);
  });

  it("returns false when turns is missing", () => {
    expect(isGuardrailAbortReason({ type: "guardrail", guardrail: "stall", detail: "x" })).toBe(false);
  });

  it("returns false when turns is not a number", () => {
    expect(isGuardrailAbortReason({ type: "guardrail", guardrail: "stall", turns: "5", detail: "x" })).toBe(false);
  });

  it("returns false when detail is missing", () => {
    expect(isGuardrailAbortReason({ type: "guardrail", guardrail: "stall", turns: 1 })).toBe(false);
  });
});

// ── SessionGuardrails: stall detection ───────────────────────────────────────

describe("SessionGuardrails — stall detection", () => {
  it("fires after consecutive turns with no tool use", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(makeConfig({ stallThreshold: 3 }), controller);

    guardrails.onAssistantMessage(textMessage("thinking about it..."));
    expect(controller.signal.aborted).toBe(false);
    guardrails.onAssistantMessage(textMessage("still thinking..."));
    expect(controller.signal.aborted).toBe(false);
    guardrails.onAssistantMessage(textMessage("almost there..."));

    expect(controller.signal.aborted).toBe(true);
    const reason = getAbortReason(controller);
    expect(reason?.guardrail).toBe("stall_detected");
    expect(reason?.turns).toBe(3);

    guardrails.dispose();
  });

  it("resets stall counter on tool use", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(makeConfig({ stallThreshold: 3 }), controller);

    guardrails.onAssistantMessage(textMessage("thinking..."));
    guardrails.onAssistantMessage(textMessage("hmm..."));
    // Tool use resets counter
    guardrails.onAssistantMessage(toolUseMessage());
    guardrails.onAssistantMessage(textMessage("thinking again..."));
    guardrails.onAssistantMessage(textMessage("still at it..."));

    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });

  it("does not fire when stallThreshold is 0 (disabled)", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(makeConfig({ stallThreshold: 0 }), controller);

    for (let i = 0; i < 20; i++) {
      guardrails.onAssistantMessage(textMessage("looping forever..."));
    }

    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });
});

// ── SessionGuardrails: empty response detection ──────────────────────────────

describe("SessionGuardrails — empty response detection", () => {
  it("fires after consecutive trivial responses", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ emptyResponseThreshold: 3, stallThreshold: 0 }),
      controller,
    );

    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(textMessage("ok")); // < 10 chars
    guardrails.onAssistantMessage(textMessage("")); // trivial

    expect(controller.signal.aborted).toBe(true);
    const reason = getAbortReason(controller);
    expect(reason?.guardrail).toBe("empty_response");

    guardrails.dispose();
  });

  it("resets empty counter on non-trivial text", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ emptyResponseThreshold: 3, stallThreshold: 0 }),
      controller,
    );

    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(emptyMessage());
    // Non-trivial text (>= 10 chars) resets the empty counter
    guardrails.onAssistantMessage(textMessage("This is a meaningful response with good content."));
    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(emptyMessage());

    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });

  it("resets empty counter on tool use", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ emptyResponseThreshold: 3, stallThreshold: 0 }),
      controller,
    );

    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(toolUseMessage());
    guardrails.onAssistantMessage(emptyMessage());
    guardrails.onAssistantMessage(emptyMessage());

    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });

  it("does not fire when emptyResponseThreshold is 0 (disabled)", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ emptyResponseThreshold: 0, stallThreshold: 0 }),
      controller,
    );

    for (let i = 0; i < 20; i++) {
      guardrails.onAssistantMessage(emptyMessage());
    }

    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });
});

// ── SessionGuardrails: warm-up period ────────────────────────────────────────

describe("SessionGuardrails — warm-up period", () => {
  it("skips detection during minTurnsBeforeDetection", () => {
    const controller = new AbortController();
    // minTurnsBeforeDetection=5 means turns with turnCount < 5 skip detection
    // (i.e., turns 1–4 are skipped). Turn 5 runs detection.
    const guardrails = new SessionGuardrails(
      makeConfig({ stallThreshold: 3, minTurnsBeforeDetection: 5 }),
      controller,
    );

    // First 4 turns are in warm-up (turnCount 1–4, all < 5) — detection skipped
    for (let i = 0; i < 4; i++) {
      guardrails.onAssistantMessage(textMessage("warm-up turn"));
    }
    expect(controller.signal.aborted).toBe(false);

    // Turn 5: first detection turn — stall counter starts at 1
    guardrails.onAssistantMessage(textMessage("post-warmup 1"));
    expect(controller.signal.aborted).toBe(false);

    // Turn 6: stall counter = 2
    guardrails.onAssistantMessage(textMessage("post-warmup 2"));
    expect(controller.signal.aborted).toBe(false);

    // Turn 7: stall counter = 3 >= stallThreshold(3) — fires
    guardrails.onAssistantMessage(textMessage("post-warmup 3"));
    expect(controller.signal.aborted).toBe(true);
    expect(getAbortReason(controller)?.guardrail).toBe("stall_detected");

    guardrails.dispose();
  });
});

// ── SessionGuardrails: idle timeout ──────────────────────────────────────────

describe("SessionGuardrails — idle timeout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires after idle timeout expires", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ idleTimeoutMs: 5000 }),
      controller,
    );

    expect(controller.signal.aborted).toBe(false);

    vi.advanceTimersByTime(4999);
    expect(controller.signal.aborted).toBe(false);

    vi.advanceTimersByTime(1);
    expect(controller.signal.aborted).toBe(true);
    expect(getAbortReason(controller)?.guardrail).toBe("idle_timeout");

    guardrails.dispose();
  });

  it("resets idle timer on assistant message", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ idleTimeoutMs: 5000, stallThreshold: 0 }),
      controller,
    );

    vi.advanceTimersByTime(4000);
    guardrails.onAssistantMessage(textMessage("still working"));

    vi.advanceTimersByTime(4000);
    expect(controller.signal.aborted).toBe(false);

    vi.advanceTimersByTime(1000);
    expect(controller.signal.aborted).toBe(true);

    guardrails.dispose();
  });

  it("pauses and resumes correctly", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ idleTimeoutMs: 5000 }),
      controller,
    );

    vi.advanceTimersByTime(3000);
    guardrails.pauseIdleTimer();

    // Time passes while paused — should not fire
    vi.advanceTimersByTime(10000);
    expect(controller.signal.aborted).toBe(false);

    // Resume starts a fresh full-duration timer
    guardrails.resumeIdleTimer();
    vi.advanceTimersByTime(4999);
    expect(controller.signal.aborted).toBe(false);

    vi.advanceTimersByTime(1);
    expect(controller.signal.aborted).toBe(true);

    guardrails.dispose();
  });

  it("does not fire when idleTimeoutMs is 0 (disabled)", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ idleTimeoutMs: 0 }),
      controller,
    );

    vi.advanceTimersByTime(999_999);
    expect(controller.signal.aborted).toBe(false);

    guardrails.dispose();
  });
});

// ── SessionGuardrails: double-abort safety ───────────────────────────────────

describe("SessionGuardrails — double-abort safety", () => {
  it("does not throw on second abort attempt", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ stallThreshold: 1 }),
      controller,
    );

    // First abort
    guardrails.onAssistantMessage(textMessage("stall"));
    expect(controller.signal.aborted).toBe(true);

    // Second abort attempt — should not throw
    expect(() => {
      guardrails.onAssistantMessage(textMessage("stall again"));
    }).not.toThrow();

    guardrails.dispose();
  });
});

// ── SessionGuardrails: turn counter ──────────────────────────────────────────

describe("SessionGuardrails — turn counter", () => {
  it("tracks turn count correctly", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ stallThreshold: 0 }),
      controller,
    );

    expect(guardrails.turns).toBe(0);
    guardrails.onAssistantMessage(toolUseMessage());
    expect(guardrails.turns).toBe(1);
    guardrails.onAssistantMessage(textMessage("hello"));
    expect(guardrails.turns).toBe(2);
    guardrails.onAssistantMessage(emptyMessage());
    expect(guardrails.turns).toBe(3);

    guardrails.dispose();
  });
});

// ── SessionGuardrails: dispose ───────────────────────────────────────────────

describe("SessionGuardrails — dispose", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("clears idle timer on dispose", () => {
    const controller = new AbortController();
    const guardrails = new SessionGuardrails(
      makeConfig({ idleTimeoutMs: 5000 }),
      controller,
    );

    guardrails.dispose();

    // Timer should be cleared — advancing time should not abort
    vi.advanceTimersByTime(10000);
    expect(controller.signal.aborted).toBe(false);
  });
});
