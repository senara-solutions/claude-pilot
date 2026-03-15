---
title: "Replacing tmux+flock relay with Claude Agent SDK wrapper"
category: architecture
date: 2026-03-15
tags: [claude-agent-sdk, typescript, subprocess, permission-interception, tmux]
components: [claude-pilot, permissions, transport]
---

## Problem

The previous workflow for automating Claude Code permission handling used a multi-hop chain: Claude Code hooks → claude-asked plugin → bash relay script (flock-based) → mika-dev agent → tmux keypresses back to Claude Code. This dropped 50+ events in rapid succession because `flock -n` (non-blocking) silently skipped events when the agent was busy, and tmux pane state (copy-mode, dead panes) caused silent failures.

## Root Cause

The hook-based architecture is fundamentally fire-and-forget — hooks cannot return decisions to Claude Code. The tmux back-channel was a workaround that introduced two failure modes: (1) flock concurrency control drops events instead of queueing them, and (2) tmux pane state is fragile and unrecoverable without detection.

## Solution

Replace the entire chain with the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`), which runs Claude Code as a subprocess and provides a `canUseTool` callback that **pauses execution** until a decision is returned. Key implementation details:

- **`canUseTool` callback** intercepts all permission requests and questions. The SDK guarantees every event is processed (no dropped events).
- **External command transport** via `execFile` (not `exec`) sends a `PilotEvent` JSON on stdin and reads a `PilotResponse` JSON from stdout. No shell injection risk.
- **Zod validation** of external agent responses with a discriminated union schema (`allow | deny | answer`).
- **Malformed JSON retry**: if the external agent returns bad JSON, claude-pilot sends the event again with an `error` field explaining what went wrong. Only one retry — then falls back to interactive user prompt.
- **AbortSignal propagation**: SIGINT/SIGTERM aborts the SDK query and kills child processes cleanly.
- **Non-interactive auto-deny**: when `process.stdin.isTTY === false`, escalations are auto-denied instead of hanging.
- **Structured JSON result on stdout**: `{"status":"success","turns":12,"cost_usd":0.42}` for agent consumption. Exit code 1 on SDK error.

The external agent (mika-dev) is treated as a black box — claude-pilot sends events and waits. If the agent escalates internally (e.g., asks a human via Telegram), it just takes longer. claude-pilot is unaware of and does not participate in the agent's internal process.

## Key Decisions

1. **TypeScript SDK over Python**: More mature — more hook events, `dontAsk` mode, no workarounds needed for `canUseTool`.
2. **Command transport only for v1**: Webhook deferred. stdin/stdout JSON is sufficient and simpler.
3. **No template variables in command args**: The full event payload is on stdin — the external agent has everything it needs in the JSON.
4. **Pattern-based env scrubbing**: `/KEY$/i, /SECRET/i, /TOKEN$/i, /PASSWORD/i, /CREDENTIAL/i` instead of a hardcoded list. Catches new secrets automatically.
5. **Generous timeout (120s default)**: Accounts for human-in-the-loop via the external agent.

## Prevention

- When building event relay systems, prefer synchronous callbacks (like `canUseTool`) over fire-and-forget hooks + back-channels. The callback model eliminates entire categories of race conditions and dropped events.
- When spawning child processes, always use `execFile` with argument arrays — never `exec` with string interpolation.
- When accepting responses from external processes, validate with a schema (Zod) before trusting. Treat all external data as untrusted.
- When scrubbing env vars for child processes, use pattern matching rather than explicit key lists to be future-proof.
