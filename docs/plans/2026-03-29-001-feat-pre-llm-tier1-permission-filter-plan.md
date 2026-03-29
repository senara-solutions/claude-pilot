---
title: "feat: pre-LLM Tier 1 permission filter to skip relay for safe operations"
type: feat
status: completed
date: 2026-03-29
issue: "#14"
---

# feat: pre-LLM Tier 1 permission filter to skip relay for safe operations

## Overview

Add a `tier1.ts` module that short-circuits safe tool operations in `createPermissionHandler()` before relaying to the external agent. This eliminates ~53% of relay round-trips (~430K tokens, ~82s latency per session) for trivially safe Tier 1 operations like Read, Glob, Grep, and safe Bash commands.

## Problem Statement

The current flow: Claude Code tool request → claude-pilot relay → mika-dev LLM inference → classify Tier 1 → respond `{"action": "allow"}`. For Read/Glob/Grep this is pure overhead — the answer is always "allow."

## Proposed Solution

### Architecture

```
Tool request arrives at createPermissionHandler()
  │
  ├─ logToolRequest()           ← existing (line 32)
  │
  ├─ isTier1AutoApprove()?  ←── NEW: early return before relay
  │   ├─ YES → logTool(AUTO) → return {behavior: "allow"}
  │   └─ NO  → continue to relay/interactive
  │
  ├─ relay disabled? → interactiveFallback()    ← existing (line 35)
  │
  └─ build PilotEvent → invokeCommand()         ← existing (line 40+)
```

### Tier Classification

**Always auto-approve (tool name alone):**
- `Read`, `Glob`, `Grep`

**Auto-approve with input inspection:**
- `Bash` — deny-list scan on full raw command string first, then split on `&&`/`||`/`;`/`|` and check each sub-command against safe patterns
- `Write`/`Edit` — auto-approve if `file_path` resolves within the project directory (symlink-aware)

**Never auto-approve (always relay):**
- `AskUserQuestion` — always needs agent intelligence
- Unrecognized tools — conservative default

### Deny-List (Checked First — Fail-Safe)

Applied to the **full raw command string** before splitting. If any pattern matches, return `false` (relay decides):

| Pattern | Rationale |
|---------|-----------|
| `rm -rf` | Destructive file deletion |
| `git push --force` / `git push -f` | Force push overwrites history |
| `git push` to `main`/`master` | Direct push to protected branches |
| `git reset --hard` | Discards uncommitted work |
| `DROP TABLE` / `DELETE FROM` | Destructive SQL |
| `cargo publish` | Publishes crate to registry |
| `sed -i` | In-place file modification |
| `gh label delete` / `gh label edit` | Destructive GitHub ops |
| `bash -c` / `sh -c` / `eval` | Arbitrary code execution via interpreter |
| `xargs` | Command amplifier, cannot statically analyze |
| `find` with `-exec` or `-delete` | Destructive find operations |
| `$(` or backticks | Command substitution — cannot statically analyze contents |

### Safe Bash Patterns (After Deny-List Pass)

Each sub-command (after splitting) must match ONE of these:

**Safe git:**
- `git status`, `git log`, `git diff`, `git branch`, `git show`, `git commit`, `git push` (not to main/master), `git checkout`, `git worktree`, `git rev-parse`, `git remote`, `git fetch`, `git pull`, `git add`, `git stash`, `git tag`, `git merge`
- NOT with `--force` / `-f` flag

**Build/test:**
- `cargo check/test/clippy/fmt/build`
- `npm run build/dev/test/lint/fmt`, `npm install`, `npm ci`
- `npx tsc`, `npx vitest`

**Read-only shell:**
- `ls`, `cat`, `head`, `tail`, `wc`, `find` (without -exec/-delete), `grep`, `sed` (without -i), `awk`, `mkdir`, `echo`, `printf`, `dirname`, `basename`, `realpath`, `readlink`, `stat`, `file`, `which`, `type`, `env`, `pwd`, `date`, `sort`, `uniq`, `tr`, `cut`, `diff`, `comm`, `test`, `[`

