---
title: "The `[cache] turn N` log counter counts subagent messages too — it is not the SDK `max_turns` unit"
date: 2026-09-28
module: claude_pilot.agent
component: guardrails-observability
problem_type: observability_pitfall
category: tooling-decisions
severity: medium
tags: [max-turns, guardrails, watchdog, budget, subagents, sidechain, parent-tool-use-id, observability, log-counter, false-alarm, mika-2562, claude-pilot-145, claude-pilot-147]
applies_when: "reading a pilot's `[cache] turn N` count to judge whether the max_turns cap fired, especially for a session that fanned out subagents"
---

# `[cache] turn N` counts subagent messages — it is not the `max_turns` unit

## What happened

A pilot ran with `pilot_budget_armed max_turns=150 source=env`, yet its log reached
`[cache] turn 366` before halting on `stall_detected` (session `b8417083`, mika#2562
impl). At a glance the cap looked broken: armed at 150, ran to 366. It was **not**
broken. The two numbers count different things.

## The two counters, and their units

- **`max_turns` (the cap)** bounds the SDK **main query loop** only. It is passed at
  `agent.py` `_sdk_guardrail_kwargs` (`kwargs["max_turns"] = config.maxTurns`), and the
  SDK stops the main loop with `error_max_turns` at `max_turns + 1`. `ResultMessage.num_turns`
  is the main-loop count.
- **`[cache] turn N`** (`ui.py` `log_cache_usage`, driven by `guardrails.on_assistant_message`)
  increments on **every** `AssistantMessage` on the stream, grouped by `message_id`, with
  **no `parent_tool_use_id` filter**. Subagent (sidechain) `AssistantMessage`s carry a
  `parent_tool_use_id` and the SDK message parser streams them onto the *parent* stream, so
  the pilot sees and counts them too.

When a session fans out subagents (e.g. a `ce-code-review` reviewer salvo — 8 concurrent
reviewers, several relaunched on the parent model after the subagents blew their context on
`Prompt is too long`), their messages inflate `[cache] turn N` far past the main-loop turn
count. In `b8417083`: one `[init]` session, one `query()`, **no** `error_max_turns`, ~97
tool dispatches, a tail of thinking-only turns that tripped `stall_detected` (5 consecutive
no-tool turns). The main-loop turns stayed **under 150** the whole 34-minute run; the surplus
up to 366 was subagent traffic.

## The pattern

**Before concluding a turn/budget cap misfired, confirm it on the cap's OWN unit.** For
`max_turns` that means `error_max_turns` / `ResultMessage.num_turns`, **not** `grep -c '\[cache\].*turn'`
— which over-reports true agentic turns by the whole subagent fan-out. The discriminating
control: a **no-subagent** session with `max_turns=150` logs exactly 150 `[cache] turn` lines
and then `error_max_turns ... after 151 turns` (`[cache] turn` == main loop 1:1 only when
nothing fans out). A high `[cache] turn` count with no `error_max_turns` and Task* SDK
messages present is a subagent salvo, not a runaway main loop.

Refuted alternatives (worth stating because they are the tempting ones): it was **not** the
cap failing to be passed on a resume/fall-through path (single session, single query, the
armed value was in `_sdk_guardrail_kwargs`), and **not** a counter reset on stall-resume (no
pilot-level resume fired; the 30 denials were all non-terminal deny-with-notify, so the
resume controller never armed).

## Adjacent coverage gap (separate design item, not this defect)

`max_turns` bounds only the main loop, so **subagent fan-out is bounded by no turn or cost
budget** — a session can run 8 concurrent reviewers / 34 min / 366 stream-turns and be stopped
only by the `stall_detected` heuristic. That is a real p2/p3 design question (a bound on total
spawned subagent work), tracked separately if wanted — it is **not** why the 150 cap "did not
bite" here. The cap bit correctly on its own unit.

## References

- Source: `agent.py` (`_sdk_guardrail_kwargs`, `on_assistant_message` path), `ui.py`
  (`log_cache_usage`), `guardrails.py` (turn grouping); bundled SDK `types.py`
  (`max_turns`, `ResultMessage.num_turns`, `AssistantMessage.parent_tool_use_id`).
- Context: cpp#145 (four wait ceilings), cpp#147 (three silences). Incident: session
  `b8417083` (mika#2562 impl). Doctrine: "plafond touché → lire le pourquoi" — here the
  *why* is a unit mismatch, so the disposition is a memo, not a fix.
