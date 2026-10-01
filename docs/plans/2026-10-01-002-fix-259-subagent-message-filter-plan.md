---
ticket: cpp#259 / mika#1833
kind: fix
class: guardrail-defect (false-positive stall + over-counted turn/budget unit — subagent messages arm the MAIN pilot's counters)
status: landed (source + tests + docs ; filter at `on_assistant_message`, no classifier change, suite green)
---

# Subagent messages (`parent_tool_use_id`) must not arm the main pilot's stall or turn counter — Plan (cpp#259)

## Goal Capsule

`guardrails.on_assistant_message` (guardrails.py) receives EVERY SDK
`AssistantMessage`, including those emitted by SUBAGENTS (the `Agent` tool —
they carry a non-None `parent_tool_use_id`). `agent.py` (the call ~l.626) did
NOT filter on it. When a multi-agent review dispatches N reviewers, their
interleaved tool-less messages (thinking, synthesis) each incremented the MAIN
pilot's `_consecutive_stall_turns` and `_turn_count`. At `stallThreshold` (5),
`stall_detected` KILLED a working session.

Founding case **73e6f3ee** (mika#1833, 2026-10-01): `/ce:code-review` dispatched
8 reviewers at 03:10:32Z; the last MAIN tool message was 03:15:04Z and subagents
were still calling `Read`/`Bash` at 03:15:04Z; at 03:15:06Z `stall_detected: 5
consecutive turns with no tool calls`. The log's "turns" 170→192 in 10s were all
subagent messages (177 of 197 post-dispatch messages were subagent). The session
died on the pipeline queue (review + compound lost). Same side effect on the
budget unit: `turn 192` with `maxTurns=150`, because the same path feeds
`_turn_count` and the `[cache] turn N` line.

Goal: a message with a non-None `parent_tool_use_id` never arms the MAIN pilot's
stall / turn / empty counters, but is still proof of life (rearms the idle
deadline) and is tallied separately for observability. This is a guardrail
defect — NOT a permission-classifier change; permissions.py / tier1.py untouched.

## Cause racine (verified at the source, HEAD 1271d04, isolated clone)

- SDK `AssistantMessage` carries `parent_tool_use_id: str | None` (confirmed by
  `inspect` on the bundled `claude_agent_sdk.types`). It is non-None iff the
  message was emitted by a subagent spawned via the `Agent` tool; the SDK parser
  streams subagent messages onto the PARENT stream, so the pilot sees them.
- `agent.py` passed only `content` / `message_id` / `usage` into
  `on_assistant_message`; `parent_tool_use_id` was dropped on the floor.
- `on_assistant_message` therefore ran its full main-counter path
  (`_turn_count += 1`, speculative `_consecutive_stall_turns += 1`, empty
  detection, `[cache] turn N` boundary event) for subagent messages too.
- This is exactly the "known fact" recorded in
  `docs/solutions/tooling-decisions/the-cache-turn-log-counter-counts-subagent-messages-not-sdk-turns.md`
  (that doc diagnosed the OBSERVABILITY memo; cpp#259 is the FIX).

## Le fix (filter at the single documented site; reuse existing fields)

`guardrails.py`:
- New field `_subagent_message_count: int = 0` and read-only property
  `subagent_messages` — a separately-named counter, logging only, never arms a
  guardrail (AC1/AC5 naming requirement).
- `on_assistant_message` gains a `parent_tool_use_id: str | None = None`
  parameter. When non-None, an EARLY branch: increment `_subagent_message_count`,
  call `_bump_idle_deadline()` (liveness — AC2), and `return None`. No
  `TurnBoundaryEvent`, no touch to `_turn_count` / `_consecutive_stall_turns` /
  `_consecutive_empty_turns` / the wait-state machine.

`agent.py`:
- Pass `parent_tool_use_id=getattr(message, "parent_tool_use_id", None)` into
  `on_assistant_message`. A subagent message returns None → the boundary/cache/
  heartbeat block is skipped, so `[cache] turn N` fires only for MAIN turns and
  carries the MAIN count again (comparable to `maxTurns`).

The main is AWAITING_TOOL on the `Agent` tool throughout; only the Agent
tool_result (a `UserMessage` without a subagent parent) retires it via the
unchanged `note_activity`. No guardrail restructuring; no classifier edit.

## Acceptance criteria

- [x] **AC1 — main counters exclude subagents.** Stall AND turn counters count
  only messages without `parent_tool_use_id`; subagent messages carry a
  separately-named `subagent_messages` count (logging only). Proven:
  `test_turn_count_and_cache_boundary_exclude_subagents`,
  `test_positive_control_73e6f3ee_shape_does_not_trip_stall`.
- [x] **AC2 — idle still sees subagent activity as alive.** A subagent message
  rearms the idle deadline; a subagent-only busy stretch does not look idle.
  Proven: `test_subagent_message_rearms_the_idle_deadline` (100 subagent
  messages across ~1.7x the idle budget, deadline advances each time, no abort).
- [x] **AC3 — positive control (73e6f3ee shape).** 8 reviewers dispatched via the
  `Agent` tool (→ AWAITING_TOOL) then 8 interleaved tool-less subagent messages ⇒
  NO `stall_detected`, main turn count stays 1. Proven:
  `test_positive_control_73e6f3ee_shape_does_not_trip_stall`.
- [x] **AC4 — negative control (cpp#4 not regressed).** 5 MAIN tool-less turns ⇒
  `stall_detected` STILL fires. And subagent messages neither arm nor DISARM the
  main counter (tool-bearing subagent messages interleaved with a genuine main
  stall do not rescue it). Proven:
  `test_negative_control_five_main_tool_less_turns_still_trip_stall`,
  `test_subagent_interleave_does_not_rescue_a_genuine_main_stall`.
- [x] **AC5 — `[cache] turn N` = MAIN count.** The boundary event's
  `just_closed_turn` equals the MAIN turn count; a global subagent count is kept
  under a different name (`subagent_messages`). Demo (before/after on the
  73e6f3ee shape): BEFORE turn count 9 + `stall_detected`; AFTER turn count 1,
  `subagent_messages=8`, no abort.
- [x] **AC6 — scope.** permissions.py / tier1.py / policy untouched (classifier
  not weakened). Full suite green: 1583 passed; ruff clean; mypy clean.

## Fire-Disposition

- **Feu** : `stall_detected` (and the inflated turn/budget unit) firing on a
  MAIN pilot whose only "stall" was N subagents' interleaved tool-less
  thinking/synthesis turns — the dominant stall population since 2026-09-19 (4 of
  5 measured `stall_detected` were subagent-heavy; founding 73e6f3ee). **Éteint** :
  a subagent message is excluded from the main stall/turn counters; the working
  session survives the fan-out, and `[cache] turn N` is comparable to `maxTurns`
  again.
- **Not touched (the true-stall control, b00a0d7d)** : a genuine main stall (0
  subagent messages in its tail) STILL fires `stall_detected` — AC4 is the
  regression lock. The filter removes a false positive; it does not remove the
  guardrail.
- **Vérif de sortie** : 7 new cpp#259 tests GREEN; full suite 1583 passed; ruff
  clean; mypy clean; verify-pipeline green (docs + source). Before/after demo
  reproduced at the source (9 → 1 main-turn count on the 73e6f3ee shape).
- **Résidu (nommé)** : (1) the SUBAGENT fan-out itself is still bounded by no
  turn/cost budget — a separate p2/p3 design item (cpp#228), out of scope here;
  the adjacent-coverage note in the cache-turn solution doc already flags it.
  (2) Subagent tool RESULTS (`UserMessage` with a subagent `parent_tool_use_id`)
  still flow through `note_activity` and decrement the MAIN `_pending_tool_uses`;
  this fix deliberately does not touch `note_activity` (the ticket scopes the fix
  to `on_assistant_message` and warns against broad guardrail restructuring), and
  the stall/turn defect — the founding kill — is fully closed without it. If the
  wait-state accounting under fan-out proves wrong in the field, that is a
  distinct follow-up.

## Références

- Solution : `docs/solutions/tooling-decisions/a-subagent-message-does-not-arm-the-main-pilots-stall-or-turn-counter.md`.
- Known fact (observability memo this fix resolves) :
  `docs/solutions/tooling-decisions/the-cache-turn-log-counter-counts-subagent-messages-not-sdk-turns.md`.
- Doctrine : cpp#4 (stall on text-only turns — the negative control preserved).
  Liveness vs production : `liveness-signals-are-not-all-production-signals.md`.
  SDK message union : `sdk-message-union-must-be-handled-exhaustively.md`.
  Related/adjacent : cpp#228 (fan-out budget — distinct), cpp#257 ("Prompt is
  too long" on `Agent` dispatch). Incident: 73e6f3ee (mika#1833).