**PR/issue ops:**
- `gh pr create/view/list/checkout/diff/checks`, `gh issue view/list`

### Write/Edit Path Safety (`isWithinProject`)

1. Receive `cwd` (project root) via `PermissionHandlerOptions`
2. For existing paths: `fs.realpathSync(filePath)` to resolve symlinks
3. For non-existent paths (Write): `fs.realpathSync(dirname(filePath))` + basename
4. If parent also doesn't exist: return `false` (relay)
5. Check: `path.relative(resolvedCwd, resolvedPath)` must not start with `..` and must not be absolute

## Technical Considerations

### Security Design Decisions

1. **Deny-list scans full raw command string** — catches `$(rm -rf /)`, `echo | xargs rm`, `find -exec rm` patterns. Accepts false positives (safe but wastes a relay call) over false negatives (dangerous command auto-approved).

2. **Command substitution blocked** — `$(...)` and backtick patterns are in the deny-list. Cannot statically analyze nested commands.

3. **No quote-aware splitting** — Naive split on delimiters. Garbled sub-commands (from splitting inside quotes) won't match safe patterns → fall through to relay. This is safe and simple.

4. **`python3` removed from safe list** — Turing-complete interpreter, `python3 -c "..."` can execute anything.

5. **`tee` removed from safe list** — Writes to files, not read-only.

6. **Symlink resolution for Write/Edit** — `fs.realpathSync()` prevents symlink traversal attacks.

### Interface Changes

```typescript
// permissions.ts — PermissionHandlerOptions gains cwd
interface PermissionHandlerOptions {
  config?: PilotConfig;
  relay: boolean;
  verbose: boolean;
  cwd: string;  // NEW: project root for Write/Edit path checks
}
```

```typescript
// cli.ts — pass cwd to createPermissionHandler
const handler = createPermissionHandler({
  config,
  relay: !noRelay,
  verbose,
  cwd: resolvedCwd,
});
```

### Threat Model

The tier1 filter trusts:
- The project directory contents (npm scripts, config files)
- Git operations within the project
- Read operations on any file (consistent with Read tool being unconditionally approved)

The tier1 filter does NOT trust:
- Arbitrary command execution (eval, bash -c, python3 -c)
- Write operations outside the project
- Commands with force/destructive flags
- Unknown tools

## System-Wide Impact

- **Interaction graph**: `isTier1AutoApprove()` is called synchronously before any async relay. No callbacks, no observers, no side effects beyond logging.
- **Error propagation**: No new error paths. Returns boolean; false = fall through to existing flow.
- **State lifecycle risks**: None — pure function, no state mutation.
- **API surface parity**: The `PermissionHandlerOptions.cwd` addition is the only interface change. `cli.ts` already has `cwd` resolved.

## Implementation Steps

### 1. Create `src/tier1.ts`

New module with exports:
- `isTier1AutoApprove(toolName: string, input: Record<string, unknown>, cwd: string): boolean`
- Internal helpers: `isTier3Dangerous(command)`, `isSafeBashCommand(command)`, `isSafeGitCommand(sub)`, `isSafeBuildCommand(sub)`, `isSafeShellCommand(sub)`, `isSafePrCommand(sub)`, `isWithinProject(filePath, cwd)`

### 2. Integrate into `src/permissions.ts`

- Add `cwd: string` to `PermissionHandlerOptions`
- Import `isTier1AutoApprove` from `./tier1.js`
- Insert early return after `logToolRequest()` (line 32), before relay check (line 35):

```typescript
// Tier 1 auto-approval: skip relay for safe operations
if (isTier1AutoApprove(toolName, input, opts.cwd)) {
  logTool(toolName, summarizeInput(toolName, input), "AUTO");
  return { behavior: "allow", updatedInput: input };
}
```

### 3. Update `src/cli.ts`

- Pass `cwd` to `createPermissionHandler()` call

### 4. Install vitest and create `test/tier1.test.ts`

