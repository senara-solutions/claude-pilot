---
title: "fix: V8 fatal error (exit code 5) on process teardown"
type: fix
status: active
date: 2026-04-02
issue: 29
---

# fix: V8 fatal error (exit code 5) on process teardown

## Overview

Since Apr 1, every claude-pilot run exits with code 5 (V8 Fatal Error) during Node.js process teardown, despite the session completing successfully. The crash happens AFTER `runAgent()` returns, AFTER `closeFileLog()`, during event loop drain when the Claude Agent SDK's transport cleanup runs. 100% repro rate (4/4 runs since Apr 1 20:49).

A prior fix (commit `ebf92b6`) added global `uncaughtException`/`unhandledRejection` handlers that catch the error and set exit code 1, but this is insufficient — callers still see non-zero exit and treat the run as failed. The root fix is to call `process.exit()` before SDK teardown can crash.

## Problem Statement

The `@anthropic-ai/claude-agent-sdk` spawns Claude Code as a child process and registers cleanup handlers on `process.on("exit")`. After `runAgent()` returns, Node.js begins event loop drain, and the SDK's transport cleanup (readline interface on child stdout, SIGTERM of child process) triggers a V8 fatal error — a native engine crash that bypasses all JavaScript error handlers.

**Impact:**
- Every self-dev callback reports "HANDLER CRASH" to mika-dev
- mika-dev wastes 8-10 tool calls per callback handling a false failure
- Work items get incorrectly marked as `blocked`
- User gets false "Handler crash during PR creation" notifications

## Proposed Solution

Force a clean `process.exit()` after the session completes and output is written, before Node.js drains the event loop and SDK teardown can crash.

### Changes

#### 1. `src/cli.ts` — Force clean exit after session completes

After `closeFileLog()` at line 383, add:

```typescript
closeFileLog();

// Force exit with the intended code. After runAgent() returns and ResultJson
// is on stdout, there is nothing left to do. Letting Node.js drain the event
// loop risks a V8 fatal error (exit code 5) from SDK transport cleanup.
// See: https://github.com/senara-solutions/claude-pilot/issues/29
process.exit(process.exitCode ?? 0);
```

This covers all post-`runAgent()` flows:
| Flow | exitCode before exit | Result |
|------|---------------------|--------|
| Success | `undefined` → 0 | Exits 0 |
| SDK error result | `1` (set in agent.ts) | Exits 1 |
| Guardrail abort | `1` (set in agent.ts) | Exits 1 |
| User abort (SIGINT) | `undefined` → 0 | Exits 0 |

#### 2. `src/logger.ts` — Add error handler to prevent stream errors escalating

Add a no-op `'error'` handler to the write stream in `initFileLog()` so that log file I/O errors (disk full, permissions) don't trigger `uncaughtException` and change the exit code:

```typescript
export function initFileLog(filePath: string): void {
  mkdirSync(dirname(filePath), { recursive: true, mode: 0o700 });
  fileStream = createWriteStream(filePath, { flags: "a", mode: 0o600 });
  fileStream.on("error", () => {}); // Swallow — log file is best-effort
}
```

#### 3. `src/guardrails.ts` — Unref idle timer (defense-in-depth)

Add `.unref()` to the idle timer so it cannot keep the event loop alive if `dispose()` is somehow skipped:

```typescript
this.idleTimer = setTimeout(() => { this.abort(); }, this.config.idleTimeoutMs);
// Change to:
this.idleTimer = setTimeout(() => { this.abort(); }, this.config.idleTimeoutMs).unref();
```

With the `process.exit()` fix, this is belt-and-suspenders — but it prevents the timer from blocking shutdown if the `process.exit()` line is ever removed.

#### 4. Update solution doc

Update `docs/solutions/runtime-errors/silent-exit-code-5-from-sdk-teardown.md` to document the `process.exit()` fix as the primary defense, with the global handlers as fallback.

## Technical Considerations

### Log file truncation
`closeFileLog()` calls `fileStream.end()` which is async — the stream may not fully flush before `process.exit()`. This is acceptable: the log file is diagnostic (best-effort), not the primary output contract. ResultJson on stdout (the critical output) is written synchronously within `runAgent()` before returning to `cli.ts`.

### ResultJson stdout flush
`process.stdout.write(JSON.stringify(resultJson) + "\n")` for a ~200 byte payload is effectively synchronous in practice (well under pipe buffer limits). `process.exit()` also attempts to flush stdio before terminating.

### Global handlers remain as fallback
The `uncaughtException` and `unhandledRejection` handlers stay. They are no longer the primary defense against exit code 5 but still serve as a safety net for unexpected errors in any lifecycle phase.

### SIGINT flow: double closeFileLog()
The SIGINT shutdown handler calls `closeFileLog()`, then `runAgent()` returns to `main()` which calls it again. The second call is a no-op because `closeFileLog()` nulls `fileStream` after `.end()`. Safe by design.

## Acceptance Criteria

- [x] `process.exit(process.exitCode ?? 0)` added after `closeFileLog()` in `cli.ts` with explanatory comment referencing #29
- [x] Write stream error handler added in `initFileLog()` in `logger.ts`
- [x] Idle timer `.unref()`'d in `guardrails.ts`
- [x] Solution doc updated with the `process.exit()` fix
- [x] `npx tsc --noEmit` passes (type-check)
- [x] `npm run build` succeeds
- [x] Existing tests pass (if any)

## Sources

- GitHub issue: [#29](https://github.com/senara-solutions/claude-pilot/issues/29)
- Existing solution doc: `docs/solutions/runtime-errors/silent-exit-code-5-from-sdk-teardown.md`
- Prior fix commit: `ebf92b6` (global error handlers — partial fix)
- SDK source: `@anthropic-ai/claude-agent-sdk@0.2.76` transport cleanup
