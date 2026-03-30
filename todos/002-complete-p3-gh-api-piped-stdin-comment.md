---
status: completed
priority: p3
issue_id: "18"
tags: [code-review, security, documentation]
dependencies: []
---

# Document gh api piped stdin edge case

## Problem Statement

The `gh api` auto-approve check blocks `-X`, `--method`, `-f`, `-F`, `--field`, `--raw-field`, and `--input`. However, `gh api` can receive a request body via piped stdin (e.g., `echo '{"body":"..."}' | gh api repos/owner/repo/issues`). The compound splitter splits on `|`, so each side is evaluated independently — `echo '...'` is safe-listed and `gh api repos/.../issues` passes with no mutation flags.

## Findings

- **Source**: security-sentinel (Medium severity), agent-native-reviewer (Warning)
- **Location**: `src/tier1.ts` lines 252-255
- Practical risk is LOW because: (1) Claude Code typically doesn't pipe to gh api, (2) the relay agent would normally generate such commands, (3) both sides must independently pass safe checks
- The trailing `-` flag (explicit stdin read) is also not caught

## Proposed Solutions

### Option 1: Add a code comment documenting the known gap (Recommended)
Add a comment in `isSafeGhCommand` noting the piped stdin limitation.
- **Pros**: Documents the gap for future contributors, zero code risk
- **Cons**: Doesn't fix the gap
- **Effort**: Small
- **Risk**: None

### Option 2: Block gh api after pipe operator
Would require passing positional context from `splitCompoundCommand`.
- **Pros**: Closes the gap
- **Cons**: Increases complexity, changes the splitting architecture
- **Effort**: Medium
- **Risk**: Could over-block legitimate read-only pipes

## Acceptance Criteria

- [ ] Comment added to `isSafeGhCommand` documenting the piped stdin limitation

## Work Log

| Date | Action | Learnings |
|------|--------|-----------|
| 2026-03-30 | Created from ce:review | Security and agent-native reviewers flagged edge case |
