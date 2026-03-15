# Brainstorm: claude-pilot — SDK-based Claude Code wrapper

**Date:** 2026-03-15
**Status:** Draft

## What We're Building

**claude-pilot** is a TypeScript CLI tool that wraps Claude Code using the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`). It replaces the current tmux-based relay workflow with a programmatic interception layer that guarantees every permission request and question from Claude Code is processed.

### Problem Statement

The current workflow uses Claude Code hooks → claude-asked plugin → bash relay script → mika-dev agent → tmux keypresses back to Claude Code. This is flaky because:

1. **Non-blocking flock drops events** — when mika-dev is processing one event, subsequent events are silently skipped (50+ events dropped in rapid succession observed in logs)
2. **No event queueing** — file-based locking is too simplistic for async event streams
3. **Tmux back-channel is fragile** — pane state (copy-mode, dead panes) causes silent failures
4. **Sequential bottleneck** — agent must finish before the next event can start

### Solution

The Claude Agent SDK runs Claude Code as a subprocess and provides callbacks (`canUseTool`, hooks) that **pause execution** until your code returns a decision. This means:

- Every event is guaranteed to be processed (no dropped events)
- No tmux needed — responses go back programmatically via the SDK
- No flock needed — the SDK handles sequencing
- All plugins, slash commands, and compound-engineering work (Claude Code runs headless but fully functional)
- Minimal log output provides visibility into what Claude is doing

## Why This Approach

**TypeScript SDK** was chosen over Python SDK and Rust native because:

- TypeScript SDK is the most mature — more hook events, `dontAsk` mode, no workarounds needed
- Python SDK requires a dummy `PreToolUse` hook workaround for `canUseTool` to work
- Rust native would mean reimplementing the entire agent loop and Claude Code's built-in tools
- The project is small and focused — low overhead to maintain alongside the Rust codebase

## Key Decisions

### 1. Architecture: SDK launcher replaces tmux workflow

Instead of running `claude` in a tmux session, the user runs `claude-pilot`. It starts Claude Code via the SDK's `query()` function and intercepts all events through `canUseTool` and hooks.

### 2. Generic, not mika-specific

claude-pilot is a generic wrapper. It forwards intercepted events to a **configurable external command or webhook**. Today that command is `mika --agent mika-dev ask`, but it could be anything.

### 3. Dual transport: command + webhook

Supports two ways to communicate with the external agent:
- **Command**: invoke a shell command, read structured JSON from stdout
- **Webhook**: POST to a URL, receive JSON response

Configurable per-project.

### 4. Structured JSON response format

The external agent responds with structured JSON:

```json
{"action": "allow"}
{"action": "deny", "message": "Destructive command blocked"}
{"action": "answer", "text": "Use approach A because..."}
{"action": "escalate"}
```

`escalate` means: pass the prompt through to the user for manual decision.

### 5. Relay toggle (enable/disable)

claude-pilot supports a mode where mika-dev interception is disabled — events pass straight through to the user. Replaces the current `~/.local/state/claude-relay.enabled` file mechanism.

### 6. Compound engineering plugin compatibility

The SDK runs the real Claude Code binary. All `.claude-plugin` directories, hooks, slash commands (including `/ce:plan`, `/ce:work`, `/ce:review`, and the `/mika` workflow) work as-is.

## How It Works

```
User runs: claude-pilot [prompt or slash command]
    |
    v
claude-pilot starts Claude Code via SDK query()
    |
    v
Claude Code runs headless (all plugins, no TUI)
    |
    +-- Tool needs permission ──────────────────┐
    +-- Claude asks a question (AskUserQuestion) ┤
    |                                             v
    |                                    canUseTool callback fires
    |                                             |
    |                                    claude-pilot formats event
    |                                             |
    |                          ┌──────────────────┴────────────────┐
    |                          v                                    v
    |                   Command transport                    Webhook transport
    |                   mika ask --session $id "msg"         POST /events {payload}
    |                          |                                    |
    |                          v                                    v
    |                   Agent returns JSON                   HTTP JSON response
    |                   {"action": "allow"}                  {"action": "allow"}
    |                          |                                    |
    |                          └──────────────────┬────────────────┘
    |                                             v
    |                                    claude-pilot returns
    |                                    decision to SDK
    |                                             |
    v                                             v
Claude Code continues execution ◄────────────────┘
```

## Configuration

```json
{
  "transport": "command",
  "command": "mika --agent mika-dev ask --session {{session_id}} --format json",
  "webhook_url": "http://localhost:3000/events",
  "enabled": true,
  "filter": {
    "skip_auto_approved": true,
    "skip_sub_agents": true,
    "skip_events": ["PreToolUse"]
  }
}
```

### 7. Headless with minimal log output

The SDK is headless-only — `query()` runs Claude Code as a subprocess with JSON stdio, no TUI rendering. claude-pilot will print a minimal structured log showing:
- Tool calls (name, key parameters)
- File paths being read/written
- Streaming assistant text
- Permission decisions (forwarded to agent / escalated to user)
- Cost and turn count

This gives sufficient visibility without the full TUI overhead.

## Resolved Questions

1. **TUI passthrough vs headless**: Confirmed headless-only. The SDK spawns Claude Code with `--output-format stream-json` which disables TUI rendering. Decision: use minimal log output display.

2. **Event queueing**: Not needed. The SDK pauses Claude Code execution per-event via the `canUseTool` callback — Claude Code simply waits until the callback returns. No events are dropped because there's no concurrent event stream. This is the fundamental improvement over the hooks+flock approach.

3. **Session management**: Direct passthrough. The SDK provides a `session_id` in the `system/init` message. claude-pilot passes this directly to the external agent command via `{{session_id}}` template variable. No abstraction layer needed.

## Open Questions

None — all questions resolved.
