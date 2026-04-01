---
title: "fix: add uncaughtException/unhandledRejection handlers to prevent silent exit code 5"
type: fix
status: completed
date: 2026-04-01
---

# fix: add uncaughtException/unhandledRejection handlers to prevent silent exit code 5

## Overview

claude-pilot sessions complete successfully (`[done] Success`) but the Node.js process exits with code 5 (V8 fatal error) during cleanup/teardown. This causes calling handlers (resolve-pr-conflicts, address-pr-comments) to treat the run as a failure, even though the agent session succeeded.

## Problem Statement

Exit code 5 is never explicitly set in claude-pilot — it comes from the Node.js runtime itself when an unhandled exception or promise rejection occurs during event loop drain. `cli.ts` has no global error handlers:

- No `process.on('uncaughtException', ...)`
- No `process.on('unhandledRejection', ...)`

After the agent SDK session completes and `runAgent()` returns, dangling promises or connection cleanup in `@anthropic-ai/claude-agent-sdk` can trigger a fatal error that silently kills the process. The existing `main().catch()` handler only catches synchronous throws and rejected promises from the `main()` chain — it cannot catch errors from detached async operations.

### Impact

- Handler scripts see exit code 5 and report failure for successful work
- The structured JSON result (`ResultJson`) may never reach stdout
- mika executor delivers a sparse error: `"Process Exit code: 5: "` with no context
- mika-dev receives a failure callback for work that actually succeeded

## Proposed Solution

### 1. Add global error handlers at the top of `cli.ts` (before any async work)

```typescript
// src/cli.ts — add immediately after imports, before any function definitions

// Catch late-firing errors from SDK teardown / dangling promises.
// These handlers MUST be registered before any async work begins.
// They log structured JSON to stderr (not stdout — stdout is reserved for ResultJson)
// and exit with code 1 (not 5) so callers can distinguish "known crash" from "unknown fatal".
process.on("uncaughtException", (err) => {
  process.stderr.write(
    JSON.stringify({
      error: "uncaughtException",
      message: err.message,
      stack: err.stack,
    }) + "\n",
  );
  process.exitCode = 1;
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

**Key design decisions:**
- Use `process.exitCode = 1` instead of `process.exit(1)` — allows pending I/O (stdout flush of ResultJson) to complete
- Log to **stderr**, not stdout — stdout is the structured ResultJson channel; mixing error diagnostics there would break JSON parsing by callers
- Register handlers **before** `main()` — catches errors during any phase, not just post-teardown

### 2. Ensure ResultJson is flushed before teardown

The current code already writes ResultJson inside the `for await` loop in `agent.ts` (line 114), which happens before `main()` returns and `closeFileLog()` runs. However, if the SDK throws during iteration teardown (after yielding the result message but before the iterator completes), the write may not have flushed.

Add an explicit `process.stdout.write` callback or use synchronous write confirmation:

```typescript
// In agent.ts, after writing ResultJson to stdout, set a flag
let resultWritten = false;

// ... inside the result handler:
process.stdout.write(JSON.stringify(resultJson) + "\n");
resultWritten = true;
```

Then in the global `uncaughtException` handler, check this flag to avoid duplicate output.

### 3. Harden the `main().catch()` fallback

The existing handler logs to stderr and exits with code 1. Enhance it to also emit a structured `ResultJson` to stdout so callers always have parseable output:

```typescript
main().catch((err) => {
  const resultJson: ResultJson = {
    status: "error",
    subtype: "fatal",
    turns: 0,
    cost_usd: 0,
    duration_ms: 0,
    errors: [err instanceof Error ? err.message : String(err)],
  };
  process.stdout.write(JSON.stringify(resultJson) + "\n");
  process.stderr.write(`Fatal: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
```

## Acceptance Criteria

- [x] `process.on('uncaughtException', ...)` registered before `main()` in `src/cli.ts`
- [x] `process.on('unhandledRejection', ...)` registered before `main()` in `src/cli.ts`
- [x] Both handlers log structured JSON to stderr with error type, message, and stack
- [x] Both handlers set `process.exitCode = 1` (not `process.exit(1)`)
- [x] `main().catch()` emits a `ResultJson` to stdout before exiting
- [x] `npx tsc --noEmit` passes with no type errors
- [x] Existing tests continue to pass

## Files to Modify

| File | Change |
|------|--------|
| `src/cli.ts` | Add global `uncaughtException` and `unhandledRejection` handlers; enhance `main().catch()` |

## Sources

- Related issue: [#23](https://github.com/senara-solutions/claude-pilot/issues/23)
- Evidence: mika-dev turn audit (2026-04-01) — exit code 5 on successful session
