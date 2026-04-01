---
title: "Silent exit code 5 from SDK teardown after successful session"
category: runtime-errors
date: 2026-04-01
severity: high
module: cli, agent
tags: [process-exit, uncaught-exception, unhandled-rejection, sdk-teardown, exit-code-5]
---

# Silent exit code 5 from SDK teardown after successful session

## Problem

claude-pilot sessions complete successfully (`[done] Success`) but the Node.js process exits with code 5 (V8 fatal error) during cleanup/teardown. Callers (resolve-pr-conflicts, address-pr-comments) see exit code 5 and treat the run as a failure, even though the agent session succeeded. The structured `ResultJson` on stdout is lost, and mika-dev receives: `"Process Exit code: 5: "` with no context.

## Root Cause

`cli.ts` had no global error handlers:
- No `process.on('uncaughtException', ...)`
- No `process.on('unhandledRejection', ...)`

After `runAgent()` returns and the SDK session completes, dangling promises or connection cleanup in `@anthropic-ai/claude-agent-sdk` can trigger asynchronous errors during event loop drain. These escape the `main().catch()` handler (which only catches synchronous throws and rejected promises from the `main()` promise chain) and hit Node.js's default behavior: exit with code 5.

## Solution

### 1. Register global handlers early in `cli.ts` (after imports, before any functions)

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

### 2. Harden `main().catch()` to emit `ResultJson` to stdout

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

## Key Design Decisions

- **`process.exitCode = 1` in global handlers** (not `process.exit(1)`): allows pending I/O (e.g. ResultJson already written to stdout by `agent.ts`) to flush before exit.
- **`setTimeout(() => process.exit(1), 500).unref()` for uncaughtException**: after an uncaught exception, Node.js is in an undefined state. The safety timeout prevents the process from hanging if the event loop doesn't drain, while `.unref()` ensures the timer won't keep the process alive if it drains sooner.
- **stderr for diagnostics, stdout for ResultJson**: the global handlers write to stderr only, preserving the single-line stdout contract. The `main().catch()` writes a minimal `ResultJson` to stdout because at that point no ResultJson has been emitted yet.
- **No `resultWritten` flag**: the architecture review confirmed that `uncaughtException` and `main().catch()` cannot fire for the same error under normal conditions, so a cross-module flag would add unnecessary coupling.

## Prevention

- Always register `uncaughtException` and `unhandledRejection` handlers in CLI entry points that wrap async SDKs with potential dangling resources.
- Use `process.exitCode` over `process.exit()` in global handlers to allow I/O flush, but add a safety timeout for `uncaughtException` specifically.
- Maintain strict stdout/stderr channel discipline: machine-readable output on stdout, diagnostics on stderr.
