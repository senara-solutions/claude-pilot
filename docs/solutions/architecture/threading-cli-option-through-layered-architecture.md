---
title: "Threading a new CLI option through a layered TypeScript CLI architecture"
category: architecture
date: 2026-03-16
tags:
  - typescript
  - cli-design
  - option-threading
  - agent-sdk
  - json-contract
  - config-migration
severity: low
components:
  - src/cli.ts
  - src/types.ts
  - src/permissions.ts
  - src/agent.ts
  - src/ui.ts
---

# Threading a new CLI option through a layered TypeScript CLI architecture

## Problem

claude-pilot had no way for the calling agent (mika) to associate a run with a tracked task. The only identifier was the SDK's `session_id`, generated at runtime and unknown to the caller ahead of time. Mika couldn't correlate permission events or results back to the originating task.

Additionally, the config file lived at `.claude-pilot.json` in the project root — non-standard when `.claude/` is the conventional location for Claude-related configuration.

## Root Cause

No caller-provided correlation identifier existed in the `PilotEvent` or result JSON contracts. The architecture had a `session_id` threading pattern but no equivalent for caller-owned identifiers.

## Solution

Added `--task-id <id>` CLI option, threading it through the full pipeline:

```
CLI --task-id abc123
  → parseArgs returns { taskId: "abc123", ... }
  → createPermissionHandler receives taskId in options
  → Every PilotEvent includes { task_id: "abc123", ... }
  → Result JSON includes { task_id: "abc123", ... }
```

### Key implementation patterns

**Conditional spread for optional fields** — avoids sending `task_id: undefined` in JSON:

```typescript
const event: PilotEvent = {
  type: toolName === "AskUserQuestion" ? "question" : "permission",
  session_id: sessionId,
  ...(opts.taskId && { task_id: opts.taskId }),
  tool_name: toolName,
  // ...
};
```

**CLI argument validation** — prevents `--task-id --verbose` from silently consuming the next flag:

```typescript
case "--task-id": {
  const value = args[++i];
  if (!value || value.startsWith("-")) {
    process.stderr.write("Error: --task-id requires a value\n");
    usage();
  }
  taskId = value;
  break;
}
```

**Empty string normalization** — coerces empty strings to undefined at the boundary:

```typescript
taskId: taskId || undefined, // treat empty string as absent
```

**Formalized result output** — replaced ad-hoc `Record<string, unknown>` with typed interface:

```typescript
interface ResultJson {
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

### Design constraint

Per institutional learning: IDs go in the `PilotEvent` JSON on stdin, NOT as template variables in command args. This preserves the generic transport layer and prevents shell injection.

## Review Findings

Code review caught four issues worth documenting:

1. **Stale string literal** — After renaming `.claude-pilot.json` to `.claude/claude-pilot.json`, a Zod validation error message still referenced the old path. Fix: always search for old names after renaming.

2. **Silent argument consumption** — `--task-id` without a value consumed the next positional argument silently. Fix: validate that the value exists and doesn't look like a flag.

3. **Duplicate error extraction** — Error handling logic copy-pasted into two call sites instead of extracted once. Fix: extract into a single variable, reference twice.

4. **Dead import** — `SDKMessage` type import left behind after refactoring `handleMessage` into a closure. Fix: enable `noUnusedLocals` in tsconfig to catch automatically.

## Prevention Strategies

### String literals tied to config paths

Define paths as constants and reference them everywhere. After any rename, `grep -r` for the old name before committing.

### CLI argument validation

Always validate that option values don't start with `--` and aren't undefined. Consider a CLI parsing library for projects with many flags; for minimal CLIs, add explicit edge-case tests.

### Duplicate logic

When touching existing code to add a new feature, scan for the pattern you're about to reuse. If it appears more than once, extract first, then add the new call site.

### Dead imports

Enable `noUnusedLocals` in `tsconfig.json`. It catches dead imports as compile errors with zero runtime cost.

### Cross-cutting change checklist

When a change touches 4+ files across layers:

1. `npx tsc --noEmit` — type errors, dead imports
2. Search for any old names/paths that were renamed
3. Verify every new CLI flag handles missing-value case
4. Check for duplicated blocks (5+ similar lines → extract)
5. Confirm stdout stays machine-readable, stderr stays human-readable

## Related Documents

- `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` — Foundational architecture decision. Contains the "IDs in stdin JSON, not command args" constraint.
- `docs/plans/2026-03-15-002-feat-add-task-id-cli-option-plan.md` — Implementation plan for this feature.
- `docs/plans/2026-03-15-001-feat-claude-pilot-sdk-wrapper-plan.md` — Original implementation plan establishing the stdin JSON transport pattern.
- `docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md` — Initial design brainstorm.
