# Plan: feat: pre-LLM Tier 1 permission filter to skip relay for safe operations

**Issue:** [#14](https://github.com/senara-solutions/claude-pilot/issues/14)  
**Status:** active  
**Created:** 2026-03-28  
**Target:** claude-pilot  

## Context

From turn audit of mika-dev sessions: all permission requests go through full LLM inference on mika-dev, even trivially safe Tier 1 operations (Read, Glob, Grep, git status, cargo test). In a typical claude-pilot session, ~53% of permissions are Tier 1, wasting ~430K tokens and ~82s of LLM latency per session.

## Problem

The current flow: Claude Code tool request → claude-pilot relay → mika-dev LLM inference → classify Tier 1 → respond `{"action": "allow"}`. For Read/Glob/Grep this is pure overhead — the answer is always "allow."

## Solution

Add a `tier1.ts` module that short-circuits safe operations before hitting the relay:

### Always auto-approve (tool name alone):
- `Read`, `Glob`, `Grep`

### Auto-approve with input inspection:
- `Bash` — parse `input.command`, check ALL sub-commands (split on `&&`/`||`/`;`/`|`) against safe patterns:
  - Safe git: `git status/log/diff/branch/show/commit/push/checkout/worktree` (NOT `--force`, NOT push to `main`/`master`)
  - Build/test: `cargo check/test/clippy/fmt/build`, `npm run build/dev/test`
  - Read-only shell: `ls`, `cat`, `head`, `tail`, `wc`, `find`, `mkdir`, `grep`, `sed` (NOT `sed -i`), `awk`, `tee`, `python3`
  - PR ops: `gh pr create/view/list`, `gh issue view`
- `Write`/`Edit` — auto-approve if `file_path` is within the project directory

### Never auto-approve:
- `AskUserQuestion` (always needs agent intelligence)
- Bash with Tier 3 patterns: `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`, `cargo publish`, `sed -i`, `gh label delete/edit`
- Unrecognized tools

### Deny-list checked first — if any sub-command matches a Tier 3 pattern, return `false` immediately (fail-safe).

Integration point: in `createPermissionHandler()` (permissions.ts), before building the PilotEvent payload:

```typescript
if (isTier1AutoApprove(toolName, input)) {
  logTool(toolName, summarizeInput(toolName, input), "AUTO");
  return { behavior: "allow", updatedInput: input };
}
```

## Implementation Steps

1. **Create `src/tier1.ts`** with:
   - `isTier1AutoApprove(toolName: string, input: any): boolean`
   - `isSafeBashCommand(command: string): boolean` (splits on `&&`/`||`/`;`/`|`, checks each sub-command)
   - `isSafeGitCommand(subCommand: string): boolean`
   - `isSafeBuildCommand(subCommand: string): boolean`
   - `isSafeShellCommand(subCommand: string): boolean`
   - `isTier3Dangerous(subCommand: string): boolean` (deny-list)
   - `isWithinProject(filePath: string): boolean` (for Write/Edit)

2. **Integrate into `src/permissions.ts`**:
   - Import `isTier1AutoApprove`
   - Add early return in `createPermissionHandler()` before building PilotEvent
   - Log auto-approved operations with "AUTO" tag for auditability

3. **Add comprehensive tests** (`test/tier1.test.ts`):
   - Read/Glob/Grep always auto-approved
   - Safe git commands (status, log, diff, branch, show, commit, push, checkout, worktree)
   - Build/test commands (cargo, npm)
   - Read-only shell commands
   - Write/Edit within project directory
   - Compound commands (safe && safe, safe || safe, safe ; safe, safe | safe)
   - Dangerous commands (rm -rf, git push --force, git reset --hard, DROP TABLE, cargo publish, sed -i, gh label delete/edit)
   - Mixed compounds (safe && dangerous → deny)
   - AskUserQuestion (never auto-approve)
   - Unknown tools (never auto-approve)
   - Edge cases (empty command, quoted args, sed vs sed -i, git push vs git push --force)

4. **Update documentation**:
   - Add `tier1.ts` to architecture overview in CLAUDE.md
   - Document auto-approval categories and patterns

## Acceptance Criteria

- [ ] New `tier1.ts` module with `isTier1AutoApprove()` function
- [ ] Integrated into `createPermissionHandler()` before relay
- [ ] Comprehensive tests covering: read-only tools, safe git, builds, writes in project, compound commands, dangerous commands, mixed compounds, AskUserQuestion, unknown tools, edge cases (empty command, quoted args, `sed` vs `sed -i`, `git push` vs `git push --force`)
- [ ] AUTO-approved operations logged for auditability
- [ ] No Tier 3 operations ever auto-approved (conservative — when in doubt, relay)

## Expected Impact

- ~50% reduction in relay round-trips per session
- ~430K token savings per session on mika-dev LLM
- ~80s faster claude-pilot sessions

## Companion Issues

- mika#313 — Simplify engine callback framing
- mika#314 — Inject active work items into callback turns

## Risks & Mitigations

- **Risk:** Overly permissive auto-approval allows dangerous commands.
  - **Mitigation:** Deny-list checked first, conservative patterns, when in doubt → relay.
- **Risk:** Path traversal in Write/Edit auto-approval.
  - **Mitigation:** Strict `isWithinProject()` check using `path.resolve()` and `path.relative()`.
- **Risk:** Compound command parsing misses edge cases.
  - **Mitigation:** Comprehensive test suite covering all splitting patterns.

## Dependencies

- None — pure TypeScript module, no external packages.

## Testing Strategy

- Unit tests for `tier1.ts` functions
- Integration test: run claude-pilot with a script that triggers various tool requests, verify auto-approval logs
- Manual test: run a real claude-pilot session, observe reduced relay calls

## Rollout Plan

1. Implement and test locally
2. Create PR with all changes
3. Merge to main
4. Deploy via npm publish (next release)

## Post-Deploy Monitoring & Validation

- Monitor claude-pilot logs for "AUTO" tags
- Track relay call count reduction via metrics
- Verify no dangerous commands slip through (audit logs)
