---
title: "feat: Claude Pilot SDK Wrapper"
type: feat
status: active
date: 2026-03-15
origin: docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md
---

# feat: Claude Pilot SDK Wrapper

## Overview

**claude-pilot** is a TypeScript CLI tool that wraps Claude Code using the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`). It intercepts all permission requests and questions programmatically via the `canUseTool` callback, forwards them to an external agent via command transport, and returns the decision to the SDK — eliminating the flaky tmux+flock relay workflow entirely.

## Problem Statement / Motivation

The current workflow (Claude Code hooks -> claude-asked plugin -> bash relay -> flock -> mika-dev -> tmux keypresses) drops 50+ events in rapid succession because:

1. `flock -n` (non-blocking) gives up immediately when mika-dev is busy
2. No event queueing — lost events are gone forever
3. Tmux back-channel is fragile (pane state, copy-mode, dead panes)
4. Sequential bottleneck — agent must finish before next event starts

The Claude Agent SDK solves all of these: `canUseTool` **pauses Claude Code execution** until the callback returns. Every event is guaranteed to be processed. No tmux, no flock, no dropped events.

(see brainstorm: docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md)

## Proposed Solution

A TypeScript CLI (`claude-pilot`) that:

1. Starts Claude Code headlessly via the SDK's `query()` function
2. Intercepts permission requests and questions via `canUseTool`
3. Forwards events to an external agent via command transport (stdin JSON, stdout JSON)
4. Receives structured JSON decisions (`allow`/`deny`/`answer`)
5. Maps decisions back to the SDK's `PermissionResult` format
6. On malformed JSON: informs the agent once and retries; if still bad, falls back to user
7. Falls back to interactive user prompt when agent fails
8. Streams minimal log output showing tool calls, text, and decisions

## Technical Approach

### Architecture

```
User runs: claude-pilot "prompt"
    |
    v
CLI entry (src/cli.ts)
  - Parse args (prompt, --no-relay, --cwd, --verbose)
  - Load config from .claude-pilot.json
  - Wire up AbortController for SIGINT/SIGTERM
  - Detect TTY (process.stdin.isTTY) for non-interactive mode
    |
    v
Agent runner (src/agent.ts)
  - Call query() with canUseTool callback
  - Stream messages via includePartialMessages
  - Render log output to stderr
    |
    +-- canUseTool fires ──────────────────────┐
    |                                           v
    |                                  Permission handler (src/permissions.ts)
    |                                    - If --no-relay: escalate to user
    |                                    - If sub-agent (options.agentID): auto-allow
    |                                    - Format PilotEvent from tool call
    |                                    - Invoke command transport
    |                                    - Validate response with Zod
    |                                    - Map response to PermissionResult
    |                                    - On malformed JSON: retry once with error feedback
    |                                    - On persistent failure: fall back to user
    |                                    - If non-interactive + failure: auto-deny
    |                                           |
    v                                           v
query() continues ◄────────────────────────────┘
    |
    v
Result message → print summary, exit
```

### SDK Configuration

```typescript
query({
  prompt: userPrompt,
  options: {
    permissionMode: "default",       // canUseTool fires for non-auto-approved tools
    includePartialMessages: true,    // streaming text for log output
    cwd: targetCwd,
    abortController: controller,
    settingSources: ["user", "project", "local"],  // load .claude/settings.json
    canUseTool: async (toolName, input, options) => {
      // options.signal — AbortSignal, propagated to transport
      // options.agentID — present if from sub-agent
      // options.toolUseID — unique ID for this tool call
      // options.decisionReason — why permission was triggered
      // options.blockedPath — file path that triggered the request
      return permissionHandler(toolName, input, options);
    },
  }
});
```

### Event Payload Contract (claude-pilot -> external agent)

Sent via **stdin** as JSON to the command:

```typescript
interface PilotEvent {
  type: "permission" | "question";
  session_id?: string;             // from SDK init message, if available
  tool_name: string;               // e.g. "Bash", "Write", "AskUserQuestion"
  tool_input: Record<string, unknown>;  // full SDK tool input
  tool_use_id: string;
  decision_reason?: string;        // SDK's explanation of why permission triggered
  blocked_path?: string;           // file path that triggered the request
  cwd: string;
  timestamp: string;               // ISO 8601
}
```

### Response Contract (external agent -> claude-pilot)

Validated with Zod at runtime. The external agent is a **black box** — claude-pilot sends events and waits for responses. If the agent needs to escalate internally (e.g., ask a human via Telegram), it simply takes longer to respond. claude-pilot is unaware of and does not participate in the agent's internal decision process.

```typescript
type PilotResponse =
  | { action: "allow" }
  | { action: "deny" }
  | { action: "answer"; answers: Record<string, string> }  // for AskUserQuestion
