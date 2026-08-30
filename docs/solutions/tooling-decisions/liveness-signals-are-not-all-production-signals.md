---
title: "Separate liveness from production when rearming the idle watchdog — a tool result is not a generation delta"
date: 2026-08-30
last_updated: 2026-08-30
module: claude_pilot.guardrails
component: idle-watchdog
problem_type: tooling_decision
category: tooling-decisions
tags: [claude-agent-sdk, stream-event, user-message, guardrails, idle-timeout, liveness, cpp-125, cpp-123, mika-2029]
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

Collapsing the two would make the abort detail lie about how much the model produced, and could mask a genuine throttle as recovered. Both share one private `_bump_idle_deadline()` so the O(1) deadline-move stays in one place; only the accounting differs.

### 3. Prove the knob, then prove it is not vacuous

A test that "a streaming turn is not killed" is worthless without its dual — "a genuinely silent session still fires `idle_timeout`". Keep both, at both levels: the guardrail unit (deadline arithmetic) and the agent loop end-to-end (the watchdog racing the message stream through `_merge_stream`). The end-to-end silence test uses a real delay before the next message so the watchdog actually wins the race the way it does in production.
