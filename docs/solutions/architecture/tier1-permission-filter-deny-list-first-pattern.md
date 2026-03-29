---
title: "Tier 1 permission filter: deny-list-first pattern for safe tool auto-approval"
category: architecture
date: 2026-03-29
tags: [permissions, security, performance, canUseTool, deny-list]
severity: medium
modules: [permissions, tier1]
---

# Tier 1 permission filter: deny-list-first pattern for safe tool auto-approval

## Problem

All tool permission requests in claude-pilot were relayed to the external agent (mika-dev) for LLM-based classification, even trivially safe operations like `Read`, `Glob`, `Grep`, and `git status`. In a typical session, ~53% of permissions were Tier 1 (always "allow"), wasting ~430K tokens and ~82s of latency per session.

## Root Cause

The `canUseTool` callback in `permissions.ts` had a single path: relay everything to the external agent. No local classification existed. Every tool request — regardless of safety — incurred the full round-trip: serialize event → execFile transport → LLM inference → parse response.

## Solution

Added a `src/tier1.ts` module with a synchronous `isTier1AutoApprove(toolName, input, cwd)` function, inserted as an early return in `createPermissionHandler()` before any relay logic.

### Key design: deny-list-first

```typescript
export function isSafeBashCommand(command: string): boolean {
  // 1. Deny-list scans FULL raw command string FIRST
  if (isTier3Dangerous(command)) return false;

  // 2. Only then split and check safe patterns
  const subCommands = splitCompoundCommand(command);
  return subCommands.every((sub) => isSafeSubCommand(sub));
}
```

The deny-list runs on the **full raw command string** before any splitting. This catches:
- Command substitution: `$(rm -rf /)` — the `$(` pattern matches even inside a "safe" command
- Pipe-to-dangerous: `echo | xargs rm` — `xargs` matches on the raw string
- Find with -exec: `find . -exec rm {} \;` — matched before `find` hits the safe-list

### Security findings from review

1. **`gh api` was unrestricted** — auto-approved without checking for `-X DELETE`/`--method POST`. Fixed by rejecting commands with method override or field input flags.
2. **`cp`, `mv`, `touch`, `env` were in the safe list** — these can write outside the project directory, bypassing the `isWithinProject()` check that protects Write/Edit. Removed.
3. **`git config` was in safe git subcommands** — can install malicious hooks (`core.hooksPath`) or create aliases that execute arbitrary commands. Removed.
4. **`git branch -D` not in deny-list** — force-deletes branches. Added.
5. **Process substitution `<(...)` not in deny-list** — allows embedding arbitrary commands. Added.

### Write/Edit path safety

Uses `fs.realpathSync()` to resolve symlinks before checking path containment:

```typescript
export function isWithinProject(filePath: string, cwd: string): boolean {
  const resolvedCwd = realpathSync(cwd);
  const absPath = resolve(resolvedCwd, filePath);
  const resolvedPath = tryResolveRealPath(absPath); // realpathSync with parent fallback
  const rel = relative(resolvedCwd, resolvedPath);
  return !rel.startsWith("..") && !isAbsolute(rel);
}
```

For new files (Write creating), resolves the parent directory's realpath + basename.

## Prevention

1. **When adding commands to safe-lists, ask: "Can this write outside the project?"** — if yes, relay it. Shell commands don't get `isWithinProject()` checks.
2. **When adding tool-specific auto-approvals (like `gh api`), check ALL flags** — a command that defaults to safe (GET) may have flags that make it destructive (DELETE).
3. **Deny-list must scan raw string, not parsed sub-commands** — command substitution, xargs, and other amplifiers hide inside "safe" wrappers.
4. **Test every deny-list pattern with an embedding test** — e.g., test `rm -rf` not just standalone but embedded in `echo $(rm -rf /)`.

## Files

- `src/tier1.ts` — filter module (deny-list, safe patterns, path safety)
- `src/permissions.ts` — integration point (early return before relay)
- `test/tier1.test.ts` — 160 test cases
