---
title: "Silent exit code 5 from SDK teardown after successful session"
category: runtime-errors
date: 2026-04-02
severity: high
module: cli, agent, logger, guardrails
tags: [process-exit, uncaught-exception, unhandled-rejection, sdk-teardown, exit-code-5]
---

# Silent exit code 5 from SDK teardown after successful session

## Problem

claude-pilot sessions complete successfully (`[done] Success`) but the Node.js process exits with code 5 (V8 fatal error) during cleanup/teardown. Callers (resolve-pr-conflicts, address-pr-comments) see exit code 5 and treat the run as a failure, even though the agent session succeeded. The structured `ResultJson` on stdout is lost, and mika-dev receives: `"Process Exit code: 5: "` with no context.

## Root Cause

After `runAgent()` returns and the SDK session completes, the `@anthropic-ai/claude-agent-sdk` transport cleanup (readline interface on child stdout, SIGTERM of child process) triggers a V8 fatal error during Node.js event loop drain. This is a native engine crash that bypasses all JavaScript error handlers — `process.on('uncaughtException')` never fires for exit code 5.

## Solution

### Layer 1: Force clean exit (primary defense)

After `runAgent()` returns and `closeFileLog()` runs, force `process.exit()` to prevent Node.js from draining the event loop and triggering SDK teardown:

```typescript
// In cli.ts main(), after runAgent() returns:
closeFileLog();

// Force exit with the intended code. After runAgent() returns and ResultJson
// is on stdout, there is nothing left to do. Letting Node.js drain the event
// loop risks a V8 fatal error (exit code 5) from SDK transport cleanup.
// See: https://github.com/senara-solutions/claude-pilot/issues/29
process.exit(process.exitCode ?? 0);
```

This covers all post-`runAgent()` flows:
| Flow | exitCode | Result |
|------|----------|--------|
| Success | `undefined` → 0 | Exits 0 |
| SDK error result | `1` (set in agent.ts) | Exits 1 |
| Guardrail abort | `1` (set in agent.ts) | Exits 1 |
| User abort (SIGINT) | `undefined` → 0 | Exits 0 |

### Layer 2: Global error handlers (fallback)

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

### Layer 3: Hardened `main().catch()` with ResultJson

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

**Logger:** No-op error handler on the file write stream prevents log I/O errors from escalating to `uncaughtException` and changing the exit code:
```typescript
fileStream.on("error", () => {}); // Swallow — log file is best-effort
```

**Guardrails:** Idle timer uses `.unref()` so it cannot keep the event loop alive if `dispose()` is somehow skipped:
```typescript
this.state.idleTimer = setTimeout(() => { ... }, ms).unref();
```

## Key Design Decisions

- **`process.exit()` is the primary defense**, not the global handlers. The V8 fatal error (exit code 5) bypasses JavaScript entirely — `uncaughtException` never fires for it. Only `process.exit()` before teardown prevents the crash.
- **`process.exitCode = 1` in global handlers** (not `process.exit(1)`): allows pending I/O (e.g. ResultJson already written to stdout by `agent.ts`) to flush before exit.
- **`setTimeout(() => process.exit(1), 500).unref()` for uncaughtException**: after an uncaught exception, Node.js is in an undefined state. The safety timeout prevents the process from hanging if the event loop doesn't drain, while `.unref()` ensures the timer won't keep the process alive if it drains sooner.
- **stderr for diagnostics, stdout for ResultJson**: the global handlers write to stderr only, preserving the single-line stdout contract. The `main().catch()` writes a minimal `ResultJson` to stdout because at that point no ResultJson has been emitted yet.
- **Log file truncation is acceptable**: `closeFileLog()` calls `fileStream.end()` which is async. Some tail log data may be lost when `process.exit()` fires immediately after. This is fine — the log file is diagnostic, not the primary output contract.

## Prevention

- Always force `process.exit()` after the main work is done in CLI tools that wrap SDKs with complex teardown. Don't trust Node.js event loop drain.
- Register `uncaughtException` and `unhandledRejection` handlers as a fallback for errors that occur before the main exit point.
- Use `process.exitCode` over `process.exit()` in global handlers to allow I/O flush, but add a safety timeout for `uncaughtException` specifically.
- Maintain strict stdout/stderr channel discipline: machine-readable output on stdout, diagnostics on stderr.
- Add no-op error handlers on best-effort I/O streams (log files) to prevent them from affecting exit codes.
- Use `.unref()` on timers that shouldn't keep the process alive during shutdown.