No test framework exists. Add vitest as dev dependency. Test cases:

**Read-only tools:**
- Read, Glob, Grep → auto-approve regardless of input

**Safe Bash:**
- `git status` / `git log` / `git diff` / `git branch` / `git show` / `git commit` / `git push origin feat` / `git checkout -b feat` / `git worktree add`
- `cargo test` / `cargo build` / `cargo clippy` / `cargo fmt` / `cargo check`
- `npm run build` / `npm run test` / `npm install`
- `ls -la` / `cat file` / `head -10 file` / `grep pattern file` / `mkdir -p dir`

**Compound commands:**
- `git status && cargo test` → auto-approve (both safe)
- `ls | grep pattern` → auto-approve (both safe)
- `git status && rm -rf /` → relay (deny-list match)
- `safe ; dangerous` → relay

**Dangerous (deny-list):**
- `rm -rf /tmp` / `git push --force` / `git push -f origin main` / `git reset --hard`
- `DROP TABLE users` / `cargo publish` / `sed -i 's/a/b/' file`
- `bash -c "anything"` / `sh -c "anything"` / `eval "anything"` / `xargs rm`
- `find . -exec rm {} \;` / `find . -delete`
- `echo $(rm -rf /)` / `` echo `rm -rf /` ``
- `git push origin main` / `git push origin master`

**Write/Edit path safety:**
- `Write` to `src/foo.ts` (within project) → auto-approve
- `Write` to `/etc/passwd` → relay
- `Edit` with `../../outside/file` → relay
- `Write` to non-existent path within project → auto-approve

**Never auto-approve:**
- `AskUserQuestion` → relay
- `UnknownTool` → relay

**Edge cases:**
- Empty Bash command → relay
- Bash with no `command` field → relay
- Write with no `file_path` field → relay
- `sed` without `-i` → auto-approve
- `git push` without args → auto-approve

### 5. Update CLAUDE.md

Add `src/tier1.ts` to architecture overview.

### 6. Export `summarizeInput` from permissions.ts

Currently private. Either export it, or duplicate the minimal logic needed in the integration point. Since `logTool` in the integration point needs a summary, and `summarizeInput` is right there in `permissions.ts`, keeping it local and using it inline is cleanest.

## Acceptance Criteria

- [x] New `src/tier1.ts` module with `isTier1AutoApprove()` function
- [x] Deny-list scans full raw command string before any splitting
- [x] Command substitution (`$(...)`, backticks) blocked by deny-list
- [x] `git push` to main/master detected and relayed
- [x] Integrated into `createPermissionHandler()` before relay
- [x] `PermissionHandlerOptions` extended with `cwd: string`
- [x] `isWithinProject()` uses `fs.realpathSync()` for symlink safety
- [x] Comprehensive vitest test suite (`test/tier1.test.ts`)
- [x] AUTO-approved operations logged with "AUTO" tag
- [x] No Tier 3 operations ever auto-approved
- [x] `python3`, `tee` NOT in safe list
- [x] CLAUDE.md updated with `tier1.ts` in architecture overview

## Expected Impact

- ~50% reduction in relay round-trips per session
- ~430K token savings per session on mika-dev LLM
- ~80s faster claude-pilot sessions

## Dependencies

- vitest (new dev dependency for tests)
- No runtime dependencies — pure TypeScript module

## Companion Issues

- mika#313 — Simplify engine callback framing
- mika#314 — Inject active work items into callback turns

## Sources

- Issue: [#14](https://github.com/senara-solutions/claude-pilot/issues/14)
- Prior plan: `docs/plans/2026-03-28-001-feat-pre-llm-tier-1-permission-filter-plan.md`
- Learnings: `docs/solutions/code-quality/code-review-fixes-type-safety-and-security-hardening.md` — runtime type guards, path traversal prevention
- Learnings: `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — every permission decision must log
- Architecture: `docs/solutions/architecture/sdk-wrapper-replaces-tmux-relay.md` — canUseTool callback flow
