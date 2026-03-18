# Fix Relay Approvals & Improve Monitoring

**Date:** 2026-03-18
**Status:** Ready for planning

## What We're Building

Two fixes to claude-pilot after a failed self-dev run where all 8 tool calls were auto-denied with "non-interactive mode" despite a valid `.claude/claude-pilot.json` config being present.

### Problem 1: Relay silently disabled

The log shows zero `[relay]` entries — the relay path in `permissions.ts:42` was never entered. The condition `!opts.relay || !opts.config` evaluated to `true`, causing all tools to fall through to `interactiveFallback` → auto-deny (no TTY). There's no diagnostic logging to explain WHY relay was disabled — was the config not found? Was the CWD wrong? Was there a parse error? We're blind.

### Problem 2: Monitoring too sparse

When tools get denied, the log only shows `[denied] Bash: non-interactive mode — auto-denied`. No tool inputs, no context about what Claude was attempting. The human-readable text stream is interleaved but doesn't show Read/Glob/Grep operations. You can't tell what's happening.

## Why This Approach

Diagnostic-first: add startup logging, explicit config flag, and richer tool request logging. Minimal changes, solves the immediate problem, no over-engineering.

## Key Decisions

1. **Add `[config]` startup log** — show resolved CWD, config path, found/not-found, relay enabled/disabled, and the reason. This would have immediately revealed the root cause.

2. **Add `--relay-config <path>` CLI flag** — explicitly pass config path, bypassing `resolve(cwd, ".claude", "claude-pilot.json")` path resolution. Useful when the CWD and config location differ (e.g., worktrees where `.claude/` was manually copied).

3. **Log all tool requests with input summary** — every `canUseTool` callback logs `[tool:request] Bash: cargo test` BEFORE the decision logic runs. Currently only the decision outcome is logged (`[denied]`, `[tool]`). Adding the request log means you always see what Claude is attempting, regardless of the decision path taken.

4. **Log relay round-trips** — `[relay:send]` when forwarding to mika, `[relay:recv]` when response arrives (with response action). Currently only `[relay]` (forwarded) is logged, not the response.

5. **Log fallback reason** — when relay fails and falls to interactive, log why (`[fallback] TransportError: Command produced no output`). This already exists in `logFallback` but add the specific error context.

## Scope

### Files to change

| File | Change |
|------|--------|
| `src/cli.ts` | Add `--relay-config` flag, log `[config]` entry after loadConfig |
| `src/permissions.ts` | Log `[tool:request]` on every canUseTool entry. Log relay send/recv. |
| `src/transport.ts` | No changes needed (already has verbose logging) |
| `src/ui.ts` | Add `logConfig()`, `logToolRequest()`, `logRelayRecv()` functions |
| `README.md` | Document `--relay-config` flag |

### Not in scope

- JSON Lines structured log (approach B) — revisit if diagnostic logging proves insufficient
- Webhook/callback monitoring (approach C) — too complex for now
- Changes to mika's run.sh or self-dev skill — those are separate

## Open Questions

None — approach is clear.
