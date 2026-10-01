---
title: "A subagent message (`parent_tool_use_id`) must not arm the main pilot's stall or turn counter"
date: 2026-10-01
module: claude_pilot.guardrails
component: stall-detector
problem_type: false_positive_guardrail
category: tooling-decisions
tags: [guardrails, stall-detected, subagents, sidechain, parent-tool-use-id, turn-counter, max-turns, idle-watchdog, liveness, false-alarm, claude-pilot-259, mika-1833]
applies_when: "wiring an SDK AssistantMessage to any per-turn counter or guardrail, or reasoning about a stall_detected / turn-count anomaly on a session that fanned out subagents"
---

# A subagent message must not arm the main pilot's stall or turn counter

## What happened

`/ce:code-review` dispatched 8 reviewers via the `Agent` tool (session
`73e6f3ee`, mika#1833). The last MAIN tool-bearing message was at 03:15:04Z, and
the subagents were still calling `Read`/`Bash` at that same instant — the session
was working. At 03:15:06Z it was killed: `[guardrail] stall_detected: 5
consecutive turns with no tool calls`. The log showed "turns" 170→192 in **10
seconds**, every one "thinking-only, no actions". Those were not main turns:
**177 of the 197 messages after the fan-out were subagent messages.** Across the
400 most recent pilot logs, 4 of the 5 `stall_detected` trips since 2026-09-19
were subagent-heavy; only `b00a0d7d` (0 subagent messages in its tail) was a
genuine stall.

## Root cause

The SDK streams a subagent's `AssistantMessage`s onto the **parent** stream, each
carrying a non-None `parent_tool_use_id` (the `Agent` tool that spawned it).
`guardrails.on_assistant_message` grouped by `message_id` but had **no
`parent_tool_use_id` filter**, and `agent.py` dropped the field instead of
passing it. So every subagent thinking/synthesis turn — which has no tool_use —
incremented the MAIN pilot's `_consecutive_stall_turns` toward `stallThreshold`
(5) and its `_turn_count`. The same path feeds the `[cache] turn N` line, so that
counter over-reported too (`turn 192` under `maxTurns=150`). The companion memo
`the-cache-turn-log-counter-counts-subagent-messages-not-sdk-turns.md` had
already named the unit mismatch as an observability pitfall; this is its fix.

## The pattern

**A per-turn counter or guardrail keyed on the SDK message stream must decide,
per message, whether it belongs to the loop it is counting.** `max_turns` (the
SDK cap) bounds only the main query loop, so any pilot-side counter meant to be
comparable to it — stall turns, `[cache] turn N`, a budget-per-phase tally — must
exclude messages with a `parent_tool_use_id`. Subagent messages belong to a
different loop (the sidechain); folding them into the main counter is a category
error that both over-counts the unit and manufactures false `stall_detected`
kills proportional to the fan-out width.

The clean cut is at the single boundary site (`on_assistant_message`): branch on
`parent_tool_use_id` and return early for subagent messages, keeping a
separately-named `subagent_messages` count for observability. Do NOT reach into
the stall/empty/turn fields with ad-hoc conditionals scattered down the method —
one early branch is auditable; N guards are not.

## But liveness is not production — keep the idle path alive

The early return must still **rearm the idle deadline** (`_bump_idle_deadline()`).
A subagent-only busy stretch is proof the session is alive, so the filter that
excludes it from the stall/turn counters must not simultaneously make it look
idle to the watchdog — that would trade a false `stall_detected` for a false
`idle_timeout`. This is the same liveness-vs-production split cpp#123/#125/#145
drew for `StreamEvent` and `UserMessage`: a signal can prove the session is alive
without being a MAIN-loop turn. Separate the two responsibilities explicitly.

The negative control is the regression lock: a genuine main stall (5 MAIN
tool-less turns, the `b00a0d7d` shape) must STILL trip `stall_detected`, and
tool-bearing subagent messages interleaved with it must not rescue it — the
filter removes a false positive, it does not remove the guardrail (cpp#4).

## Refuted alternative

Filtering in `agent.py` by skipping `on_assistant_message` entirely for subagent
messages was tempting (fewer parameters) but wrong: the method is the one place
that also owns the idle-deadline rearm and the `subagent_messages` tally. Skipping
it would drop the liveness signal and the observability count. The branch belongs
INSIDE the guardrail, which is why the fix passes the flag in rather than
gating the call out.

## References

- Source: `guardrails.py` (`on_assistant_message` subagent branch,
  `subagent_messages` property), `agent.py` (passes `parent_tool_use_id`); bundled
  SDK `types.py` (`AssistantMessage.parent_tool_use_id`).
- Known fact this resolves:
  `the-cache-turn-log-counter-counts-subagent-messages-not-sdk-turns.md`.
- Doctrine: cpp#4 (stall on text-only turns — the preserved negative control),
  cpp#123/#125/#145 (`liveness-signals-are-not-all-production-signals.md`),
  `sdk-message-union-must-be-handled-exhaustively.md`. Adjacent (distinct): cpp#228
  (subagent fan-out budget), cpp#257 ("Prompt is too long" on `Agent` dispatch).
  Incident: `73e6f3ee` (mika#1833). PR: cpp#259.
