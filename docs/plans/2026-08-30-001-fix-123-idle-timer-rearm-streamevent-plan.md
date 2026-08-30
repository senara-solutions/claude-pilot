---
title: Idle Timer Rearm on Intra-Turn Stream Activity - Plan
type: fix
date: 2026-08-30
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Idle Timer Rearm on Intra-Turn Stream Activity - Plan

## Goal Capsule

Objective: a headless pilot session that is still producing output runs to completion instead of being killed at 300 s, so a dispatched dev-groom ticket reaches an open PR.

Means: rearm the idle guardrail on content-bearing SDK stream events, and give the message loop a terminal branch so no SDK message type is discarded silently (KTD1, KTD2, KTD3).

Authority hierarchy: `senara-solutions/claude-pilot#123` body > this plan > implementer judgment. Upstream evidence lives on `senara-solutions/mika#2029`.

Stop conditions: stop and surface if the idle guardrail cannot be made to distinguish content deltas from `ping` keepalives with the SDK version pinned in `uv.lock` — that distinction is the whole fix, and a version that hides the raw SSE `type` invalidates KTD2.

Execution profile: single-repo, three source files plus tests. No migration, no external contract change.

Tail ownership: the implementer owns the PR through CI green.

## Product Contract

### Summary

Rearm the idle timer whenever the SDK delivers a content-bearing stream event, not only at turn boundaries. Handle `StreamEvent` in the agent message loop, and log any SDK message type the loop does not handle so the next silent class is visible on its first occurrence.

### Problem Frame

`agent.py:116` sets `include_partial_messages=True`, so the SDK delivers `StreamEvent` messages carrying raw Anthropic SSE deltas. The `async for` loop in `_run_agent_inner` branches on `SystemMessage`, `RateLimitEvent`, `AssistantMessage`, and `ResultMessage`. `StreamEvent` matches none of them, falls to the bottom of the loop body, and is discarded with no log line and no counter.

The idle timer is therefore rearmed from only three places: construction, `resume_idle_timer()` after the relay window, and `on_assistant_message()` on a `message_id` change — a turn boundary. Nothing rearms it inside a turn. A turn whose generation runs past `idleTimeoutMs` is aborted while the model is still producing.

The guardrail's own comment at `guardrails.py:246-248` claims the opposite: "idle timeout is reserved for 'nothing at all' from the SDK." The code tests "no new turn boundary," which with partial messages enabled is a strictly weaker predicate.

Measured on the 25 most recent `claude-pilot completed` rows in the local mika task store: 1 turn to 303 558-307 163 ms (n=18), 2 turns to 602 733 / 603 426 / 603 531 ms (n=3), 3 turns to 843 528 ms, 5 turns to 590 584 ms. `duration` tracks `turns x ~301 s` across three distinct turn counts. Every terminal turn consumes the entire idle budget. The 5-turn outlier (~118 s per turn) is the only session in the sample that produced work and carried a non-null cost — its turns closed under the threshold, so it survived.

The failure is silent by construction, which is why six diagnostic rounds on mika#2029 eliminated auth, egress transport, tool surface, relay hang, entry-skill load, and system-prompt preset before reaching it. A path that logs nothing is indistinguishable from a path never taken.

### Key Decisions

- Raising `idleTimeoutMs` is rejected. It moves the wall instead of restoring the predicate, and it lengthens every genuine hang by the same amount. Governs R1, R2.
- The guardrail must stay able to detect true silence. Restoring intra-turn visibility must not cost the detector its purpose. Governs R2, R3.

### Requirements

Guardrail behavior

- R1. A turn that streams content for longer than `idleTimeoutMs` does not trip the idle guardrail.
- R2. A session that delivers no content-bearing stream event for `idleTimeoutMs` still trips `idle_timeout` at the configured threshold.
- R3. Keepalive traffic does not count as progress: a stream carrying only `ping` events is silent for R2's purposes.
- R4. `pause_idle_timer()` and `resume_idle_timer()` keep their current relay-window semantics; a resume grants a full fresh budget.
- R5. Rearming costs O(1) per stream event — no asyncio task is created or cancelled per delta.

Observability

- R6. An SDK message type the agent loop does not handle is logged on its first occurrence per type, at most once per type per session.
- R7. An `idle_timeout` abort detail reports how many content-bearing stream events the session observed, so the next reader can tell a producing session from a silent one without instrumenting anything.

### Scope Boundaries

In scope: the rearm predicate, the `StreamEvent` branch, the unhandled-message branch, and tests.

Out of scope: the value of `idleTimeoutMs`; `stall_detected`, `empty_response`, and `maxTurns`, which cover degenerate content and keep their current division of labour; the mika-side fixes #2013, #2045, #2046 and the eliminated egress lineage mika#1901 / PR#2019.

### Acceptance Examples

- AE1. Covers R1, R5. A turn opens, then 400 content deltas arrive over 600 s with no turn boundary. No abort fires.
- AE2. Covers R2. A turn opens, then nothing arrives for `idleTimeoutMs`. `idle_timeout` fires.
- AE3. Covers R3. A turn opens, then only `ping` events arrive for longer than `idleTimeoutMs`. `idle_timeout` fires.
- AE4. Covers R4. Activity arrives while the timer is paused for the relay. The resume grants a full budget rather than inheriting the paused remainder.

### Sources

