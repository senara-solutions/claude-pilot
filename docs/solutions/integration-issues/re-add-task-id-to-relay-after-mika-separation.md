---
title: "Re-add --task-id to relay calls after mika separated correlation from completion"
category: integration-issues
date: 2026-04-01
tags: [transport, task-id, relay, cli-args, mika, correlation]
---

## Problem

Intermediate relay calls (`mika --agent mika-dev ask -`) during a claude-pilot session had no task correlation in the mika agent's session metadata. The `--task-id` value parsed from the claude-pilot CLI flowed only to `agent.ts` for output/logging — it never reached the relay invocation.

Previously, `--task-id` was forwarded per-relay-call but was removed (see `external-command-stdin-relay.md`) because mika treated every `--task-id` invocation as a task completion attempt, causing cascading "task already completed" errors.

## Root Cause

mika's CLI conflated task correlation (`--task-id`) with task completion. Every relay permission callback with `--task-id` triggered a status transition, which failed on the second call. mika#358 / mika#367 (merged 2026-04-01) separated these into two distinct flags: `--task-id` (correlation-only metadata) and `--task-complete` (completion trigger).

## Solution

Thread `taskId` through the existing permission handler chain to `invokeCommand`, following the same pattern as `--model`:

**1. `src/permissions.ts`** — Add `taskId?: string` to `PermissionHandlerOptions` interface.

**2. `src/transport.ts`** — Add `taskId?: string` parameter to `invokeCommand`, inject into args:
```ts
if (taskId) args.push("--task-id", taskId);
```

**3. `src/cli.ts`** — Pass `opts.taskId` to `createPermissionHandler`.

**4. `src/permissions.ts`** — Forward `opts.taskId` to both `invokeCommand` calls (first attempt and retry).

Result: `mika --agent mika-dev ask --task-id <id> -` when taskId is present.

## Prevention

- Session-scoped metadata (`--model`, `--task-id`) is acceptable as CLI args since it is constant across all relay calls. Per-event data belongs in the stdin JSON payload (`PilotEvent`).
- When external commands use identifiers for both metadata and side effects, ensure the external command separates these concerns (as mika did with `--task-id` vs `--task-complete`) before forwarding identifiers on every invocation.
- Cross-reference: `external-command-stdin-relay.md` (original removal), `threading-cli-option-through-layered-architecture.md` (original `--task-id` implementation).