```

### Response Mapping to SDK PermissionResult

| Agent Response | SDK PermissionResult |
|:---|:---|
| `{"action": "allow"}` | `{behavior: "allow", updatedInput: originalInput}` |
| `{"action": "deny"}` | `{behavior: "deny", message: "Denied by external agent"}` |
| `{"action": "answer", "answers": {...}}` | `{behavior: "allow", updatedInput: {questions: original.questions, answers}}` |
| Malformed JSON (1st attempt) | Retry: send error feedback to agent, invoke again |
| Malformed JSON (2nd attempt) | Fall back to interactive user prompt |
| Timeout / command failure | Fall back to interactive user prompt |
| Non-interactive + any failure | Auto-deny with log message |

### Malformed Response Retry Flow

If the external agent returns invalid JSON or a response that doesn't match the Zod schema:

1. **First failure**: claude-pilot invokes the command again, appending an error message to the event: `"error": "Previous response was malformed: <details>. Expected JSON: {action: allow|deny|answer}"`
2. **Second failure**: claude-pilot stops forwarding this event and falls back to interactive user prompt (or auto-deny if non-interactive)

This gives the agent one chance to self-correct without entering an infinite retry loop.

### AskUserQuestion Flow

When `toolName === "AskUserQuestion"`:

1. claude-pilot sends a `PilotEvent` with `type: "question"`, `tool_input` containing the full `{questions: [...]}` structure
2. External agent inspects the questions, options, and multiSelect flags
3. External agent returns `{"action": "answer", "answers": {"Question text?": "Selected label"}}` — keys are exact question text, values are option labels (comma-separated for multiSelect)
4. claude-pilot maps this into `{behavior: "allow", updatedInput: {questions: original.questions, answers: agent.answers}}`

If the agent fails, claude-pilot prints the question text and accepts freeform text input via readline.

### Interactive Fallback (User Answers from claude-pilot)

When the user needs to answer directly (agent failure, relay disabled, or malformed response after retry):

**For tool permissions:**
```
[ESCALATE] Claude wants to use: Bash
  Command: npm install express

  Allow? (y/n): y
```

**For AskUserQuestion:**
```
[QUESTION] How should I format the output?

  Your answer: Summary
```

Output goes to **stderr** (log stream). Input reads from **stdin** via `readline`.

**Non-interactive mode** (when `process.stdin.isTTY === false`, e.g. CI): Auto-deny on escalation with a log message. Never hang waiting for input.

### Command Transport

Uses `child_process.execFile` (NOT `exec`) to prevent shell injection:

```typescript
// src/transport.ts
import { execFile } from "node:child_process";

function invokeCommand(
  config: PilotConfig,
  event: PilotEvent,
  signal: AbortSignal
): Promise<PilotResponse> {
  const child = execFile(config.command, config.args ?? [], {
    timeout: config.timeout ?? 120_000,
    env: scrubEnv(process.env),
    maxBuffer: 1024 * 1024,      // 1MB
    signal,                       // propagate AbortSignal from SDK
  });

  // Write event payload to stdin
  child.stdin.write(JSON.stringify(event));
  child.stdin.end();

  // Read stdout (always capture, even on non-zero exit)
  // Validate JSON response with Zod
}
```

The external command receives the full `PilotEvent` on stdin and returns `PilotResponse` on stdout. The command has all the context it needs in the JSON payload — no template variables needed.

**Config example:**
```json
{
  "command": "mika",
  "args": ["--agent", "mika-dev", "ask"],
  "timeout": 120000
}
```

### Environment Variable Security

Before spawning external commands, scrub known sensitive env vars:

```typescript
const SCRUB_KEYS = [
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY",
  "CLAUDE_API_KEY",
];