- `src/claude_pilot/agent.py:110-130` — `ClaudeAgentOptions`, `include_partial_messages=True`.
- `src/claude_pilot/agent.py:146-320` — the message loop and its four `isinstance` branches.
- `src/claude_pilot/guardrails.py:239-250` — turn-boundary rearm and the comment the code contradicts.
- `src/claude_pilot/guardrails.py:355-385` — `_reset_idle_timer` and `_idle_watchdog`.
- `claude_agent_sdk.types` — `Message` is a six-member union; `StreamEvent.event` is the raw Anthropic stream event dict.
- `senara-solutions/mika#2029` — measurements and the six eliminated hypotheses.

## Planning Contract

### Key Technical Decisions

KTD1. Track a monotonic last-activity deadline instead of recreating the timer task. `_idle_watchdog` loops: compute `remaining = last_activity + timeout - loop.time()`, sleep it, and abort only when it has actually elapsed. Activity updates a float. Governs R5. Rejected: calling `_reset_idle_timer()` per delta, which would create and cancel thousands of asyncio tasks per turn.

KTD2. Recognize progress by an allow-list of raw SSE event types: `content_block_delta`, `content_block_start`, `content_block_stop`, `message_start`, `message_delta`, `message_stop`. `ping` and `error` are excluded. Governs R3. An allow-list is chosen over excluding `ping` because a future keepalive under a new name would silently defeat the guardrail, and that failure — an inert detector — is worse than the one being fixed. The Anthropic top-level SSE type set is small and stable; extensions have historically arrived as `content_block_delta` subtypes, which this list already admits. KTD3 makes an unrecognized type visible rather than silent, so the allow-list's blind spot is observable.

KTD3. Give the message loop a terminal branch that logs the first occurrence of each unhandled message type. Governs R6. This is the fix for the defect class, not only for this instance: the bug existed because `StreamEvent` fell off the end of the loop without a trace.

KTD4. Count content-bearing stream events on the guardrail and include the total in the `idle_timeout` abort detail. Governs R7. One number in the line that already gets read turns the next occurrence of this class into a one-line diagnosis.

### Assumptions

- `StreamEvent.event` is a `dict` whose `type` key holds the raw SSE event name. Guarded with `isinstance` and `.get` so a shape change degrades to "not progress" rather than raising.
- Turn-boundary rearm stays in place. Intra-turn activity is additive to it, not a replacement.

### Sequencing

U1 lands the guardrail mechanism, U2 wires the loop, U3 covers both. U2 depends on U1's public method.

## Implementation Units

### U1. Deadline-based idle watchdog and stream-activity signal

Goal: `SessionGuardrails` rearms on an O(1) activity signal and reports the count it observed.

Requirements: R1, R2, R4, R5, R7.

Files: `src/claude_pilot/guardrails.py`.

Approach: add `_last_activity_at: float` and `_stream_activity_count: int`. Add `note_stream_activity()` which increments the counter and sets `_last_activity_at` to `loop.time()` — no task work. `_reset_idle_timer()` sets `_last_activity_at` and creates the watchdog task as it does today, preserving R4. Rewrite `_idle_watchdog` per KTD1 as a loop over the remaining deadline. Append the observed count to the `idle_timeout` abort detail per KTD4. Leave the `rate_limited` branch untouched. Expose `stream_activity_count` as a read-only property.

Test scenarios: activity extends the deadline past the original expiry; no activity aborts on schedule; activity during a pause does not resurrect the timer; resume grants a full budget.

Verification: `uv run pytest tests/test_guardrails.py`.

### U2. Handle StreamEvent and close the silent branch

Goal: the agent loop feeds intra-turn progress to the guardrail and never discards a message type without a trace.

Requirements: R1, R3, R6.

Files: `src/claude_pilot/agent.py`, `src/claude_pilot/ui.py`.

Approach: import `StreamEvent`. Add a branch before `AssistantMessage` that reads the raw event type, calls `guardrails.note_stream_activity()` when it is in the KTD2 allow-list, and `continue`s either way. Add a terminal branch at the end of the loop body that logs the first occurrence of each unhandled message type, tracked in a local `set`. Add `log_unhandled_message(type_name)` to `ui.py` next to the other renderers.

Test scenarios: a content delta rearms; a `ping` does not; an unknown message type logs once and does not log twice.

Verification: `uv run pytest tests/`.

### U3. Tests

Goal: both directions of the guardrail are pinned so a future refactor cannot silently restore the old predicate.

Requirements: R1, R2, R3, R4, R5.

Files: `tests/test_guardrails.py`, `tests/test_agent.py` if present, else a new module.

Approach: cover AE1-AE4 with a short `idleTimeoutMs` so tests stay fast. Use `asyncio` sleeps well under a second and assert on `aborted` / `abort_reason`. Add a case asserting `note_stream_activity()` creates no new task, satisfying R5 by construction rather than by timing.

Test scenarios: AE1, AE2, AE3, AE4, plus the no-task-churn assertion.

Verification: `uv run pytest`.

## Verification Contract

- `uv run ruff check`
- `uv run mypy src`
- `uv run pytest`
- `bash scripts/verify-pipeline.sh`

The behavioral exit criterion is AE1 and AE2 passing together: a streaming turn survives past the threshold, and a silent one still dies at it. Either alone is not the fix.

## Definition of Done

Global:

- R1-R7 hold, each traced to a passing test.
- All four verification commands pass.
- No change to `idleTimeoutMs` or to any other guardrail threshold.
- No dead-end or experimental code left in the diff.
- The PR cross-references mika#2029 and cpp#123.

Per unit:

- U1: the watchdog is deadline-driven; `note_stream_activity()` creates no asyncio task.
- U2: `StreamEvent` is handled; an unhandled type logs exactly once per type per session.
- U3: AE1-AE4 pass, and the no-task-churn assertion passes.
