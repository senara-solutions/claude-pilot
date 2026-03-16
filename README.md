# claude-pilot

A TypeScript CLI that runs Claude Code headlessly and forwards every permission request and question to an external agent for automated decision-making.

## Why this exists

The old workflow was painful: Claude Code running in a tmux session, a bash relay script using `flock` to serialize events, keystrokes injected back via `tmux send-keys`. It worked — until it didn't. Events dropped silently when the agent was busy (50+ lost in rapid succession). Panes died or entered copy-mode at the worst times. There was no queueing, no retry, no guarantee that anything got through.

The [Claude Agent SDK](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk) changes the game. Its `canUseTool` callback **pauses Claude Code's execution** until your code returns a decision. No events dropped, no tmux, no flock. Claude just waits.

## What it does

claude-pilot starts Claude Code via the SDK's `query()` function, intercepts tool permission requests and questions through `canUseTool`, packages them as structured JSON, and sends them to a configurable external command. The external agent (whatever it is) makes the decision, returns JSON, and Claude Code continues. If the agent fails, claude-pilot retries once with error feedback, then falls back to asking the user interactively.

## How it works

```
claude-pilot "fix the login bug"
    │
    ▼
SDK query() starts Claude Code headlessly
    │
    ├── Tool needs permission ─────────────┐
    ├── Claude asks a question ─────────────┤
    │                                       ▼
    │                              canUseTool callback
    │                                       │
    │                              Format PilotEvent JSON
    │                                       │
    │                              execFile → external agent
    │                              (event on stdin, response on stdout)
    │                                       │
    │                              Validate response (Zod)
    │                                       │
    │                              Map to PermissionResult
    │                                       │
    ▼                                       ▼
Claude Code continues ◄────────────────────┘
```

The external agent is a black box. claude-pilot sends an event and waits. If the agent internally escalates to a human via Telegram and takes two minutes — fine. claude-pilot doesn't know or care.

## Quick start

```bash
# Install dependencies
npm install

# Run directly (development)
npm run dev -- "your prompt here"

# Build and run
npm run build
npm start -- "your prompt here"
```

Requires Node.js >= 22 and Claude Code installed.

## Configuration

Create `claude-pilot.json` in the target project's `.claude/` directory:

```json
// .claude/claude-pilot.json
{
  "command": "mika",
  "args": ["--agent", "mika-dev", "ask"],
  "timeout": 120000
}
```

| Field     | Type       | Default  | Description                              |
|-----------|------------|----------|------------------------------------------|
| `command` | `string`   | required | The command to invoke                    |
| `args`    | `string[]` | `[]`     | Arguments passed to the command          |
| `timeout` | `number`   | `120000` | Max wait time per invocation (ms, 1s–10m)|

If no config file is found, claude-pilot runs in no-relay mode (all prompts go to the user).

## CLI options

```
claude-pilot [options] <prompt>

  --task-id <id>  Task identifier for external agent tracking
  --no-relay      Disable agent forwarding, answer all prompts locally
  --cwd <dir>     Working directory for Claude Code (default: current)
  --verbose       Show debug output
  --help          Show help
```

## Response contract

The external agent receives a `PilotEvent` on stdin and must write one of these JSON responses to stdout:

### Allow a tool

```json
{"action": "allow"}
```

### Deny a tool

```json
{"action": "deny", "message": "Reason for denial"}
```

### Answer a question

```json
{"action": "answer", "answers": {"What branch?": "main"}}
```

The `answers` object maps question text to answer text.

### Event payload

The agent receives this on stdin:

```json
{
  "type": "permission",
  "session_id": "abc123",
  "task_id": "task-456",
  "tool_name": "Bash",
  "tool_input": {"command": "rm -rf /tmp/build"},
  "tool_use_id": "toolu_xyz",
  "decision_reason": "tool not in allowlist",
  "blocked_path": "/tmp/build",
  "cwd": "/home/user/project",
  "timestamp": "2026-03-15T10:30:00.000Z"
}
```

`type` is `"permission"` for tool calls, `"question"` for `AskUserQuestion` events.

## Error handling

1. **Malformed response** — claude-pilot retries once, sending the original event again with an `error` field describing what went wrong
2. **Second failure** — falls back to interactive prompt (if TTY is available)
3. **Non-interactive mode** (no TTY) — auto-denies on fallback
4. **Abort** — SIGINT/SIGTERM triggers graceful shutdown via AbortController

## Architecture

```
src/cli.ts          Entry point: arg parsing, config loading, signal handling
src/agent.ts        SDK query() wrapper, message stream iteration, result output
src/permissions.ts  canUseTool handler: relay → retry → interactive fallback
src/transport.ts    execFile command transport with Zod validation
src/ui.ts           Stderr log renderer (ANSI colors)
src/types.ts        PilotEvent, PilotResponse (Zod schemas), PilotConfig
```

## Security

- **Environment scrubbing** — env vars matching `KEY$`, `SECRET`, `TOKEN$`, `PASSWORD`, `CREDENTIAL` are stripped before spawning the external command
- **execFile over exec** — prevents shell injection; arguments are passed as an array, never interpolated into a shell string
- **Sub-agent auto-allow** — tool calls from Claude Code's own sub-agents are allowed automatically and never forwarded to the external agent
