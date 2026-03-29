---
title: "feat: Add session termination guardrails (max-turns, stall detection)"
type: feat
status: completed
date: 2026-03-29
issue: 15
---

# feat: Add Session Termination Guardrails

## Overview

After a task completes, claude-pilot sessions can enter a degenerate loop where the LLM generates empty/idle responses indefinitely. Observed in task `7bf0c14b` — ~35 idle responses burning ~250K input tokens across 9 wasted LLM calls. The root cause is that `agent.ts` iterates `for await (const message of q)` with no defense against the SDK continuing to yield empty text responses after the task is logically complete.

This plan adds four guardrails to detect and terminate degenerate sessions:
1. **Max-turns limit** — SDK-native, configurable cap
2. **Stall detection** — no tool calls for N consecutive turns
3. **Empty-response detection** — N consecutive trivial text responses
4. **Idle timeout** — no meaningful progress for X seconds

## Problem Statement

- Wasted tokens (~250K in the observed case)
- Tasks stuck in `in_progress` (ResultJson never emitted, mika-dev callback never fires)
- Agent orchestrator keeps spinning sessions for a finished task
- No observability into why a session ran long

## Proposed Solution

### Architecture

A new `guardrails.ts` module encapsulates all detection state machines. The message loop in `agent.ts` calls into this module on each message. When a guardrail fires, it aborts the session via the existing `AbortController` and emits a structured `ResultJson` with the termination reason.

```
cli.ts (config + abort wiring)
  → agent.ts (message loop)
    → guardrails.ts (state machine, checks each message)
      → abortController.abort(reason) when threshold breached
    → ResultJson emitted with guardrail subtype
```

### SDK-Native vs Application-Level

| Guardrail | Layer | Mechanism |
|-----------|-------|-----------|
| Max-turns | SDK-native | Pass `maxTurns` to `query()` options; SDK emits `error_max_turns` result |
| Max-budget | SDK-native | Pass `maxBudgetUsd` to `query()` options; SDK emits `error_max_budget_usd` result |
| Stall detection | Application | Track `SDKAssistantMessage` turns; count consecutive turns with no `tool_use` content blocks |
| Empty-response | Application | Track `SDKAssistantMessage` turns; count consecutive turns where all text content is < 10 chars |
| Idle timeout | Application | Timer resets on meaningful progress; fires `abortController.abort()` on expiry |

## Technical Approach

### Phase 1: Foundation — Config, Types, Abort Path Enhancement

**`src/types.ts`** — Extend `PilotConfigSchema` with guardrail fields:

```typescript
// src/types.ts additions
export const GuardrailConfigSchema = z.object({
  maxTurns: z.number().int().min(1).optional(),
  maxBudgetUsd: z.number().min(0.01).optional(),
  stallThreshold: z.number().int().min(1).optional(),
  emptyResponseThreshold: z.number().int().min(1).optional(),
  idleTimeoutMs: z.number().int().min(1000).optional(),
  minTurnsBeforeDetection: z.number().int().min(0).optional(),
}).optional();

export const PilotConfigSchema = z.object({
  command: z.string().min(1),
  args: z.array(z.string()).optional(),
  timeout: z.number().int().min(1000).max(600_000).optional(),
  model: z.string().min(1).optional(),
  guardrails: GuardrailConfigSchema,
});
```

Default values (applied in code, not schema):

| Field | Default | Rationale |
|-------|---------|-----------|
| `maxTurns` | `200` | Generous for complex tasks; SDK handles natively |
| `maxBudgetUsd` | none (disabled) | Budget varies per task; let caller configure |
| `stallThreshold` | `5` | 5 consecutive turns with no tool calls = stalled |
| `emptyResponseThreshold` | `5` | 5 consecutive trivial responses = degenerate |
| `idleTimeoutMs` | `300000` (5 min) | No progress for 5 minutes = idle |
| `minTurnsBeforeDetection` | `10` | Skip stall/empty checks in early planning turns |

**`src/types.ts`** — Extend `ResultJson` to include termination reason:

```typescript
export interface ResultJson {
  status: "success" | "error" | "terminated";
  subtype: string;
  task_id?: string;
  session_id?: string;
  turns: number;
  cost_usd: number;
  duration_ms: number;
  errors?: string[];
  termination_reason?: string; // guardrail that fired
}
```

**`src/agent.ts`** — Enhance `AbortError` catch block:

Currently the `AbortError` handler (line 93-98) silently returns with no output. It must:
1. Check if the abort was guardrail-initiated (via abort reason)
2. Emit `ResultJson` with tracked turn count, duration, and termination reason
3. Track `sessionId`, turn count, and start time locally in `runAgent()`

