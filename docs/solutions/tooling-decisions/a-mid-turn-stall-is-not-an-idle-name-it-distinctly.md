---
title: "A mid-turn stall is not an idle — name it distinctly, or a hung generation launders into idle_timeout"
date: 2026-09-28
last_updated: 2026-09-28
module: claude_pilot.guardrails
component: idle-watchdog
problem_type: tooling_decision
category: tooling-decisions
tags: [guardrails, idle-timeout, stream-stalled, watchdog, observability, classification, cpp-219, cpp-214, cpp-145]
applies_when: "a single guardrail state or abort reason is covering two distinct failure populations, and one of them is disappearing behind the other's name"
---

# A mid-turn stall is not an idle — name it distinctly

## Context

The idle watchdog (cpp#145) classifies a silence by `_wait_state`. The IDLE
state is reached when `_pending_tool_uses == 0 AND not _awaiting_model` — nobody
is outstanding. It aborts at `idleTimeoutMs` (480s post-cpp#214a) as
`idle_timeout`.

But IDLE covered two different things. A turn that OPENED (`message_start`) and
then froze mid-generation — a content block never closed, only `progress=False`
pings on the wire (excluded from the deadline reset) — is ALSO IDLE: after
`message_start` the model-wait window is already closed (`message_start` is the
only event that closes it), and no tool is pending. So a hung generation fell
into the same bucket as a genuinely finished-and-quiet turn, and both aborted
`idle_timeout`. The cpp#214 measurement found four deaths this described,
including the 2026-09-27 10:13Z death that produced no `[done]` and no artifact —
silent, and indistinguishable from a benign idle in the logs.

## Guidance

### 1. One reason must not cover two populations that call for different reads

`idle_timeout` answered "the turn ended and nothing more came." A mid-turn stall
answers "a generation was in flight and it hung." Those are different operational
facts. Collapsing them means the more urgent one (a hung generation) is read as
the benign one (a quiet session), and it vanishes. This is the same move cpp#145
made splitting `awaiting_tool`/`awaiting_model` out of `idle_timeout`, and cpp#168
made with `watchdog_error`: when a state hides a distinct failure population,
give that population its own name.

### 2. Classify from a signal already on the wire — do not add timing

The state machine already sees every raw SSE event in `note_stream_activity`. A
turn is OPEN between its `message_start` and its `message_stop`; track exactly
that with one bool (`_turn_open`), keyed on the message boundary, NOT on
content_block events (robust to a turn with several content blocks). Then in the
watchdog's IDLE branch: turn open → `stream_stalled`; else → `idle_timeout`. The
new `GuardrailAbortReason` value is additive, like `watchdog_error` before it.

### 3. Split the NAME, never the TIMING

`stream_stalled` fires at the SAME `idleTimeoutMs` deadline as `idle_timeout`.
This decision changes WHICH reason and WHAT detail surface — not WHEN a session
is killed. No new ceiling is introduced. Deciding to wait *longer* for a live-
but-slow generation is a separate question (a generation heartbeat) that depends
on whether the API exposes a "still generating" signal beyond keepalive pings; at
this writing none is identified. That is cpp#219 volet 2, an investigation, not
this change. Pin the timing invariant with a test that proves the stall fires at
the idle budget and no borrowed/new ceiling.

### 4. Reclassify only what is unambiguous — fail safe toward the old reason

The rule is fail-safe: only a CLEARLY open turn (`message_start` observed, no
`message_stop`) is reclassified. Any other state — a closed turn, nothing seen
yet, content deltas with no observed `message_start`, or an older caller passing
`None` — falls back to `idle_timeout`, the prior behaviour. The guardrail must
never MISS a death to gain precision; it may only rename a death it is sure
about. The detail string then names the stall explicitly (turn open, ping-only,
window/session event counts) so it is visible, not silent.

### 5. Emitting the reason is claude-pilot's whole job here; consuming it is not

claude-pilot's part of observability is making `stream_stalled` and its detail
available on the same abort/exit/log surface every other reason uses
(`agent.py` sets `subtype=reason.guardrail` and calls
`log_guardrail(reason.guardrail, reason.detail)` — no extra wiring needed). The
severity-tiered operator notification (mika#1381) is a SEPARATE mika-side
consumer of this reason; it does not belong in this repo.

## Related

- cpp#219 — this split (volet 1: classification + observability); volet 2
  (generation heartbeat) is a separate investigation and may be a no-op if the
  API exposes no "still generating" signal
- cpp#214 — the measurement that isolated the IDLE domain and surfaced the four
  laundered mid-turn deaths; raised `idleTimeoutMs` 300s → 480s (the ceiling
  `stream_stalled` inherits)
- cpp#145 — the four-ceiling watchdog and the IDLE-vs-WAITING split this extends
- `docs/solutions/tooling-decisions/an-idle-ceiling-is-a-calibrated-number-not-a-guess.md`
- `docs/solutions/tooling-decisions/liveness-signals-are-not-all-production-signals.md`
