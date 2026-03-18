---
title: "fix: Add relay diagnostics and tool request logging"
type: fix
status: active
date: 2026-03-18
origin: docs/brainstorms/2026-03-18-fix-relay-and-monitoring-brainstorm.md
---

# fix: Add relay diagnostics and tool request logging

## Overview

A self-dev run auto-denied all 8 tool calls with "non-interactive mode" despite valid `.claude/claude-pilot.json` config at the worktree path. The log contained zero `[relay]` entries — the relay path was never entered. No diagnostic information existed to explain why. Two fixes: (1) make relay failures diagnosable, (2) make all tool requests visible.

## Problem Statement

The relay decision at `permissions.ts:42` (`!opts.relay || !opts.config`) silently disabled relay. The `[config]` startup phase has no logging — when `loadConfig()` returns `undefined`, there's no indication of what path was checked, whether the file existed, or why relay was disabled. The user sees only `[denied]` with no preceding context.

Additionally, denied tool calls only show the tool name + "non-interactive mode", not what Claude was attempting (command text, file path, etc.). This makes the log useless for understanding what happened.

## Proposed Solution

### 1. Add `--relay-config <path>` CLI flag

**File:** `src/cli.ts`

Add to `parseArgs()` switch block:
```typescript
case "--relay-config": {
  const value = args[++i];
  if (!value || value.startsWith("-")) {
    process.stderr.write("Error: --relay-config requires a path\n");
    usage();
  }
  relayConfig = resolve(value);
  break;
}
```

Add to return type and value: `relayConfig?: string`.

**In `main()`**, if `relayConfig` is set:
- Call `loadConfig` with the explicit path (new overload — see below)
- If file not found or invalid: **hard error** (`process.exit(1)`), not silent fallback
- Takes precedence over CWD-based config discovery
- If combined with `--no-relay`: warn that `--relay-config` is ignored

Update `loadConfig()` to accept an optional explicit path:
```typescript
function loadConfig(cwd: string, explicitPath?: string): PilotConfig | undefined {
  const configPath = explicitPath ?? resolve(cwd, ".claude", "claude-pilot.json");
  // ... rest unchanged, but if explicitPath was provided and file not found, throw instead of returning undefined
}
```

### 2. Add `[config]` startup log

**File:** `src/ui.ts` — add `logConfig()`:
```typescript
export function logConfig(cwd: string, configPath: string, found: boolean, relay: boolean): void {
  const status = found ? "found" : "NOT FOUND";
  const relayStr = relay ? "enabled" : "disabled";
  log(`${DIM}[config]${RESET} cwd=${cwd} config=${configPath} [${status}] relay=${relayStr}`);
}
```

**File:** `src/cli.ts` — call `logConfig()` after `loadConfig()` returns, before the relay-disable warning. This fires unconditionally (even when config is missing), so the log always shows what happened.

**Log output examples:**
```
[config] cwd=/data/workspace/project config=/data/workspace/project/.claude/claude-pilot.json [found] relay=enabled
[config] cwd=/data/workspace/worktree config=/data/workspace/worktree/.claude/claude-pilot.json [NOT FOUND] relay=disabled
[config] cwd=/data/workspace/project config=/explicit/path/config.json [found] relay=enabled  (--relay-config)
```

### 3. Log every tool request before decision logic

**File:** `src/ui.ts` — add `logToolRequest()`:
```typescript
export function logToolRequest(toolName: string, detail: string): void {
  log(`${DIM}[tool:request]${RESET} ${BOLD}${toolName}${RESET}: ${detail}`);
}
```

**File:** `src/permissions.ts` — add at top of `canUseTool` handler, AFTER the sub-agent auto-allow check (line 39), BEFORE the relay check (line 42):
```typescript
logToolRequest(toolName, summarizeInput(toolName, input));
```

This means sub-agent calls are NOT logged (too noisy), but every user-facing tool request is visible regardless of whether relay succeeds or fails.

