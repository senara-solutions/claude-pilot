# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**claude-pilot** is a TypeScript CLI that wraps Claude Code using the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`). It runs Claude Code headlessly and intercepts permission requests and questions via the SDK's `canUseTool` callback, forwarding them to an external agent (e.g., `mika --agent mika-dev ask`) for automated decision-making.

## Commands

```bash
npm run dev -- "prompt"        # Run directly with tsx (development)
npm run build                  # Build with tsup → dist/cli.js
npm start -- "prompt"          # Run built version
npx tsc --noEmit               # Type-check without building
```

Usage: `claude-pilot [options] <prompt>`
- `--no-relay` — Disable agent forwarding, answer all prompts locally
- `--cwd <dir>` — Working directory for Claude Code
- `--verbose` — Show debug output

## Architecture

```
src/cli.ts          → Entry point: arg parsing, config loading, signal handling
src/agent.ts        → SDK query() wrapper, message stream iteration, log rendering
src/permissions.ts  → canUseTool handler: relay, retry, interactive fallback
src/transport.ts    → execFile command transport with Zod validation
src/ui.ts           → Stderr log renderer (ANSI colors)
src/types.ts        → PilotEvent, PilotResponse (Zod schema), PilotConfig
```

**Flow**: CLI → `query()` with `canUseTool` callback → on tool permission needed → format `PilotEvent` → invoke external command via `execFile` (stdin JSON) → validate response with Zod → map to SDK `PermissionResult` → return to SDK.

**Key design decisions**:
- External agent is a **black box** — claude-pilot sends events and waits. If the agent escalates internally (e.g., asks a human via Telegram), it just takes longer to respond. claude-pilot is unaware.
- Response contract is minimal: `{action: "allow"}`, `{action: "deny"}`, `{action: "answer", answers: {...}}`
- Malformed JSON from the agent triggers one retry with error feedback, then falls back to interactive user prompt
- Sub-agent tool calls are auto-allowed (not forwarded)
- Non-interactive mode (no TTY) auto-denies on failure
- `execFile` (not `exec`) prevents shell injection
- Sensitive env vars (`ANTHROPIC_API_KEY`, etc.) are scrubbed before spawning commands

## Configuration

Place `.claude-pilot.json` in the project root:
```json
{
  "command": "mika",
  "args": ["--agent", "mika-dev", "ask"],
  "timeout": 120000
}
```

## Key SDK Types

- `canUseTool(toolName, input, options)` — options includes `signal` (AbortSignal), `agentID` (sub-agent), `toolUseID`, `decisionReason`, `blockedPath`
- `PermissionResult` — `{behavior: "allow", updatedInput}` or `{behavior: "deny", message}`
- `AskUserQuestion` — intercepted via `canUseTool` when `toolName === "AskUserQuestion"`, response requires `{behavior: "allow", updatedInput: {questions, answers}}`

## Planning Documents

- Brainstorm: `docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md`
- Plan: `docs/plans/2026-03-15-001-feat-claude-pilot-sdk-wrapper-plan.md`
