---
title: "Code review fixes: type safety, path traversal, env scrubbing, schema compliance"
category: code-quality
date: 2026-03-17
tags:
  - defensive-programming
  - type-safety
  - security
  - validation
  - schema-compliance
  - code-review
severity: medium
components:
  - src/cli.ts
  - src/permissions.ts
  - src/transport.ts
  - src/agent.ts
---

# Code review fixes: type safety, path traversal, env scrubbing, schema compliance

## Problem

Multi-agent code review of claude-pilot (TypeScript CLI wrapping Claude Code via Agent SDK) identified 6 actionable issues across the codebase: 2 P1 (type safety violations that could crash at runtime) and 4 P2 (security and defensive programming gaps). All were fixed and verified with `tsc --noEmit` and `npm run build`.

## Root Causes

**Type safety violations:** Unsafe narrowing via `as` casts without runtime validation in error handling and SDK input paths. Accessing `.message` on `unknown` values without type guards in `.catch()` callbacks.

**Security / input validation:** Task ID parameter used directly in file paths without sanitization, enabling path traversal. Environment variable scrubbing regex patterns incomplete or inconsistently anchored, allowing sensitive keys like `AWS_ACCESS_KEY_ID` and `DATABASE_URL` to leak to external agents.

**Configuration state management:** Fallback config object `{ command: "" }` violated own Zod schema (`z.string().min(1)`), creating an impossible state that the type system couldn't prevent.

## Solution

| Issue | Location | Fix | Impact |
|-------|----------|-----|--------|
| **Unsafe `err.message`** | `cli.ts:179` | `err instanceof Error ? err.message : String(err)` | Prevents crash on non-Error catch values |
| **Unsafe questions cast** | `permissions.ts:195` | `Array.isArray()` check + early deny return | Prevents crash in AskUserQuestion fallback path |
| **Path traversal via --task-id** | `cli.ts:136` | `taskId?.replace(/[^a-zA-Z0-9_-]/g, "_")` | Prevents writing logs outside intended directory |
| **Incomplete env scrubbing** | `transport.ts:6` | Expanded from 5 to 9 patterns; removed `$` anchor from `/KEY$/i`; added `DATABASE_URL`, `DSN`, `AUTH`, `PRIVATE` | Prevents credential leakage to external agent |
| **Unsafe error array cast** | `agent.ts:60` | `Array.isArray(rawErrors) && rawErrors.every((e): e is string => typeof e === "string")` | Prevents silent data corruption from malformed SDK errors |
| **Phantom config state** | `cli.ts:160`, `permissions.ts:18` | Made `config` optional in type; added `!opts.config` guard | Type system prevents impossible fallback state |

### Key code changes

**Safe error handling pattern (cli.ts):**

```typescript
// Before: crashes if non-Error thrown
main().catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
});

// After: handles unknown safely
main().catch((err) => {
  process.stderr.write(`Fatal: ${err instanceof Error ? err.message : String(err)}\n`);
});
```

**Runtime guard before iteration (permissions.ts):**

```typescript
// Before: unsafe cast, crashes on malformed input
const questions = input.questions as Array<{...}>;

// After: runtime check with graceful fallback
const questions = input.questions;
if (!Array.isArray(questions)) {
  return { behavior: "deny", message: "Malformed AskUserQuestion: missing questions array" };
}
```

**Filename sanitization (cli.ts):**

```typescript
// Before: path traversal possible
const logName = opts.taskId ? `${opts.taskId}.log` : "session.log";

// After: only safe characters
const sanitized = opts.taskId?.replace(/[^a-zA-Z0-9_-]/g, "_");
const logName = sanitized ? `${sanitized}.log` : "session.log";
```

**Broadened env scrubbing (transport.ts):**

```typescript
// Before: /KEY$/i missed AWS_ACCESS_KEY_ID (ends in _ID)
const SCRUB_PATTERNS = [/KEY$/i, /SECRET/i, /TOKEN$/i, /PASSWORD/i, /CREDENTIAL/i];

// After: unanchored patterns + new categories
const SCRUB_PATTERNS = [/KEY/i, /SECRET/i, /TOKEN/i, /PASSWORD/i, /CREDENTIAL/i, /^DATABASE_URL$/i, /DSN$/i, /AUTH/i, /PRIVATE/i];
```

## Prevention

### Never access properties on `unknown` without guards

Always check `instanceof Error` before accessing `.message` in `.catch()` blocks. Consider a utility: `const getErrorMessage = (err: unknown) => err instanceof Error ? err.message : String(err)`.

### Never use `as` casts on external data

For SDK inputs, add runtime guards: `Array.isArray()` before iterating, `typeof` checks before accessing properties. Use Zod schemas when the shape is complex. Every `as` cast on external data is a potential crash site.

### Sanitize user input before filesystem use

Whitelist approach: `input.replace(/[^a-zA-Z0-9_-]/g, "_")`. Always use `path.join(baseDir, sanitizedName)` rather than string interpolation. Audit all paths constructed from CLI arguments.

### Use unanchored patterns for env scrubbing

Avoid `$`-anchored patterns like `/KEY$/i` that miss compound names (`AWS_ACCESS_KEY_ID`). Test patterns against both simple names (`API_KEY`) and compound names. Consider an allowlist approach for maximum security.

### Never bypass your own validation schemas

If a Zod schema requires `min(1)`, don't create synthetic defaults that violate it. Make the field optional in the type system instead, and handle `undefined` at runtime. The type system should prevent impossible states.

### Cross-cutting change checklist

When fixes touch 4+ files:

1. `npx tsc --noEmit` for type errors
2. Search for patterns being fixed elsewhere in the codebase (inconsistency check)
3. Verify runtime guards exist on all fallback paths
4. Confirm env scrub patterns cover common secret naming conventions
5. `npm run build` to verify output

## Related Documents

- `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` -- Foundational architecture. Establishes the `execFile` transport, pattern-based env scrubbing, and Zod validation patterns that this review hardened.
- `docs/solutions/architecture/threading-cli-option-through-layered-architecture.md` -- Documents the `--task-id` threading pattern and CLI argument validation. Contains the cross-cutting change checklist used to verify these fixes.