The abort reason is passed via `abortController.abort(reason)` where `reason` is a `GuardrailAbortReason` object:

```typescript
// src/guardrails.ts
export interface GuardrailAbortReason {
  type: "guardrail";
  guardrail: "stall_detected" | "empty_response" | "idle_timeout";
  turns: number;
  detail: string;
}
```

In the `AbortError` catch block:

```typescript
catch (err) {
  if (err instanceof AbortError) {
    const reason = abortController.signal.reason;
    if (reason && typeof reason === "object" && reason.type === "guardrail") {
      const resultJson: ResultJson = {
        status: "terminated",
        subtype: reason.guardrail,
        turns: turnCount,
        cost_usd: 0, // not available on abort
        duration_ms: Date.now() - startTime,
        termination_reason: reason.detail,
        ...(opts.taskId && { task_id: opts.taskId }),
        ...(sessionId && { session_id: sessionId }),
      };
      process.stdout.write(JSON.stringify(resultJson) + "\n");
      logGuardrail(reason.guardrail, reason.detail);
    } else {
      process.stderr.write("\n");
    }
    return;
  }
  throw err;
}
```

### Phase 2: Guardrails Module

**New file: `src/guardrails.ts`**

A stateful class that processes SDK messages and decides when to terminate:

```typescript
// src/guardrails.ts
import type { GuardrailConfig } from "./types.js";

export interface GuardrailAbortReason {
  type: "guardrail";
  guardrail: "stall_detected" | "empty_response" | "idle_timeout";
  turns: number;
  detail: string;
}

interface GuardrailState {
  turnCount: number;
  consecutiveStallTurns: number;    // turns with no tool_use
  consecutiveEmptyTurns: number;    // turns with trivial text only
  lastProgressTime: number;         // Date.now() of last meaningful event
  idleTimer: ReturnType<typeof setTimeout> | null;
}

export class SessionGuardrails {
  private state: GuardrailState;
  private config: Required<GuardrailConfig>;
  private abortController: AbortController;

  constructor(config: GuardrailConfig, abortController: AbortController) { ... }

  /** Called on each SDKAssistantMessage to evaluate turn-level guardrails */
  onAssistantMessage(message: SDKAssistantMessage): void { ... }

  /** Called on stream_event text deltas to track idle timeout */
  onStreamEvent(): void { ... }

  /** Pause idle timer during canUseTool execution */
  pauseIdleTimer(): void { ... }

  /** Resume idle timer after canUseTool returns */
  resumeIdleTimer(): void { ... }

  /** Clean up timers */
  dispose(): void { ... }

  get turns(): number { return this.state.turnCount; }
}
```

**Turn boundary detection:** The SDK emits `SDKAssistantMessage` (`type: 'assistant'`) as a complete turn containing `message.content` blocks. This is the correct turn boundary — NOT individual `stream_event` deltas. The current `agent.ts` does not handle `type === 'assistant'` messages; a new handler branch is needed.

**Stall detection logic:**

```typescript
onAssistantMessage(msg: SDKAssistantMessage): void {
  this.state.turnCount++;

  if (this.state.turnCount < this.config.minTurnsBeforeDetection) return;

  const hasToolUse = msg.message.content.some(
    (block) => block.type === "tool_use"
  );

  if (hasToolUse) {
    this.state.consecutiveStallTurns = 0;
    this.state.consecutiveEmptyTurns = 0;
    this.resetIdleTimer();
    return;
  }

  // No tool use — check for stall
  this.state.consecutiveStallTurns++;
  if (this.state.consecutiveStallTurns >= this.config.stallThreshold) {
    this.abort("stall_detected",
      `${this.state.consecutiveStallTurns} consecutive turns with no tool calls`);
  }

  // Check for empty/trivial response
  const totalTextLength = msg.message.content
    .filter((b) => b.type === "text")
    .reduce((sum, b) => sum + b.text.trim().length, 0);

  if (totalTextLength < 10) {
    this.state.consecutiveEmptyTurns++;
    if (this.state.consecutiveEmptyTurns >= this.config.emptyResponseThreshold) {
      this.abort("empty_response",
        `${this.state.consecutiveEmptyTurns} consecutive trivial responses (<10 chars)`);
    }
  } else {
    this.state.consecutiveEmptyTurns = 0;
    this.resetIdleTimer();
  }
}
```

**Idle timeout logic:**

