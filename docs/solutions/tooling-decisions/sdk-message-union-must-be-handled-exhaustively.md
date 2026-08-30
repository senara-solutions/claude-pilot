---
title: "Handle the claude-agent-sdk Message union exhaustively — a dropped member is an invisible failure path"
date: 2026-08-30
last_updated: 2026-08-30
module: claude_pilot.agent
component: message-loop
problem_type: tooling_decision
category: tooling-decisions
tags: [claude-agent-sdk, stream-event, include-partial-messages, guardrails, idle-timeout, silent-failure, cpp-123, mika-2029]
applies_when: "adding or reviewing an isinstance branch in the SDK message loop, changing ClaudeAgentOptions streaming flags, or diagnosing a headless session that dies at exactly the idle-timeout threshold"
---

# Handle the SDK `Message` union exhaustively — a dropped member is an invisible failure path

## Context

cpp#123 / mika#2029: every headless pilot session was dying at `idle_timeout` having made zero tool calls. Six hypotheses were eliminated with hard evidence first — Anthropic 401/auth, egress transport, restricted tool surface, a hanging permission relay, the entry skill failing to load, and the system-prompt preset being clobbered. All six were wrong.

The actual cause was three lines that did not exist. `agent.py` set `include_partial_messages=True`, so the SDK delivered a `StreamEvent` per raw Anthropic SSE event. The message loop branched on four of the union's six members. `StreamEvent` matched nothing, reached the bottom of the loop body, and was discarded — no log line, no counter, no comment.

Because nothing rearmed the idle timer inside a turn, any turn whose generation ran past 300 s was killed while the model was still producing. The measured signature: `duration ~= turns x 301 s`, holding across 1-, 2-, and 3-turn sessions. Every terminal turn burned the entire budget.

## Guidance

### 1. `Message` is a six-member union — enumerate it, don't pattern-match the ones you remember

```python
Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent
```

Before adding or reviewing a branch, check the union in the installed SDK:

```bash
SP=$(ls -d ~/.local/share/uv/tools/claude-pilot/lib/python*/site-packages | head -1)
grep -n "^Message = " -A 10 "$SP/claude_agent_sdk/types.py"
```

A member with no branch is not "ignored" — it is *unobservable*. `grep -rn StreamEvent src/` returning nothing was the whole bug, visible in one command, for weeks.

### 2. Give the loop a terminal branch — the fix for the class, not the instance

Every branch ends in `continue`, so anything reaching the bottom matched nothing. Name it once per type per session:

```python
type_name = type(message).__name__
if type_name not in unhandled_message_types:
    unhandled_message_types.add(type_name)
    log_unhandled_message(type_name)
```

Bounded (one line per type), and the next union member the SDK adds is visible on its first occurrence instead of costing another round of diagnosis. Note the ordering trap: the `ResultMessage` branch had no `continue` and fell through to the bottom by design, so adding the terminal branch required giving it one.

### 3. A liveness detector must consume the stream that proves liveness

`guardrails.py` claimed "idle timeout is reserved for 'nothing at all' from the SDK." It actually tested "no new turn boundary" — the timer was rearmed only on a `message_id` change, which does not arrive until a turn **ends**. With partial messages enabled, those are different predicates, and the weaker one kills working sessions.

When a comment states an invariant, check that the code's inputs can express it. Here the inputs (turn boundaries) could not distinguish a producing turn from a dead one, so the invariant was unprovable regardless of threshold.

### 4. Rearm by moving a deadline, not by recreating the timer task

A turn emits thousands of deltas. Cancelling and recreating an `asyncio.Task` per delta is task churn. Track a monotonic timestamp and let the watchdog recompute on wake:

```python
async def _idle_watchdog(self) -> None:
    timeout = self._config.idleTimeoutMs / 1000.0
    loop = asyncio.get_running_loop()
    while True:
        remaining = self._last_activity_at + timeout - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(remaining)
```

O(1) per event, one task per arming, and `pause_idle_timer()` / `resume_idle_timer()` keep working unchanged.

### 5. Allow-list what counts as progress; never rearm on a keepalive

`ping` proves the socket is open, not that the model is producing. Rearming on it makes the guardrail inert for as long as the connection lives — a worse failure than the one being fixed. Use an allow-list (`content_block_delta`, `content_block_start`/`stop`, `message_start`/`delta`/`stop`), not an exclusion of `ping`, so a future keepalive under a new name cannot disarm the detector. Guideline 2 keeps the allow-list's blind spot observable.

### 6. Raising the threshold is not the fix

`idleTimeoutMs` was never the problem; the rearm predicate was. Raising it moves the wall and lengthens every genuine hang by the same amount. When a timeout fires "too early," first ask what it measures — not what it is set to.

### 7. Put the count in the line that already gets read

The abort detail now reads `No meaningful progress for 300s (0 content stream events this session)`. A silent session and a producing one used to render identically. One number turns the next occurrence of this class into a one-line diagnosis instead of six rounds.

## Diagnostic shortcut

A session dying at *exactly* the threshold, repeatedly, is a timer artifact — not a hang. Check whether duration scales with turn count:

```bash
sqlite3 ~/.mika/data/mika.db \
  "SELECT substr(result, instr(result,'Turns:'), 12), substr(result, instr(result,'Duration:'), 20)
   FROM tasks WHERE result LIKE '%claude-pilot completed%' ORDER BY rowid DESC LIMIT 25;"
```

`duration ~= turns x threshold` means each turn is being cut off at the budget. A genuine hang does not scale with turn count — that single test separates "the timer is killing work" from "the SDK stopped delivering," and it costs nothing.
