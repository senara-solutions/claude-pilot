---
title: "fix: call Query.close() and strip exit listeners to prevent exit code 5"
type: fix
status: completed
date: 2026-04-02
---

# fix: call Query.close() and strip exit listeners to prevent exit code 5

## Overview

claude-pilot still exits with code 5 (V8 fatal error) despite the `process.exit()` fix from #30. Two independent analyses converged on the same proximate trigger: the SDK's `process.on('exit')` handler calling `child.kill("SIGTERM")` during V8 teardown. They differ on root cause depth — one identifies a missing `q.close()` call, the other suspects V8 heap corruption from pipe/readline ops during the session. This plan combines both approaches for defense-in-depth.

## Problem Statement

Two prior fixes addressed exit code 5:
1. **#28** (`ebf92b6`): Added `uncaughtException`/`unhandledRejection` handlers — insufficient because exit code 5 bypasses JavaScript entirely
2. **#30** (`b5866de`): Added `process.exit(process.exitCode ?? 0)` after `runAgent()` — insufficient because `process.exit()` still triggers synchronous `process.on('exit')` handlers

The SDK's `ProcessTransport` registers this handler during `initialize()`:
```javascript
process.on("exit", () => {
  if (this.process && !this.process.killed) this.process.kill("SIGTERM");
});
```

The SDK's `close()` method calls `cleanup()` → `transport.close()` which deregisters this handler:
```javascript
close() {
  // ...
  if (this.processExitHandler)
    process.off("exit", this.processExitHandler), this.processExitHandler = void 0;
}
```

`cleanup()` is idempotent (guarded by `this.cleanupPerformed`), so calling `close()` after the iterator is naturally exhausted is safe.

**Why `process.exit()` alone doesn't help:** `process.exit(code)` calls V8 isolate disposal at the C++ level. If the SDK's exit handler fires during disposal (killing child, readline cleanup), or if the heap is already corrupted from pipe/readline ops during the session, the disposal itself crashes with exit code 5. No JavaScript-level fix can prevent a C++ crash — the fix must prevent the problematic code from running during disposal.

**Impact:** Every claude-pilot session reports exit code 5, causing mika-dev to treat successful work as failures (8-10 wasted tool calls per callback, work items incorrectly marked as blocked).

## Proposed Solution

Three layers of defense, each independently reducing the chance of exit code 5:

### Changes

#### 1. `src/agent.ts` — Call `q.close()` in finally block

The SDK `Query` object has a `close()` method that calls `transport.close()`, deregistering the `process.on('exit')` handler and gracefully killing the child. Currently never called.

```typescript
// Current (line 160-162):
  } finally {
    guardrails.dispose();
  }

// Proposed:
  } finally {
    // Deregister the SDK's process.on('exit') handler that kills the child
    // process during teardown. Without this, process.exit() in cli.ts triggers
    // the handler, causing a V8 fatal error (exit code 5).
    try { q.close(); } catch { /* already cleaned up */ }
    guardrails.dispose();
  }
```

The try-catch prevents `close()` from masking the original error if the transport is already torn down.

#### 2. `src/cli.ts` — Strip residual exit listeners before process.exit()

Even after `q.close()`, strip all exit listeners as defense-in-depth against heap corruption during V8 isolate disposal:

```typescript
// Current (lines 383-389):
  closeFileLog();
  process.exit(process.exitCode ?? 0);

// Proposed:
  closeFileLog();

  // Defense-in-depth: remove any residual exit listeners the SDK may
  // have registered. q.close() should handle this, but if V8 heap is
  // already compromised from pipe/readline ops, fewer exit handlers
  // means less code running during disposal.
  process.removeAllListeners("exit");

  process.exit(process.exitCode ?? 0);
```

#### 3. `package.json` — Upgrade SDK to latest

Bump `@anthropic-ai/claude-agent-sdk` from `^0.2.76` to latest. The SDK is 14+ versions behind; newer versions may have transport teardown fixes. Run `npm install && npm run build && npx tsc --noEmit`.

#### 4. `docs/solutions/runtime-errors/silent-exit-code-5-from-sdk-teardown.md` — Update solution doc

Add `q.close()` and `removeAllListeners("exit")` as the primary defense layers. Demote `process.exit()` to third layer. Document the SDK upgrade.

## Technical Considerations

- **`close()` is synchronous** (returns `void`) and **idempotent** (SDK guards with `cleanupPerformed` flag)
- **`close()` in abort path**: On `AbortError`, the catch block returns early, but `finally` still runs. `close()` will SIGTERM an already-aborting child — safe because `close()` checks `!this.process.killed` before sending signals
- **`removeAllListeners("exit")` scope**: Only strips listeners registered on the `process` object. Our own SIGINT/SIGTERM handlers are on different events and unaffected. The uncaughtException handler is also unaffected.
- **SDK upgrade risk**: Patch-level bump on a caret range (`^0.2.76` → `^0.2.90`). Same minor version, backward-compatible API. Type-check after install confirms compatibility.
- **`process.exit()` remains**: Keep it as defense-in-depth. After `q.close()` + `removeAllListeners`, it becomes a clean exit with no registered handlers to race against.

## Acceptance Criteria

- [x] `q.close()` called in `finally` block of `runAgent()` in `src/agent.ts`, wrapped in try-catch
- [x] SDK upgraded to latest version in `package.json`
- [x] Solution doc updated with new defense layers
- [x] `npx tsc --noEmit` passes
- [x] `npm run build` succeeds

## Verification

1. `npm install && npm run build && npx tsc --noEmit`
2. Run a test session: `./bin/claude-pilot --no-relay --cwd /tmp "echo hello"`
3. Check exit code: `echo $?` — should be 0, not 5
4. If still exit 5, add diagnostic before `process.exit()`:
   ```typescript
   process.stderr.write(`[diag] exit listeners: ${process.listenerCount("exit")}, handles: ${(process as any)._getActiveHandles?.()?.length ?? "?"}\n`);
   ```

## Sources

- Prior fix #28: `ebf92b6` (global error handlers)
- Prior fix #30: `b5866de` (process.exit)
- Independent analysis: `/home/samidarko/SynologyDrive/obsidian/main/Mika/abstract-knitting-fog.md`
- SDK source: `node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs` — `ProcessTransport.initialize()`, `ProcessTransport.close()`, `h9.cleanup()`
- Existing solution doc: `docs/solutions/runtime-errors/silent-exit-code-5-from-sdk-teardown.md`
- Prior plan: `docs/plans/2026-04-02-001-fix-v8-fatal-error-exit-code-5-on-teardown-plan.md`
