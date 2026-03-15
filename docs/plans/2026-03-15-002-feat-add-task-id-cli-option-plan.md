---
title: "feat: Add --task-id CLI option for mika task tracking"
type: feat
status: completed
date: 2026-03-15
---

# Add --task-id CLI option for mika task tracking

## Overview

Add a `--task-id <id>` CLI flag to claude-pilot so the caller (mika) can associate a run with a tracked task. The task_id flows into every `PilotEvent` sent to the external agent and into the result JSON written to stdout on completion. The session_id (from the Claude SDK) is already in `PilotEvent` — this adds the caller's own correlation identifier alongside it.

## Problem Statement

Mika needs to track which claude-pilot runs belong to which task. Currently there is no caller-provided identifier — only the SDK's `session_id`, which is generated at runtime and unknown to the caller ahead of time. Without a task_id, mika cannot correlate permission events or results back to the originating task.

## Proposed Solution

Thread an optional `--task-id` through the existing data flow, following the same pattern as `session_id` but simpler (known at startup, not discovered at runtime).

```
CLI --task-id abc123
  → parseArgs returns { taskId: "abc123", ... }
  → createPermissionHandler receives taskId in options
  → Every PilotEvent includes { task_id: "abc123", ... }
  → Result JSON includes { task_id: "abc123", ... }
```

When omitted, `task_id` is absent from both PilotEvent and result JSON (key not present, not `null`).

## Acceptance Criteria

- [x] `--task-id <id>` CLI flag added, accepts any non-empty string
- [x] `task_id` included in every `PilotEvent` when provided
- [x] `task_id` included in result JSON on stdout when provided
- [x] `session_id` also added to result JSON (trivial, useful for correlation)
- [x] `task_id` shown in `[init]` log line when present
- [x] `--help` output documents the new flag
- [x] Config file moved from `.claude-pilot.json` to `.claude/claude-pilot.json`
- [x] CLAUDE.md updated with the new option and new config path
- [x] README.md updated with the new option and new config path

## Technical Approach

### File changes

#### `src/types.ts`

- Add `task_id?: string` to `PilotEvent` interface (after `session_id`)
- Create a `ResultJson` interface to formalize the result output shape (currently ad-hoc `Record<string, unknown>`):

```typescript
export interface ResultJson {
  status: "success" | "error";
  subtype: string;
  task_id?: string;
  session_id?: string;
  turns: number;
  cost_usd: number;
  duration_ms: number;
  errors?: string[];
}
```

#### `src/cli.ts`

- Add `taskId?: string` to `parseArgs` return type
- Add `--task-id` case to the switch (consume next arg, same pattern as `--cwd`)
- Add to `usage()` output
- Change config path from `resolve(cwd, ".claude-pilot.json")` to `resolve(cwd, ".claude", "claude-pilot.json")`

```typescript
case "--task-id":
  taskId = args[++i];
  break;
```

- Pass `taskId` through to `createPermissionHandler` and `runAgent`

#### `src/permissions.ts`

- Add `taskId?: string` to `PermissionHandlerOptions`
- Include `task_id: opts.taskId` in every `PilotEvent` construction (line ~48-58)
- When `opts.taskId` is undefined, omit the field (spread pattern or conditional)

#### `src/agent.ts`

- Add `taskId?: string` and capture `sessionId` on `AgentOptions`
- Pass both into the result JSON construction
- Refactor: `handleMessage` needs access to task_id and session_id for result output — either pass as parameters or make it a closure over `opts`
- Update `logInit` call to include task_id

#### `src/ui.ts`

- Update `logInit` signature to accept optional `taskId`
- Append to init line: `Session abc12345, model claude-sonnet, task my-task-123`

### Edge cases

| Scenario | Behavior |
|---|---|
| `--task-id` omitted | Field absent from PilotEvent and result JSON |
| `--task-id ""` (empty) | Treated as omitted (no task_id) |
| `--task-id` with no following value | Next arg consumed (same as `--cwd` pattern) — may eat the prompt, but "prompt is required" error surfaces |
| `--no-relay --task-id abc` | task_id still appears in result JSON (useful for caller tracking even without relay) |
| Abort (SIGINT) | No result JSON emitted (existing behavior, out of scope to change) |
| Retry events | task_id carried forward via spread operator — correct by default |

### Key constraint

Per institutional learning (`docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md`): IDs go in the `PilotEvent` JSON on stdin, **not** as template variables in command args. This change follows that pattern.

### Backward compatibility

The `task_id` field is optional in `PilotEvent`. The external agent (mika-dev) must ignore unknown fields or be updated first. If mika-dev does strict schema validation, coordinate: update mika-dev to accept `task_id` before deploying this change.

## Sources

- Institutional learning: `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` — IDs go in stdin JSON, not command args
- session_id pattern: `src/permissions.ts:31-58`, `src/agent.ts:35-37`
- Result JSON construction: `src/agent.ts:71-81`