```typescript
private resetIdleTimer(): void {
  if (this.state.idleTimer) clearTimeout(this.state.idleTimer);
  if (!this.config.idleTimeoutMs) return;

  this.state.lastProgressTime = Date.now();
  this.state.idleTimer = setTimeout(() => {
    const elapsed = Date.now() - this.state.lastProgressTime;
    this.abort("idle_timeout",
      `No meaningful progress for ${Math.round(elapsed / 1000)}s`);
  }, this.config.idleTimeoutMs);
}
```

**Idle timer pause/resume during `canUseTool`:** The permission handler in `permissions.ts` can take 60-120s when the relay agent escalates to a human. The idle timer must pause during this period to avoid false positives. The `SessionGuardrails` instance is passed to the permission handler, which calls `pauseIdleTimer()` before invoking the relay and `resumeIdleTimer()` after.

### Phase 3: Integration into Message Loop

**`src/agent.ts`** — Wire guardrails into the message loop:

```typescript
export async function runAgent(opts: AgentOptions): Promise<void> {
  const startTime = Date.now();
  let sessionId: string | undefined;

  // Resolve guardrail config with defaults
  const guardrailConfig = resolveGuardrailDefaults(opts.guardrailConfig);

  const guardrails = new SessionGuardrails(guardrailConfig, opts.abortController);

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
      ...(guardrailConfig.maxTurns && { maxTurns: guardrailConfig.maxTurns }),
      ...(guardrailConfig.maxBudgetUsd && { maxBudgetUsd: guardrailConfig.maxBudgetUsd }),
    },
  });

  try {
    for await (const message of q) {
      // ... existing system/init handler ...

      // NEW: Handle complete assistant messages (turn boundaries)
      if (message.type === "assistant") {
        guardrails.onAssistantMessage(message);
        continue;
      }

      // ... existing stream_event handler ...
      if (message.type === "stream_event") {
        guardrails.onStreamEvent(); // track activity for idle timer
        // ... existing text_delta handling ...
        continue;
      }

      // ... existing result handler ...
    }
  } catch (err) {
    if (err instanceof AbortError) {
      // Enhanced: check for guardrail reason
      const reason = opts.abortController.signal.reason;
      if (isGuardrailAbortReason(reason)) {
        const resultJson: ResultJson = {
          status: "terminated",
          subtype: reason.guardrail,
          turns: guardrails.turns,
          cost_usd: 0,
          duration_ms: Date.now() - startTime,
          termination_reason: reason.detail,
          ...(opts.taskId && { task_id: opts.taskId }),
          ...(sessionId && { session_id: sessionId }),
        };
        process.stdout.write(JSON.stringify(resultJson) + "\n");
        logGuardrail(reason.guardrail, reason.detail);
      } else {
        process.stderr.write("\n");
      }
      return;
    }
    throw err;
  } finally {
    guardrails.dispose(); // clean up idle timer
  }
}
```

### Phase 4: UI and Logging

**`src/ui.ts`** — Add guardrail log function:

```typescript
const ORANGE = "\x1b[38;5;208m"; // for guardrail events

export function logGuardrail(type: string, detail: string): void {
  log(`\n${ORANGE}[guardrail]${RESET} ${BOLD}${type}${RESET}: ${detail}`);
}

export function logGuardrailConfig(config: ResolvedGuardrailConfig): void {
  const parts = [
    config.maxTurns ? `maxTurns=${config.maxTurns}` : null,
    config.stallThreshold ? `stallThreshold=${config.stallThreshold}` : null,
    config.emptyResponseThreshold ? `emptyResponseThreshold=${config.emptyResponseThreshold}` : null,
    config.idleTimeoutMs ? `idleTimeout=${config.idleTimeoutMs / 1000}s` : null,
  ].filter(Boolean);
  log(`${DIM}[guardrails]${RESET} ${parts.join(" ")}`);
}
```

**Startup logging** (following the `[config]` pattern from the silent-relay fix):

```
[config] cwd=/path config=.claude/claude-pilot.json [found] relay=enabled
[guardrails] maxTurns=200 stallThreshold=5 emptyResponseThreshold=5 idleTimeout=300s
[init] Session abc12345, model claude-sonnet-4-20250514, task 7bf0c14b
```

### Phase 5: CLI Flag Overrides

**`src/cli.ts`** — Add CLI flags that override config values:

```
--max-turns <n>           Maximum agentic turns (default: 200)
--max-budget <usd>        Maximum cost in USD
--stall-threshold <n>     Consecutive no-tool turns before termination (default: 5)
--empty-threshold <n>     Consecutive trivial responses before termination (default: 5)
--idle-timeout <ms>       Idle timeout in milliseconds (default: 300000)
--no-guardrails           Disable all application-level guardrails
```

