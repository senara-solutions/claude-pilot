---
status: pending
priority: p3
issue_id: "18"
tags: [code-review, security]
dependencies: []
---

# Add find -execdir to secondary guard clause

## Problem Statement

The secondary guard clause for `find` in `isSafeShellCommand` blocks `-exec` and `-delete` but not `-execdir`, which also executes arbitrary commands.

## Findings

- **Source**: agent-native-reviewer (Warning)
- **Location**: `src/tier1.ts` line 222
- The deny-list pattern at line 73 (`/\bfind\s.*-(exec|delete)\b/`) would catch `-execdir` because it contains `exec` as a substring. However, the secondary guard at line 222 uses `-(exec|delete)` which requires an exact match and would NOT catch `-execdir`.
- This is defense-in-depth — the deny-list catches it first. But the secondary guard should be consistent.

## Proposed Solutions

### Option 1: Expand secondary guard regex
```typescript
if (cmd === "find" && /-(exec|execdir|delete)\b/.test(sub)) return false;
```
- **Effort**: Small
- **Risk**: None

## Acceptance Criteria

- [ ] `find -execdir` blocked in secondary guard clause
- [ ] Test added for `find -execdir` case

## Work Log

| Date | Action | Learnings |
|------|--------|-----------|
| 2026-03-30 | Created from ce:review | Agent-native reviewer flagged missing guard |
