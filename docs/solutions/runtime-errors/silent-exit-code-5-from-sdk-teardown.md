---
title: "Silent exit code 5 from SDK teardown after successful session"
category: runtime-errors
date: 2026-04-02
severity: high
module: cli, agent, logger, guardrails
tags: [process-exit, query-close, uncaught-exception, unhandled-rejection, sdk-teardown, exit-code-5]
---

# Silent exit code 5 from SDK teardown after successful session

## Problem

claude-pilot sessions complete successfully (`[done] Success`) but the Node.js process exits with code 5 (V8 fatal error) during cleanup/teardown. Callers (resolve-pr-conflicts, address-pr-comments) see exit code 5 and treat the run as a failure, even though the agent session succeeded. The structured `ResultJson` on stdout is lost, and mika-dev receives: `"Process Exit code: 5: "` with no context.

## Root Cause

After `runAgent()` returns and the SDK session completes, the `@anthropic-ai/claude-agent-sdk` transport cleanup (readline interface on child stdout, SIGTERM of child process) triggers a V8 fatal error during Node.js event loop drain. This is a native engine crash that bypasses all JavaScript error handlers — `process.on('uncaughtException')` never fires for exit code 5.

The SDK's `ProcessTransport` registers `process.on("exit", () => child.kill("SIGTERM"))` during initialization. If `Query.close()` is never called, this handler remains registered. When `process.exit()` fires, the handler runs synchronously and attempts to kill the child process during V8 disposal, causing the fatal error. Even with `Query.close()`, V8 heap corruption from pipe/readline ops during the session can make `process.exit()` itself crash during isolate disposal.

## Solution

### Layer 1: Close the SDK Query (primary defense)

Call `q.close()` in the `finally` block of `runAgent()` to deregister the SDK's `process.on('exit')` handler and gracefully terminate the child process before control returns to `cli.ts`:

```typescript
// In agent.ts runAgent(), finally block:
} finally {
  // Deregister the SDK's process.on('exit') handler that kills the child
  // process during teardown. Without this, process.exit() in cli.ts triggers
  // the handler, causing a V8 fatal error (exit code 5).
  try { q.close(); } catch { /* already cleaned up */ }
  guardrails.dispose();
}
```

`Query.close()` calls `cleanup()` → `transport.close()` which removes the exit handler, ends stdin to the child, and sends SIGTERM with a 5-second SIGKILL fallback. `cleanup()` is idempotent (guarded by `cleanupPerformed` flag).

### Layer 2: Force clean exit (defense-in-depth)

After `q.close()` deregisters the SDK's exit handler, force `process.exit()` to prevent Node.js from draining the event loop and triggering any remaining SDK teardown:

```typescript
// In cli.ts main(), after runAgent() returns:
closeFileLog();

// Force exit with the intended code. After runAgent() returns and ResultJson
// is on stdout, there is nothing left to do. Letting Node.js drain the event
// loop risks a V8 fatal error (exit code 5) from SDK transport cleanup.
process.exit(process.exitCode ?? 0);
```

This covers all post-`runAgent()` flows:
| Flow | exitCode | Result |
|------|----------|--------|
| Success | `undefined` → 0 | Exits 0 |
| SDK error result | `1` (set in agent.ts) | Exits 1 |
| Guardrail abort | `1` (set in agent.ts) | Exits 1 |
| User abort (SIGINT) | `undefined` → 0 | Exits 0 |

### Layer 3: Global error handlers (fallback)

Registered before any async work so they cover every phase. These are now a fallback — they catch errors that occur before `runAgent()` returns or in edge cases where `process.exit()` doesn't fire.

```typescript
process.on("uncaughtException", (err) => {
  process.stderr.write(
    JSON.stringify({
      error: "uncaughtException",
      message: err.message,
      stack: err.stack,
    }) + "\n",
  );
  process.exitCode = 1;
  setTimeout(() => process.exit(1), 500).unref();
});

process.on("unhandledRejection", (reason) => {
  process.stderr.write(
    JSON.stringify({
      error: "unhandledRejection",
      message: reason instanceof Error ? reason.message : String(reason),
      stack: reason instanceof Error ? reason.stack : undefined,
    }) + "\n",
  );
  process.exitCode = 1;
});
```

### Layer 4: Hardened `main().catch()` with ResultJson

```typescript
main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  const resultJson: ResultJson = {
    status: "error",
    subtype: "fatal",
    turns: 0,
    cost_usd: 0,
    duration_ms: 0,
    errors: [message],
  };
  process.stdout.write(JSON.stringify(resultJson) + "\n");
  process.stderr.write(`Fatal: ${message}\n`);
  process.exit(1);
});
```

### Defense-in-depth: Logger and guardrail hardening

**Logger:** Error handler on the file write stream prevents log I/O errors from escalating to `uncaughtException` and changing the exit code. Emits a one-time diagnostic to stderr and disables further log writes:
```typescript
let logErrorReported = false;
fileStream.on("error", (err) => {
  if (!logErrorReported) {
    logErrorReported = true;
    process.stderr.write(`Warning: log file write error: ${err.message}\n`);
  }
  fileStream = undefined;
});
```

**Guardrails:** Idle timer uses `.unref()` so it cannot keep the event loop alive if `dispose()` is somehow skipped:
```typescript
this.state.idleTimer = setTimeout(() => { ... }, ms).unref();
```

## Key Design Decisions

- **`q.close()` is the primary defense** — it deregisters the SDK's exit handler that triggers the V8 crash. `process.exit()` is defense-in-depth, preventing event loop drain from reaching any remaining teardown code.
- **`process.exitCode = 1` in global handlers** (not `process.exit(1)`): allows pending I/O (e.g. ResultJson already written to stdout by `agent.ts`) to flush before exit.
- **`setTimeout(() => process.exit(1), 500).unref()` for uncaughtException**: after an uncaught exception, Node.js is in an undefined state. The safety timeout prevents the process from hanging if the event loop doesn't drain, while `.unref()` ensures the timer won't keep the process alive if it drains sooner.
- **stderr for diagnostics, stdout for ResultJson**: the global handlers write to stderr only, preserving the single-line stdout contract. The `main().catch()` writes a minimal `ResultJson` to stdout because at that point no ResultJson has been emitted yet.
- **Log file truncation is acceptable**: `closeFileLog()` calls `fileStream.end()` which is async. Some tail log data may be lost when `process.exit()` fires immediately after. This is fine — the log file is diagnostic, not the primary output contract.

## Prevention

- **Always call `close()` on SDK Query objects** after consuming the async iterator. The SDK registers `process.on('exit')` handlers that cause V8 crashes if not deregistered. Use `try { q.close(); } catch {}` in a `finally` block to handle double-close gracefully.
- Always force `process.exit()` after the main work is done in CLI tools that wrap SDKs with complex teardown. Don't trust Node.js event loop drain.
- Register `uncaughtException` and `unhandledRejection` handlers as a fallback for errors that occur before the main exit point.
- Use `process.exitCode` over `process.exit()` in global handlers to allow I/O flush, but add a safety timeout for `uncaughtException` specifically.
- Maintain strict stdout/stderr channel discipline: machine-readable output on stdout, diagnostics on stderr.
- Add no-op error handlers on best-effort I/O streams (log files) to prevent them from affecting exit codes.
- Use `.unref()` on timers that shouldn't keep the process alive during shutdown.