CLI values merge over config file values. `--no-guardrails` disables stall, empty-response, and idle detection but preserves SDK-native maxTurns.

## System-Wide Impact

- **Interaction graph**: `cli.ts` loads config → creates `SessionGuardrails` → passes to `runAgent()` → message loop calls `guardrails.onAssistantMessage()` / `guardrails.onStreamEvent()` on each message → guardrail fires `abortController.abort(reason)` → SDK throws `AbortError` → catch block emits `ResultJson`
- **Error propagation**: Guardrail abort → `AbortError` caught in `agent.ts` → structured `ResultJson` on stdout → `closeFileLog()` in `finally`/`cli.ts`. No unhandled paths.
- **State lifecycle risks**: The idle timer (`setTimeout`) must be disposed in a `finally` block to prevent Node.js process hang. If abort fires during `canUseTool`, the `execFile` child process is killed via `AbortSignal` (already supported in `transport.ts`).
- **API surface parity**: `ResultJson` gains a new `status: "terminated"` value and `termination_reason` field. Consumers (mika-dev) must handle this new status.
- **Integration test scenarios**: (1) Session with stall pattern → verify `terminated` ResultJson with correct subtype. (2) Idle timeout during relay wait → verify timer pauses. (3) maxTurns reached → verify SDK-native `error_max_turns` passes through correctly.

## Acceptance Criteria

- [x] `PilotConfigSchema` extended with optional `guardrails` object (`src/types.ts`)
- [x] `ResultJson` extended with `"terminated"` status and `termination_reason` field (`src/types.ts`)
- [x] New `src/guardrails.ts` module with `SessionGuardrails` class
- [x] SDK-native `maxTurns` and `maxBudgetUsd` wired from config to `query()` options (`src/agent.ts`)
- [x] `SDKAssistantMessage` handler added to message loop for turn-level guardrails (`src/agent.ts`)
- [x] Stall detection: terminates after N consecutive turns with no tool calls
- [x] Empty-response detection: terminates after N consecutive trivial text responses
- [x] Idle timeout: terminates after no meaningful progress for X seconds
- [x] Idle timer pauses during `canUseTool` execution (`src/permissions.ts`)
- [x] `AbortError` catch block emits `ResultJson` with guardrail subtype (`src/agent.ts`)
- [x] `logGuardrail()` and `logGuardrailConfig()` functions in `src/ui.ts`
- [x] Guardrail config logged at startup following `[config]` pattern
- [x] CLI flags for `--max-turns`, `--stall-threshold`, `--empty-threshold`, `--idle-timeout`, `--no-guardrails` (`src/cli.ts`)
- [x] `minTurnsBeforeDetection` prevents false positives in early planning turns
- [x] Timer cleanup in `finally` block prevents Node.js process hang
- [x] `npx tsc --noEmit` passes
- [x] `npm run build` succeeds
- [x] CLAUDE.md updated with guardrails module in architecture section

## Success Metrics

- Zero degenerate loop incidents (sessions terminate within guardrail bounds)
- Guardrail-terminated sessions produce structured `ResultJson` with clear reason
- No false positives in normal operation (verified by minTurnsBeforeDetection warm-up)
- Token waste reduced from ~250K to bounded by maxTurns * avg_turn_cost

## Dependencies & Risks

- **SDK `SDKAssistantMessage` availability**: The SDK must emit `type: 'assistant'` messages for turn-level tracking. Verified: the SDK type exists at `sdk.d.ts:1644`. Current code does not handle this type, but the `for await` loop will yield it.
- **`abortController.signal.reason` support**: Requires Node.js 17.2+. The worktree uses Node 20+ (verified by TypeScript target).
- **False positive risk**: Mitigated by `minTurnsBeforeDetection` (default 10) and idle timer pause during `canUseTool`. Conservative defaults chosen.
- **Breaking change**: `ResultJson.status` gains `"terminated"` value. mika-dev consumers must handle this. Low risk — mika-dev already handles unknown subtypes gracefully.

## Sources & References

- GitHub issue: senara-solutions/claude-pilot#15
- SDK types: `node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts` (lines 67, 978, 1644, 2065-2067)
- Existing abort mechanism: `src/cli.ts:200-207`, `src/agent.ts:93-98`
- Config pattern: `src/types.ts:5-12`
- Logging pattern: `src/ui.ts` (all `log*` functions)
- Institutional learning: `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` — AbortSignal propagation pattern
- Institutional learning: `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — diagnostic logging pattern
- Institutional learning: `docs/solutions/code-quality/code-review-fixes-type-safety-and-security-hardening.md` — type safety patterns
