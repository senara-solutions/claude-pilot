---
title: Session termination guardrails prevent degenerate LLM loops
category: architecture
date: 2026-03-29
severity: high
module: agent, guardrails, permissions
tags: [session-management, abort-signal, idle-detection, stall-detection, token-waste]
---

# Session Termination Guardrails

## Problem

After a task completes, claude-pilot sessions can enter a degenerate loop where the LLM generates empty/idle responses indefinitely. Observed in task `7bf0c14b` — ~35 idle responses burning ~250K input tokens across 9 wasted LLM calls. The root cause: `agent.ts` iterates `for await (const message of q)` until the SDK sends a `result` message, with no defense against the SDK continuing to yield empty text responses after the task is logically complete.

## Root Cause

The SDK `query()` async generator yields messages indefinitely until a `result` message arrives or the `AbortController` fires. When the LLM enters an idle loop (producing text-only "Nothing to do" responses with no tool calls), the SDK does not detect this as a termination condition — it faithfully streams the responses. Without application-level detection, the session runs until the SDK's own internal limits kick in (or never).

## Solution

Added a `SessionGuardrails` class (`src/guardrails.ts`) that monitors SDK messages at turn boundaries (`SDKAssistantMessage` events) and terminates via the existing `AbortController` mechanism when degenerate patterns are detected.

**Four guardrails implemented:**

| Guardrail | Layer | Default | Detection |
|-----------|-------|---------|-----------|
| Max turns | SDK-native | 200 | SDK emits `error_max_turns` result |
| Max budget | SDK-native | disabled | SDK emits `error_max_budget_usd` result |
| Stall detection | Application | 5 turns | N consecutive turns with no `tool_use` content blocks |
| Empty response | Application | 5 turns | N consecutive turns where all text < 10 chars |
| Idle timeout | Application | 300s | No turn boundary for X seconds |

**Key design decisions:**

1. **Turn boundary = `SDKAssistantMessage`**, not individual `stream_event` deltas. A turn is a complete assistant response with content blocks. Counting stream deltas would cause false positives constantly.

2. **Abort via `AbortController.abort(reason)`** where reason is a typed `GuardrailAbortReason` object. The existing `AbortError` catch block in `agent.ts` discriminates between guardrail aborts (emit `ResultJson` with `status: "terminated"`) and user-initiated aborts (SIGINT/SIGTERM, silent exit).

3. **Idle timer pauses during `canUseTool`** — relay agents can take 60-120s when escalating to a human. Without pausing, the idle timer would fire during legitimate permission waits.

4. **`minTurnsBeforeDetection` warm-up** (default 10) — early turns are often text-only planning. Stall/empty detection skips the warm-up period to avoid false positives.

5. **SDK-native results normalized** — `error_max_turns` and `error_max_budget_usd` results are mapped to `status: "terminated"` in `ResultJson`, giving consumers (mika-dev) a uniform termination discriminator.

6. **Config cascade: file < CLI** — `claude-pilot.json` provides defaults, CLI flags override. `--no-guardrails` disables stall/empty/idle but preserves SDK-native `maxTurns`.

## Prevention

- Always wire SDK `maxTurns` when calling `query()` — it's a free safety net with zero false-positive risk.
- For any async iterator loop that processes external messages, add a termination condition beyond "stream ends" — external systems may not end cleanly.
- When adding timers that interact with async callbacks (like `canUseTool`), always pause the timer during the callback to avoid timing-based false positives.
- Expose guardrail config at startup via structured logging (`[guardrails]` log line) so operators can verify what's active.

## Files Changed

- `src/guardrails.ts` — New module: `SessionGuardrails` class, `resolveGuardrailDefaults()`
- `src/types.ts` — `GuardrailConfigSchema`, extended `ResultJson`, `GuardrailAbortReason` type guard
- `src/agent.ts` — `SDKAssistantMessage` handler, enhanced `AbortError` catch, SDK result normalization
- `src/permissions.ts` — Idle timer pause/resume during relay calls
- `src/ui.ts` — `logGuardrail()`, `logGuardrailConfig()`
- `src/cli.ts` — CLI flags for all guardrail parameters, config merging

## Related

- Issue: senara-solutions/claude-pilot#15
- `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` — AbortSignal propagation pattern
- `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — Diagnostic logging pattern
