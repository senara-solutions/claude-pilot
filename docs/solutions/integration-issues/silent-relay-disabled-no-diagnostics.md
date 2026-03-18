---
title: Silent relay disabled with no diagnostic logging
category: integration-issues
date: 2026-03-18
severity: high
tags: [relay, permissions, config, diagnostics, logging, worktree]
components: [src/cli.ts, src/permissions.ts, src/ui.ts]
---

## Problem

A self-dev run via mika's claude-pilot skill auto-denied all 8 tool calls with `[denied] Bash: non-interactive mode — auto-denied`. The relay path was never entered — zero `[relay]` entries in the log. The config file (`.claude/claude-pilot.json`) existed at the worktree path and was valid JSON with correct schema. No diagnostic information existed to explain why relay was silently disabled.

The log showed only `[init]`, `[prompt]`, interleaved Claude text, and `[denied]` entries. Without knowing _why_ relay was disabled, debugging required reading source code and tracing the control flow manually.

## Root Cause

The relay decision at `permissions.ts:42` (`!opts.relay || !opts.config`) evaluates to `true` when `loadConfig()` returns `undefined`. `loadConfig` reads from `resolve(cwd, ".claude", "claude-pilot.json")` — if the `--cwd` passed to claude-pilot doesn't match where the config file lives (common with git worktrees where `.claude/` is gitignored and must be manually copied), the config silently isn't found.

The startup path had no logging between config loading and the relay-disable decision. When `loadConfig` returned `undefined`, the only observable effect was the warning message — which was captured by the parent process's stderr redirect and never surfaced to the user's log file in a useful location.

## Solution

Three changes (PR #4):

1. **`[config]` startup log** — fires unconditionally after `loadConfig()`, showing resolved CWD, config path, found/not-found, and relay enabled/disabled. This single line would have immediately revealed the root cause:
   ```
   [config] cwd=/worktree config=/worktree/.claude/claude-pilot.json [NOT FOUND] relay=disabled
   ```

2. **`--relay-config <path>` CLI flag** — explicitly specify config path, bypassing CWD-based discovery. Hard error if file not found (no silent fallback). Used by the mika `run.sh` handler as belt-and-suspenders alongside copying the config into the worktree.

3. **`[tool:request]` log** — logs every tool request (name + input summary) _before_ the decision logic runs. Previously, denied tools only showed `[denied] Bash: non-interactive mode` with no indication of what command Claude was trying to run.

4. **`[relay:send]` / `[relay:recv]` with latency** — replaces the old `[relay]` single entry. Shows round-trip timing and response action, making timeout-related failures diagnosable.

5. **Secret scrubbing** — `scrubSecrets()` redacts Bearer tokens, API key prefixes (sk-ant-, ghp_, xoxb-), and KEY=value patterns from logged tool inputs.

## Prevention

- **Always log configuration decisions at startup.** When a boolean flag silently controls a major code path (relay enabled/disabled), log the decision and the inputs that led to it. Silent configuration failures are the hardest to debug.
- **When copying gitignored files into worktrees, also pass explicit paths.** Belt-and-suspenders: copy the file AND tell the tool where to find it via a CLI flag.
- **Log tool request inputs before decisions, not just outcomes.** A wall of `[denied]` with no context is useless. The request log shows what was attempted regardless of the decision path.
- **Scrub secrets from any text that flows through logging.** Tool inputs (especially Bash commands) commonly contain tokens and API keys. Apply pattern-based redaction before logging.

## Related

- [External command stdin relay](../integration-issues/external-command-stdin-relay.md) — earlier fix for mika not receiving stdin (missing `-` flag)
- [Threading CLI options](../architecture/threading-cli-option-through-layered-architecture.md) — pattern for adding new CLI flags
- PR: https://github.com/senara-solutions/claude-pilot/pull/4