function scrubEnv(env: NodeJS.ProcessEnv): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter(([key]) => !SCRUB_KEYS.includes(key))
  ) as Record<string, string>;
}
```

Explicit denylist is easier to audit than pattern matching. Add keys as needed.

(Institutional learning from mika: docs/solutions/security-issues/env-var-leakage-exec-handler-child-processes.md)

### Configuration File

Location: `.claude-pilot.json` in the project root (cwd).

```typescript
interface PilotConfig {
  command: string;               // executable path (e.g. "mika")
  args?: string[];               // command arguments
  timeout?: number;              // ms, default 120000 (generous for human-in-the-loop via agent)
}
```

Minimal config — 3 fields. The `--no-relay` CLI flag controls whether forwarding is enabled. The `--verbose` flag controls log verbosity.

### Log Output (Minimal Structured)

Printed to **stderr** (colored, human-readable):

```
[init] Session abc123, model claude-opus-4-6
[tool] Read src/lib.rs (auto-approved)
[tool] Bash: cargo test → forwarded → ALLOW
[text] Running the test suite to verify...
[tool] Edit src/main.rs → forwarded → ALLOW
[question] "Which approach?" → forwarded → "Option A"
[tool] Bash: rm -rf target → forwarded → DENY
[denied] Bash: rm -rf target
[done] Success | 12 turns | $0.42 | 45s
```

## System-Wide Impact

- **Interaction graph**: claude-pilot sits between the user and Claude Code. All tool calls flow through `canUseTool`. The external agent (mika-dev) will need a new skill or updated skill to output structured JSON instead of tmux keypresses.
- **Error propagation**: External agent failures (timeout, crash, bad JSON) fall back to interactive user prompt (or auto-deny in non-interactive mode) — never silently approve.
- **State lifecycle risks**: Session IDs are passed through transparently. No state is persisted by claude-pilot itself (SDK handles session persistence).
- **Concurrent canUseTool calls**: Sub-agent tool calls are auto-allowed (hardcoded). The command transport only handles one call at a time from the main agent.

## Implementation Phases

### Phase 1: Project Setup & Core Loop

**Goal:** TypeScript project that runs Claude Code via SDK and streams output.

**Tasks:**
- [x] Initialize npm project with ESM (`"type": "module"`)
- [x] Install dependencies: `@anthropic-ai/claude-agent-sdk`, `zod`, `tsx` (dev), `tsup` (dev)
- [x] Set up `tsup.config.ts` for building
- [x] Set up `tsconfig.json` (ES2024, Node16 module resolution)
- [x] Create `src/cli.ts` — entry point with arg parsing (`process.argv`), config loading, AbortController wiring, TTY detection
- [x] Create `src/agent.ts` — `query()` wrapper with `includePartialMessages: true`, async iteration over `SDKMessage` stream
- [x] Create `src/ui.ts` — minimal log renderer (stderr, colored via ANSI codes)
- [x] Create `src/types.ts` — `PilotEvent`, `PilotResponse` (with Zod schema), `PilotConfig`
- [x] Add `.gitignore` (node_modules, dist)
- [x] Verify: run `tsx src/cli.ts "hello"` and see streaming output + result + graceful shutdown

**Files:** `package.json`, `tsconfig.json`, `tsup.config.ts`, `.gitignore`, `src/cli.ts`, `src/agent.ts`, `src/ui.ts`, `src/types.ts`

**Success criteria:** Can run Claude Code headlessly, see log output, SIGINT shutdown works.

### Phase 2: Permission Interception & Transport

**Goal:** `canUseTool` intercepts events, forwards to external command, maps responses.

**Tasks:**
- [x] Create `src/permissions.ts` — `canUseTool` implementation with:
  - Relay enabled/disabled check
  - Sub-agent auto-allow (`options.agentID` present → allow)
  - PilotEvent formatting (include `decision_reason`, `blocked_path` from SDK options)
  - AbortSignal propagation to transport
  - Response validation with Zod
  - Response mapping to `PermissionResult`
  - AskUserQuestion special handling (map answers to `updatedInput` format)
  - Interactive readline fallback (user answers from claude-pilot)
  - Non-interactive auto-deny fallback (when not TTY)
  - Malformed JSON: retry once with error feedback, then user fallback
  - Transport failures (timeout, crash): user fallback
- [x] Create `src/transport.ts` — `execFile` with stdin payload, stdout response, env scrubbing
- [x] Test with mock command (e.g. `echo '{"action":"allow"}'`)
- [ ] Test AskUserQuestion round-trip
- [ ] Test error scenarios: timeout, bad JSON, non-zero exit → fallback

**Files:** `src/permissions.ts`, `src/transport.ts`

**Success criteria:** Permission events intercepted and forwarded. All response types map correctly. Errors fall back gracefully. Non-interactive mode works.

### Phase 3: Integration & Polish

**Goal:** End-to-end workflow with mika-dev, documentation, release.

**Tasks:**
- [ ] Update mika-dev skill system prompt to output structured JSON responses (`{action: allow|deny|answer}`) when receiving claude-pilot events (replaces claude-tmux-relay tmux keypresses)
- [x] Add `bin` field to `package.json` for global install (`npm link`)
- [x] Create `.claude-pilot.json` example config
- [ ] Test full workflow: `claude-pilot "/mika #42"` → mika-dev handles permissions → PR created
- [ ] Test relay disable: `claude-pilot --no-relay "fix the bug"` → all events go to user
- [ ] Test error scenarios: agent timeout, crash, bad JSON → interactive fallback
- [ ] Test compound engineering slash commands work (`/ce:plan`, `/ce:work`, `/mika`)

**Files:** (mika-dev skill update), `.claude-pilot.json.example`

**Success criteria:** Full end-to-end workflow works. Compound engineering slash commands work. Relay toggle works. No dropped events.

## Acceptance Criteria

### Functional Requirements

- [ ] `claude-pilot "prompt"` starts Claude Code headlessly and streams log output to stderr
- [ ] All permission requests and questions are intercepted via `canUseTool`
- [ ] Events are forwarded to configured external command via stdin JSON
- [ ] External agent responses validated with Zod before use
- [ ] `AskUserQuestion` round-trips through external agent with correct answer mapping
- [ ] Malformed JSON triggers one retry with error feedback, then falls back to user
- [ ] Agent failures fall back to interactive readline prompt (user answers from claude-pilot)
- [ ] Non-interactive mode (no TTY) auto-denies on failure
- [ ] `--no-relay` flag disables forwarding (all events go to user)
- [ ] Compound engineering plugin slash commands work (`/ce:plan`, `/ce:work`, `/mika`, etc.)
- [ ] SIGINT/SIGTERM triggers graceful shutdown (AbortSignal propagated to transport)
- [ ] Session IDs passed through to external agent when available
- [ ] Sub-agent tool calls auto-allowed (not forwarded)

### Non-Functional Requirements

- [ ] No shell injection via command transport (`execFile`, not `exec`)
- [ ] Sensitive env vars scrubbed before spawning external commands (explicit denylist)
- [ ] Transport timeout configurable (default 30s)
- [ ] Log output to stderr, stdin/stdout clean for interaction

## Dependencies & Risks

| Dependency | Risk | Mitigation |
|:---|:---|:---|
| `@anthropic-ai/claude-agent-sdk` is relatively new | API may change | Pin version, monitor changelog |
| mika-dev skill must output valid JSON | Skill guidance via system prompt, not CLI flag | Malformed JSON retry with error feedback, then user fallback |
| SDK is headless-only (no TUI) | User loses familiar Claude Code interface | Minimal log output provides sufficient visibility |
| External agent may be slow (agent escalates to human via Telegram) | SDK pauses execution while waiting | Generous default timeout (120s), configurable |
| Compound engineering plugin compatibility | Untested with SDK headless mode | Test early in Phase 1; plugins loaded by Claude Code internally |

## Future Enhancements (v2)

- **Webhook transport**: POST events to a URL, receive JSON response. Add when there's a consumer.
- **`updated_input` in allow response**: Let external agent modify tool inputs before execution.
- **`updatedPermissions` in allow response**: Let external agent persist permission rules ("remember this decision").
- **Multi-turn conversational mode**: Support follow-up prompts via V2 SDK `send()`/`stream()` API.
- **Log levels**: Add `--quiet` flag for suppressed output.

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md](docs/brainstorms/2026-03-15-claude-pilot-brainstorm.md) — Key decisions: TypeScript SDK, headless with minimal log output, command transport, structured JSON responses, generic wrapper.

### SDK Documentation

- [Agent SDK Reference - TypeScript](https://platform.claude.com/docs/en/agent-sdk/typescript) — full API reference, `canUseTool` signature, `PermissionResult` type
- [Handle Approvals and User Input](https://platform.claude.com/docs/en/agent-sdk/user-input) — `canUseTool` and `AskUserQuestion`
- [Configure Permissions](https://platform.claude.com/docs/en/agent-sdk/permissions) — permission modes and evaluation order
- [Stream Responses](https://platform.claude.com/docs/en/agent-sdk/streaming-output) — `includePartialMessages`
- [Work with Sessions](https://platform.claude.com/docs/en/agent-sdk/sessions) — session resumption

### Institutional Learnings

- `mika/docs/solutions/security-issues/env-var-leakage-exec-handler-child-processes.md` — env var scrubbing pattern
- `mika/docs/solutions/logic-errors/exec-handler-stdout-discarded-on-nonzero-exit.md` — always capture stdout before checking exit code
- `claude-asked/docs/solutions/integration-issues/nodejs-hook-plugin-pitfalls.md` — stdin error handlers, double-resolve guards
- `claude-asked/docs/solutions/logic-errors/hook-event-scope-configuration.md` — which events to intercept
- `mika/docs/solutions/architecture/callback-resume-agent-lifecycle.md` — treat external responses as untrusted input
