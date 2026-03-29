---
title: "Tier 1 shell redirect bypass and safe-list over-permissiveness"
category: security-issues
date: 2026-03-29
tags: [tier1, permissions, shell-redirect, deny-list, auto-approve]
related_pr: "senara-solutions/claude-pilot#16"
related_issue: "senara-solutions/claude-pilot#14"
---

## Problem

The Tier 1 auto-approval filter (`src/tier1.ts`) had three classes of over-permissiveness discovered during PR review:

1. **Shell redirect bypass (HIGH):** Commands like `echo secret > /tmp/exfil` were auto-approved because the compound command splitter only splits on `&&`, `||`, `;`, `|` — not `>` or `>>`. The first word `echo` matched `SAFE_SHELL_COMMANDS`, so the entire command (including the redirect to an arbitrary path) was approved.

2. **`mkdir` outside project (MEDIUM):** `mkdir -p /tmp/exfil` auto-approved because shell commands don't get `isWithinProject()` checks — only `Write`/`Edit` tools do.

3. **`gh issue create` side-effect (LOW):** Creating GitHub issues is visible to others and arguably belongs in Tier 2, not Tier 1 auto-approve.

## Root Cause

The deny-list-first pattern correctly scans the full raw command string for dangerous patterns, but **output redirects (`>`, `>>`) were not in the deny-list**. This allowed any safe-listed command to write arbitrary content to arbitrary locations via shell redirection.

The broader pattern: shell commands that appear read-only can have write side-effects through redirects. This is the same class of bug that led to removing `cp`, `mv`, and `touch` from the safe list — but redirects apply to *every* command.

## Solution

1. Added `>` and `>>` output redirect pattern to `TIER3_PATTERNS` deny-list:
   ```typescript
   /(?:^|[^<])>{1,2}(?!\()/,  // output redirect > or >> (not process substitution)
   ```
   The regex avoids matching process substitution `>(...)` which has its own deny-list entry.

2. Removed `mkdir` from `SAFE_SHELL_COMMANDS`.

3. Removed `create` from `SAFE_GH_ISSUE_SUBCOMMANDS`.

## Prevention

When adding commands to safe lists, consider: **can this command have write side-effects beyond its primary purpose?** Shell redirects are the universal side-effect — any command can redirect output. The deny-list must catch redirects before safe-list matching runs. False positives (safe redirects relayed to the external agent) are cheap; false negatives (unauthorized writes) are not.