### 4. Replace `logForwarded` with `logRelaySend` + add `logRelayRecv`

**File:** `src/ui.ts`:
- Rename `logForwarded()` → `logRelaySend()` (same format: `[relay:send] Bash → agent`)
- Add `logRelayRecv()`:
```typescript
export function logRelayRecv(toolName: string, action: string, latencyMs: number): void {
  const color = action === "allow" ? GREEN : action === "deny" ? RED : YELLOW;
  log(`${DIM}[relay:recv]${RESET} ${toolName} ← ${color}${action}${RESET} (${latencyMs}ms)`);
}
```

**File:** `src/permissions.ts`:
- Replace `logForwarded(toolName)` with `logRelaySend(toolName)` (line 56)
- After successful `invokeCommand()`, call `logRelayRecv(toolName, response.action, elapsed)`:
```typescript
const start = Date.now();
const response = await invokeCommand(...);
logRelayRecv(toolName, response.action, Date.now() - start);
return mapResponse(toolName, input, response);
```
- On retry path: `logRelayRecv(toolName, "error", Date.now() - start)` before `logRetry()`

### 5. Log sequence examples

**Happy path (relay works):**
```
[config] cwd=/project config=/project/.claude/claude-pilot.json [found] relay=enabled
[init] Session abc12345, model claude-opus-4-6[1m], task t1
[tool:request] Bash: gh issue view 198 --json body
[relay:send] Bash → agent
[relay:recv] Bash ← allow (2100ms)
[tool] Bash: gh issue view 198 --json body → ALLOW
```

**Relay failure (config not found):**
```
[config] cwd=/worktree config=/worktree/.claude/claude-pilot.json [NOT FOUND] relay=disabled
Warning: No .claude/claude-pilot.json found — running in no-relay mode.
[init] Session abc12345, model claude-opus-4-6[1m], task t1
[tool:request] Bash: gh issue view 198 --json body
[denied] Bash: non-interactive mode — auto-denied
```

**Relay failure (transport error, retry, fallback):**
```
[config] cwd=/project config=/project/.claude/claude-pilot.json [found] relay=enabled
[tool:request] Bash: cargo test
[relay:send] Bash → agent
[relay:recv] Bash ← error (120000ms)
[retry] Command failed: timeout — retrying with error feedback
[relay:send] Bash → agent
[relay:recv] Bash ← error (120000ms)
[fallback] Command failed: timeout — answering from claude-pilot
[denied] Bash: non-interactive mode — auto-denied
```

## Acceptance Criteria

- [x] `claude-pilot --verbose "test"` logs `[config]` line showing CWD, config path, found/not-found, relay status
- [x] `claude-pilot --relay-config /nonexistent "test"` exits with error (not silent fallback)
- [x] `claude-pilot --relay-config ./valid-config.json "test"` uses the explicit config
- [x] `claude-pilot --relay-config ./config.json --no-relay "test"` warns that `--relay-config` is ignored
- [x] Every tool request logs `[tool:request]` with tool name + input summary before any decision
- [x] Successful relay logs `[relay:send]` then `[relay:recv]` with action + latency
- [x] Failed relay logs `[relay:recv]` with error before retry/fallback
- [x] Sub-agent tool calls are NOT logged as `[tool:request]` (only auto-allowed silently)
- [x] `npx tsc --noEmit` passes
- [x] README documents `--relay-config` flag

## MVP

### src/cli.ts

Add `--relay-config` parsing and `logConfig()` call:

