---
title: "An idle ceiling is a calibrated number, not a guess — measure the domain's resume tail, keep the hierarchy"
date: 2026-09-28
last_updated: 2026-09-28
module: claude_pilot.types
component: idle-watchdog
problem_type: tooling_decision
category: tooling-decisions
tags: [guardrails, idle-timeout, watchdog, calibration, measurement, ceiling-hierarchy, cpp-214, cpp-145, cpp-219]
applies_when: "choosing or raising a watchdog ceiling, or defending one against 'just bump it' or 'why that exact number'"
---

# An idle ceiling is a calibrated number, not a guess

## Context

The watchdog (cpp#145) has four ceilings. The IDLE one, `idleTimeoutMs`, governs
ONLY the case where nobody is outstanding — a mid-turn stall with no tool running
and no next-turn token pending. It sat at 300s (`types.py`). The three other
ceilings (`toolWaitCeilingMs`=1_800_000, `modelWaitCeilingMs`=900_000,
`rateLimitCeilingMs`=1_800_000) govern the WAITING states and are untouched here.

## Guidance

### 1. The number is calibrated on a measured domain, not picked round

The cpp#214 measurement isolated the IDLE domain and found generation flows
resume up to a MAX of 296s (p99 229s, zero recoveries past 300s in 188 samples).
So `idleTimeoutMs` is raised 300_000 -> 480_000 (8 min) = max-observed-resume
(296s) + ~1.6x margin. 480s is a data-grounded value, not a guess; write the WHY
next to the value so the next reader does not "round it back."

### 2. A ceiling censors its own tail — a survivor is byte-identical to a death

At the old 300s wall, a session that would have resumed at 296s and one that was
truly dead looked identical: both got killed at 300s. The ceiling truncates the
very distribution you would use to justify it. This is why the margin is measured
against the observed resume tail, not against the old ceiling.

### 3. Keep the hierarchy: idle < model < tool == rateLimit

480s stays strictly below `modelWaitCeilingMs`=900s, so the cpp#145 ordering
(idle < model < tool == rateLimit) holds. If idle ever crossed the model ceiling,
a session stalled mid-generation would again be censored by the wrong guard. A
test pins this invariant (`test_guardrail_ceiling_hierarchy_invariant`).

### 4. Raising a ceiling MOVES the guard, it does not REMOVE it

The named acceptance criterion: an IDLE session (nobody outstanding) that stays
mute past the raised ceiling STILL aborts with `idle_timeout`. Prove it with a
negative test (`test_cpp214_idle_session_mute_past_raised_ceiling_still_aborts`),
or the change silently disabled the guard instead of relocating it.

## This is a palliative, not the fix

Raising the wall lengthens every genuine hang by the same amount. The real fix is
stall detection / a generation heartbeat that distinguishes "resuming" from
"dead" without a wall-clock guess — that is cpp#219. 480s is the interim measure
until #219 lands.

## Related

- cpp#145 — the four-ceiling watchdog and the IDLE-vs-WAITING split
- cpp#214 — this calibration (measurement in the issue comment)
- cpp#219 — the durable fix (stall detection / generation heartbeat)
- `docs/solutions/tooling-decisions/liveness-signals-are-not-all-production-signals.md`
