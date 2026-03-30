---
status: completed
priority: p2
issue_id: "18"
tags: [code-review, typescript, type-safety]
dependencies: []
---

# ReadonlySet consistency across all module-level Sets

## Problem Statement

`SAFE_GH_SUBCOMMANDS` correctly uses `ReadonlyMap<string, ReadonlySet<string>>`, but the other module-level Sets (`SAFE_GIT_SUBCOMMANDS`, `SAFE_CARGO_SUBCOMMANDS`, `SAFE_NPM_RUN_SCRIPTS`, `SAFE_SHELL_COMMANDS`) are plain `Set<string>` with no `Readonly` annotation. This creates an inconsistency — the new code establishes a convention that the old code doesn't follow.

## Findings

- **Source**: kieran-typescript-reviewer (Medium severity)
- **Location**: `src/tier1.ts` lines 127, 161, 166, 201
- These are security-critical constants that should never be mutated at runtime
- `ReadonlySet` prevents accidental `.add()` or `.delete()` calls at the type level
- Also applies to `TIER3_PATTERNS` array (line 57) which should be `readonly RegExp[]`

## Proposed Solutions

### Option 1: Add ReadonlySet type annotations (Recommended)
```typescript
const SAFE_GIT_SUBCOMMANDS: ReadonlySet<string> = new Set([...]);
const SAFE_CARGO_SUBCOMMANDS: ReadonlySet<string> = new Set([...]);
const SAFE_NPM_RUN_SCRIPTS: ReadonlySet<string> = new Set([...]);
const SAFE_SHELL_COMMANDS: ReadonlySet<string> = new Set([...]);
const TIER3_PATTERNS: readonly RegExp[] = [...];
```
- **Pros**: One-line change per constant, consistent with SAFE_GH_SUBCOMMANDS, catches accidental mutation at compile time
- **Cons**: None
- **Effort**: Small (5 minutes)
- **Risk**: None

## Acceptance Criteria

- [ ] All module-level Sets in tier1.ts use `ReadonlySet<string>` type annotation
- [ ] `TIER3_PATTERNS` uses `readonly RegExp[]` type annotation
- [ ] `npx tsc --noEmit` passes
- [ ] All 195 tests pass unchanged

## Work Log

| Date | Action | Learnings |
|------|--------|-----------|
| 2026-03-30 | Created from ce:review | TypeScript reviewer flagged inconsistency |
