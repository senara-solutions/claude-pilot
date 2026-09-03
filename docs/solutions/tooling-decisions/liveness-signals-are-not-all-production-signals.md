---
title: "Separate liveness from production when rearming the idle watchdog — a tool result is not a generation delta"
date: 2026-08-30
last_updated: 2026-09-03
module: claude_pilot.guardrails
component: idle-watchdog
problem_type: tooling_decision
category: tooling-decisions
tags: [claude-agent-sdk, stream-event, user-message, guardrails, idle-timeout, liveness, cpp-125, cpp-123, cpp-145, mika-2029]
applies_when: "adding an isinstance branch for a new SDK Message union member, or wiring a message type to the idle watchdog"
---

# Separate liveness from production when rearming the idle watchdog

## Context

cpp#123 closed the `StreamEvent` fall-through that caused mika#2029 (see the companion doc on handling the `Message` union exhaustively). cpp#125 is its follow-on: two of the six union members still had no *explicit* branch. `StreamEvent` was handled but silent (it only moved the deadline), and `UserMessage` lived in a silent-ignore set. "Handled" and "observable" are not the same thing — a branch that logs nothing is still a diagnosis cost the next time the loop misbehaves.

## Guidance

### 1. Every union member deserves an explicit branch with a debug trace

Membership in a `_KNOWN_IGNORED_MESSAGE_TYPES` set keeps a message *quiet*, but it also keeps it *invisible* — you cannot tell from a verbose log whether the member arrived. Give it a real `isinstance` branch and a `verbose`-gated debug line. The gate matters: a turn emits thousands of `StreamEvent` deltas, so an ungated log floods. `verbose` is this codebase's debug level (see `transport.py`), so the default path stays flood-free while the operator can still ask to see the traffic.

### 2. Not all inbound traffic is model production

The idle watchdog measures *silence*, so it should be rearmed by any proof the session is alive. But rearm signals are not interchangeable:

- `note_stream_activity()` — a generation delta. Counts toward the "content stream events" total reported in the abort detail, and clears the sticky rate-limit flag (content on the wire means a throttled retry succeeded).
- `note_activity()` — a `UserMessage` tool result. Inbound liveness, so it rearms the deadline, but it is **not** model production: it must not inflate the content-stream counter, and it must not clear the rate-limit flag (a tool result is no evidence a throttled *generation* retry landed).

Collapsing the two would make the abort detail lie about how much the model produced, and could mask a genuine throttle as recovered. Both share one private `_bump_idle_deadline()` so the O(1) deadline-move stays in one place.

**cpp#145 added a third responsibility, and it is where the sharp edge now lives.** Each signal also says *who is being waited on* — so the two functions differ in accounting AND in state, not accounting alone:

- `note_activity()` (a tool result) retires that many outstanding tools and, once none remain, OPENS the model-wait window: the next turn's first token has not arrived yet.
- `note_stream_activity(event_type)` CLOSES the model-wait window — but only for a genuine production event. It never retires an outstanding tool.

### 2b. "Progress" and "the model resumed" are different claims

`_PROGRESS_STREAM_EVENT_TYPES` in `agent.py` includes `message_delta` and `message_stop`. That is correct for rearming a deadline: they are traffic, so the session is alive. It is **wrong** as proof that the model is producing — `message_stop` means the turn ENDED.

This is not a hypothetical. In three of the six sessions cpp#145 was written for (`3d5fe1ec`, `f26add11`, `e2f0ef97`), the SDK delivers those trailers *after* the tool result. A rule of "any progress event means the model resumed" therefore closes the model-wait window at the exact instant it should open, and those three sessions still die at 300s. `guardrails._PRODUCTION_STREAM_EVENTS` is the narrower set that answers the state question; the wider set still answers the deadline question.

### 2c. Overlapping concerns must be counted, not stated

Measured over 177 real dispatch-to-result pairs in `/var/log/claude-pilot`, **67 genuine production events arrive while a tool is still outstanding**. Generation and tool execution overlap on the wire. So a tool wait held as a single state scalar gets cleared mid-flight by ordinary generation, and its ceiling becomes dead configuration — a knob that reads as protection and provides none.

The general rule: when two things can be true at once, a scalar state will silently pick one. Count what can overlap (`_pending_tool_uses`), and derive the state from the counts.

### 2d. Any wait needs a ceiling

An exemption ("while waiting, the counter does not run") makes a session that never resumes immortal — the zombie `rateLimitCeilingMs` (cpp#133) exists to prevent. Give every wait its own budget and its own abort reason, so a guardrail that stops killing is impossible to build by accident and a death always names who was waited on.

### 3. Prove the knob, then prove it is not vacuous

A test that "a streaming turn is not killed" is worthless without its dual — "a genuinely silent session still fires `idle_timeout`". Keep both, at both levels: the guardrail unit (deadline arithmetic) and the agent loop end-to-end (the watchdog racing the message stream through `_merge_stream`). The end-to-end silence test uses a real delay before the next message so the watchdog actually wins the race the way it does in production.

**cpp#145 sharpened this the hard way: "alive" is not the same knob as "rearmed".** Once the wait states existed, a test asserting only that the session survived stopped proving the rearm — deleting `_bump_idle_deadline()` from `note_activity()` left the session alive on the model-wait ceiling and the whole suite stayed green. A cpp#125 lock had been destroyed by an unrelated feature and nothing noticed. Assert the deadline moved (`_last_activity_at`), not merely that nothing died, and run the mutation before believing the test.
