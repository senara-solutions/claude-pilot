---
title: "External command not receiving stdin payload — missing positional '-' flag"
category: integration-issues
date: 2026-03-17
tags: [transport, stdin, execFile, mika, relay, cli-args]
---

## Problem

claude-pilot writes `PilotEvent` JSON to the external command's stdin via `execFile`, but the command (`mika ask`) never receives it. The command exits with code 0 and empty stdout, causing `"Command produced no output"`. The retry then re-sends the event, triggering cascading `"task already completed"` errors from mika.

Observed in log:
```
[relay] Bash → forwarded to agent
[retry] Command produced no output — retrying with error feedback
[fallback] Command failed: ... Error: Task '...' has status 'completed' and cannot be completed.
[denied] Bash: non-interactive mode — auto-denied
```

## Root Cause

`mika ask` requires a positional `<MESSAGE>` argument. The convention `"-"` means "read from stdin". Without it, mika ignores stdin entirely — the JSON payload is written but never read. Additionally, `taskId` was being forwarded through the permission relay chain as `--task-id` to the external command, which was unnecessary and caused mika to attempt task status transitions on every permission request.

## Solution

**1. Append `"-"` to args in `src/transport.ts`** so the external command knows to read from stdin:

```ts
// Before:
const args = [...(config.args ?? [])];

// After:
const args = [...(config.args ?? []), "-"];
```

**2. Remove `taskId` from the relay chain** — strip it from `PermissionHandlerOptions`, `invokeCommand` signature, and `cli.ts` handler construction. The task-id remains a CLI arg to claude-pilot itself, just not forwarded per-permission-request.

**3. Tighten log file permissions** — `0o700` for directory, `0o600` for files (defense in depth, since logs may contain tool input summaries).

**4. Add relay payload metadata logging** — verbose mode now logs `type`, `tool_name`, and `tool_use_id` to the file log for debugging without leaking secrets.

## Prevention

- When integrating with external CLI tools via stdin, always verify the tool's convention for reading stdin (usually `"-"` or `--stdin` flag). Don't assume piping to stdin is sufficient.
- Avoid forwarding identifiers that cause side effects (like task status transitions) on every permission callback — pass them once at session level, not per-event.
