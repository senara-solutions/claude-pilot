---
title: "feat: Pass --task-id on intermediate relay mika ask calls"
type: feat
status: active
date: 2026-04-01
issue: "#24"
---

# Pass --task-id on intermediate relay mika ask calls

## Overview

Thread the `taskId` CLI option from `cli.ts` through the permission handler into the relay transport so that every `mika ask` invocation includes `--task-id <id>` when a task ID is available. This enables task correlation in the mika agent's session/trace metadata for permission and relay turns.

## Problem Statement

The relay transport (`src/transport.ts`) builds the external command as `mika --agent mika-dev ask -` without any task identifier. The `--task-id` value parsed from the claude-pilot CLI flows only to `agent.ts` for output/logging — it never reaches the relay invocation. This means intermediate permission/question relay calls during a claude-pilot session have no task correlation in the mika agent's session metadata.

Previously, `--task-id` was passed per-relay-call but caused "task already completed" errors because mika treated each invocation as a task completion attempt (see `docs/solutions/integration-issues/external-command-stdin-relay.md`). This was reverted. Now **mika#358 / mika#367** (merged 2026-04-01) separates `--task-id` (correlation-only) from `--task-complete` (completion), making it safe to re-add.

## Proposed Solution

Thread `taskId` through three files: `cli.ts` → `permissions.ts` → `transport.ts`. When present, inject `--task-id <id>` into the relay command args.

### Changes

#### 1. `src/permissions.ts` — Add `taskId` to handler options

Add `taskId?: string` to the `PermissionHandlerOptions` interface. Pass it through the closure to both `invokeCommand` calls (first attempt and retry).

```typescript
// src/permissions.ts:21-27
interface PermissionHandlerOptions {
  config?: PilotConfig;
  relay: boolean;
  verbose: boolean;
  cwd: string;
  guardrails?: SessionGuardrails;
  taskId?: string;  // ← NEW
}
```

#### 2. `src/transport.ts` — Accept and inject `taskId` in args

Add `taskId?: string` as a parameter to `invokeCommand`. Insert `--task-id <id>` into the args array following the same pattern as `--model`.

```typescript
// src/transport.ts:17-26
export async function invokeCommand(
  config: PilotConfig,
  event: PilotEvent,
  signal: AbortSignal,
  verbose: boolean,
  taskId?: string,  // ← NEW
): Promise<PilotResponse> {
  const timeout = config.timeout ?? 120_000;

  const args = [...(config.args ?? []), "-"];
  if (config.model) args.push("--model", config.model);
  if (taskId) args.push("--task-id", taskId);  // ← NEW
  // ...
}
```

#### 3. `src/cli.ts` — Pass `taskId` to permission handler

Pass `opts.taskId` into `createPermissionHandler`:

```typescript
// src/cli.ts:331-337
const permissionHandler = createPermissionHandler({
  ...(config && { config }),
  relay: opts.relay,
  verbose: opts.verbose,
  cwd: opts.cwd,
  guardrails,
  taskId: opts.taskId,  // ← NEW
});
```

#### 4. `src/permissions.ts` — Forward `taskId` to `invokeCommand`

In the `handler` closure, pass `opts.taskId` to both `invokeCommand` calls:

```typescript
// First attempt (line ~68)
const response = await invokeCommand(
  opts.config,
  event,
  sdkOptions.signal,
  opts.verbose,
  opts.taskId,  // ← NEW
);

// Retry attempt (line ~90)
const response = await invokeCommand(
  opts.config,
  retryEvent,
  sdkOptions.signal,
  opts.verbose,
  opts.taskId,  // ← NEW
);
```

## Acceptance Criteria

- [x] `PermissionHandlerOptions` includes `taskId?: string`
- [x] `invokeCommand` accepts and injects `--task-id <id>` into args when `taskId` is present
- [x] `cli.ts` passes `opts.taskId` to `createPermissionHandler`
- [x] Both first-attempt and retry `invokeCommand` calls receive `taskId`
- [x] When `taskId` is absent, no `--task-id` arg appears (existing behavior preserved)
- [x] `--task-complete` is NOT added anywhere
- [x] Verbose log line (`invoking: ...`) naturally shows `--task-id` when present
- [x] Type-check passes (`npx tsc --noEmit`)

## What NOT to Change

- **`PilotConfig`** — `taskId` is a runtime value, not a config file property
- **`PilotEvent`** — `task_id` in the stdin JSON payload is not needed; mika reads it from CLI args
- **`--task-complete`** — this flag is for task completion in `mika-skills/self-dev`, not for intermediate relay calls

## Sources

- **Issue:** [#24](https://github.com/senara-solutions/claude-pilot/issues/24)
- **Unblocked by:** mika#358 / mika#367 (merged 2026-04-01) — separated `--task-id` from `--task-complete`
- **Previous revert context:** `docs/solutions/integration-issues/external-command-stdin-relay.md`
- **Architecture doc:** `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md`