```typescript
// In parseArgs():
let relayConfig: string | undefined;
// ...
case "--relay-config": {
  const value = args[++i];
  if (!value || value.startsWith("-")) {
    process.stderr.write("Error: --relay-config requires a path\n");
    usage();
  }
  relayConfig = resolve(value);
  break;
}
// In return: add relayConfig

// In main():
const config = loadConfig(opts.cwd, opts.relayConfig);
logConfig(opts.cwd, opts.relayConfig ?? resolve(opts.cwd, ".claude", "claude-pilot.json"), !!config, opts.relay && !!config);

if (opts.relay && !config) {
  if (opts.relayConfig) {
    // Explicit path: hard error
    process.stderr.write(`Error: Config file not found: ${opts.relayConfig}\n`);
    process.exit(1);
  }
  // CWD discovery: warning + disable
  process.stderr.write("Warning: No .claude/claude-pilot.json found...\n");
  opts.relay = false;
}

if (!opts.relay && opts.relayConfig) {
  process.stderr.write("Warning: --relay-config is ignored when --no-relay is active\n");
}
```

### src/permissions.ts

Add `logToolRequest` call and relay timing:

```typescript
import { logToolRequest, logRelaySend, logRelayRecv, ... } from "./ui.js";

const handler: CanUseTool = async (toolName, input, sdkOptions) => {
  // Sub-agent: auto-allow (no logging)
  if (sdkOptions.agentID) {
    if (opts.verbose) logVerbose(`auto-allowing sub-agent ${sdkOptions.agentID}: ${toolName}`);
    return { behavior: "allow" as const, updatedInput: input };
  }

  // Log every user-facing tool request
  logToolRequest(toolName, summarizeInput(toolName, input));

  if (!opts.relay || !opts.config) {
    return interactiveFallback(toolName, input);
  }

  // ... build event ...
  logRelaySend(toolName);

  const start = Date.now();
  try {
    const response = await invokeCommand(...);
    logRelayRecv(toolName, response.action, Date.now() - start);
    return mapResponse(toolName, input, response);
  } catch (err) {
    if (err instanceof TransportError) {
      logRelayRecv(toolName, "error", Date.now() - start);
      logRetry(`${err.message} — retrying with error feedback`);
      // ... retry logic with same timing pattern ...
    }
    throw err;
  }
};
```

### src/ui.ts

Add new log functions, rename `logForwarded` → `logRelaySend`:

```typescript
export function logConfig(cwd: string, configPath: string, found: boolean, relay: boolean): void {
  const status = found ? "found" : "NOT FOUND";
  const relayStr = relay ? "enabled" : "disabled";
  log(`${DIM}[config]${RESET} cwd=${cwd} config=${configPath} [${status}] relay=${relayStr}`);
}

export function logToolRequest(toolName: string, detail: string): void {
  log(`${DIM}[tool:request]${RESET} ${BOLD}${toolName}${RESET}: ${detail}`);
}

export function logRelaySend(toolName: string): void {
  log(`${DIM}[relay:send]${RESET} ${toolName} → agent`);
}

export function logRelayRecv(toolName: string, action: string, latencyMs: number): void {
  const color = action === "allow" ? GREEN : action === "deny" ? RED : YELLOW;
  log(`${DIM}[relay:recv]${RESET} ${toolName} ← ${color}${action}${RESET} (${latencyMs}ms)`);
}
```

Remove `logForwarded()`.

### README.md

Add to Options section:
```
--relay-config <path>  Explicit path to claude-pilot config JSON. Overrides
                       CWD-based discovery (.claude/claude-pilot.json).
                       Hard error if file not found or invalid.
```

## Sources

- **Origin brainstorm:** [docs/brainstorms/2026-03-18-fix-relay-and-monitoring-brainstorm.md](docs/brainstorms/2026-03-18-fix-relay-and-monitoring-brainstorm.md)
- **Root cause context:** Failed self-dev run log at `/var/log/claude-pilot/4a964828-279d-4cb4-bfda-713df793c5bd.log`
- **Threading CLI options pattern:** [docs/solutions/architecture/threading-cli-option-through-layered-architecture.md](docs/solutions/architecture/threading-cli-option-through-layered-architecture.md)
- **Stdin relay learning:** [docs/solutions/integration-issues/external-command-stdin-relay.md](docs/solutions/integration-issues/external-command-stdin-relay.md)
