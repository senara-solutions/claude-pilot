---
title: content_block_stop Model-Wait-Window Fix - Plan
type: fix
date: 2026-09-11
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# content_block_stop Model-Wait-Window Fix - Plan

## Goal Capsule

Objective: `senara-solutions/claude-pilot#177` — 7 real sessions killed
`idle_timeout` "before any work" between 09-04 and 09-10, one per day
(`b8d3b90e`, `4765b744`, `e7fc0e2b`, `6b5481f6`, `a0fcb92d`, `f9cc3d35`,
`8a0b6b89`). Sibling of `senara-solutions/mika-platform#2029` (the pilot
substrate for the same *class* of kill); this ticket is the **pilot-side
cause**, and mika#2029 keeps the mika-side substrate (stale egress proxy,
`dispatch-lib.sh`'s false diagnosis, the `NO_PR` pattern).

Means: read `guardrails.py`'s `_WaitState`/`note_stream_activity`/
`note_activity` machinery (cpp#145, extended by cpp#168) closely, verify the
ticket's proposed mechanism and fix directly against that source rather than
taking it on faith, then apply the minimal, precise version of the fix and
prove it with the operator-mandated red-before/green-after gate using the
ticket's verbatim event sequence.

Authority hierarchy: `senara-solutions/claude-pilot#177` body > this plan >
implementer judgment. The ticket is unusually well-specified (verbatim stream
traces, exact code locations, a measured TTFT table); it was followed
closely, and every place this plan diverges from it in wording is called out
below.

Stop conditions: none that halt — the fix is a single-set narrowing with no
open sub-question.

Execution profile: single-repo (`claude-pilot`), one source file
(`guardrails.py`), one test file (`test_guardrails.py`, extended).

Tail ownership: implementer opens the PR through green CI (gated, no merge);
MPC reviews/merges/redeploys.

## Mechanism, verified against `guardrails.py` @ `d9f9254`/`d6b576c`

Confirmed by reading the code, not assumed from the ticket:

- `_PRODUCTION_STREAM_EVENTS` (pre-fix, `:77-84`) = `{message_start,
  content_block_start, content_block_delta, content_block_stop}`.
- `note_stream_activity` (`:442-446`, pre-fix): any event in that set sets
  `self._awaiting_model = False` and clears `_model_wait_started_at` — "the
  next turn is producing."
- `note_activity` (tool result, `:474-485`): when `_pending_tool_uses` drops
  to 0, it OPENS the model-wait window (`_awaiting_model = True`, anchors
  `_model_wait_started_at`) — this is the line the 2026-08-31 cpp#145 fix
  added specifically to stop measuring this silence against `idleTimeoutMs`.
- **The gap**: the CLI runs a dispatched tool as soon as its `tool_use`
  block's input JSON is complete — before the SSE `content_block_stop` that
  closes that same block is relayed. So on the wire, `content_block_stop`
  for the CURRENT `tool_use` block reliably arrives AFTER that block's own
  tool result, not before. Pre-fix, `content_block_stop` was a member of
  `_PRODUCTION_STREAM_EVENTS`, so `note_stream_activity("content_block_stop")`
  immediately reclosed the window `note_activity` had just opened. A turn
  with exactly ONE `tool_use` therefore always ends in `waiting: none` (the
  IDLE state) and is measured against `idleTimeoutMs` (300s); a turn with
  several `tool_use` blocks only survives by accident, because a second
  tool's result happens to arrive after the first block's trailers reopen
  the race.
- **Why it's lethal**: the `tool result -> message_start` latency measured
  across 6 timestamped 09-10 sessions is bimodal — either ≤29s or 300-389s,
  with **zero** values in between (a fixed delay, not slow generation; origin
  undetermined and explicitly out of this ticket's scope per its own "Hors
  périmètre" section). A session in the slow mode needs ~355s to produce its
  first token. Measured against the wrong ceiling (`idleTimeoutMs`=300s
  instead of `modelWaitCeilingMs`=900s), it dies ~55s before that token would
  have arrived.
- `note_stream_activity`'s docstring (pre-fix, `:424-431`) already half-saw
  this for `message_delta`/`message_stop` (cpp#145's own finding: "in three
  of the six killed sessions those trailers arrive AFTER the tool result")
  and stopped the exclusion one event short of `content_block_stop`.

This plan's own reading independently confirms the ticket's diagnosis: no
part of the proposed mechanism required correction.

## Fix: only `message_start` closes the model-wait window

Per the ticket's own proposed correctness bar — "only `message_start` proves
the next turn is producing" — narrowed to its most precise form.
`_PRODUCTION_STREAM_EVENTS` becomes:

```python
_PRODUCTION_STREAM_EVENTS = frozenset({"message_start"})
```

(pre-fix: `{message_start, content_block_start, content_block_delta,
content_block_stop}`).

This is stricter than a literal read of the ticket's fix bullet 1, which
names dropping `content_block_stop` specifically and says "more correctly,
[close] only on `message_start`" as the better shape — the plan implements
the "more correctly" version directly rather than the intermediate one,
since the ticket itself asks for "the minimal, correct version." Rationale,
independent of the ticket: between a `message_stop` and the next
`message_start`, NOTHING the wire delivers is the next turn's production —
not the trailers of the OLD turn (`message_delta`/`message_stop`, already
excluded since cpp#145) and not the content-block events of a turn already
known to be in flight (`content_block_start`/`content_block_delta`/
`content_block_stop`) either. `message_start` is the one event that can only
ever begin a genuinely new turn.

**What does NOT change**: every one of those five excluded event types
remains full LIVENESS. `note_stream_activity` calls
`self._bump_idle_deadline()` unconditionally, outside the
`_PRODUCTION_STREAM_EVENTS` check — so a stream of content deltas, or a
turn's own closing trailers, still rearms the idle deadline and increments
`stream_activity_count` exactly as before. Only the model-wait-window CLOSE
condition narrows; the REARM behavior (AC1 in cpp#145's own test suite) is
untouched, and is pinned unchanged by
`test_ac1_stream_event_still_rearms_the_deadline_under_the_state_machine`.

**What does NOT reopen**: the text-only "model spoke and stopped" case
(`on_assistant_message`, `:618-623`) is untouched — a turn with no
`tool_use` block still sets `_awaiting_model = False` directly at the turn
boundary (never via the stream-event path this fix touches), so a session
that genuinely goes silent after a text-only reply is still measured against
`idleTimeoutMs`, unchanged. This is the negative control the ticket makes
mandatory (see Gate below), and it holds specifically because the fix is
scoped to `_PRODUCTION_STREAM_EVENTS` / `note_stream_activity` only —
`on_assistant_message`'s own no-tool-use branch is a completely separate code
path that this change does not touch.

## MANDATORY negative-test gate (operator-required)

Both cases replay the ticket's verbatim event sequence: `tool_use`
AssistantMessage → tool result → `content_block_stop` → `message_delta` →
`message_stop` → silence (scaled to millisecond budgets per this test file's
existing convention — see `_idle_config`'s docstring — the real-world
budgets are `idleTimeoutMs`=300s / `modelWaitCeilingMs`=900s).

1. **`test_cpp177_content_block_stop_after_tool_result_does_not_close_the_wait`**
   — the sequence WITH the tool result. Must yield `waiting: awaiting_model`
   and NOT abort before `modelWaitCeilingMs`. Proved RED on pristine
   (pre-fix) `main` (`d6b576c`): `content_block_stop` closed the window back
   to IDLE, and the session died at the idle budget — verbatim failure
   output in the PR body. Proved GREEN after: `content_block_stop` no longer
   closes the window (excluded from the narrowed `_PRODUCTION_STREAM_EVENTS`),
   so the state stays `AWAITING_MODEL` through the same trailer sequence and
   the session survives silence up to `modelWaitCeilingMs`.
2. **`test_cpp177_text_only_turn_with_no_tool_result_still_dies_at_idle_timeout`**
   — the SAME trailing shape (`content_block_stop`/`message_delta`/
   `message_stop`) but WITHOUT a tool result: a text-only turn, the existing
   `:618-623` "model spoke and stopped" case. Must STILL abort at
   `idleTimeoutMs`. Passes UNCHANGED before and after the fix (regression
   guard) — proved by running it against both pristine and fixed
   `guardrails.py`.

Both existing cpp#145 tests that asserted a NON-`message_start` event closed
the window (`test_ac7_the_aae80d84_trace_no_longer_kills_the_session`'s
closing assertion, previously `content_block_delta`;
`test_returning_to_idle_restores_the_tighter_idle_budget`, previously
`content_block_start`) are updated in place to use `message_start` — the
event that actually proves it under the corrected, more precise rule. This
is a deliberate, narrow correction of those two tests' fixtures, not a
weakening: both still assert the window closes, on the one event that
genuinely closing it requires.

## Regression safety vs. cpp#168 and the existing wait-state suite

`_idle_watchdog`'s deadline/deferral loop, the `AWAITING_TOOL` /
`AWAITING_MODEL` / `IDLE` precedence, `_wait_ceiling_secs`,
`_abort_wait_ceiling`, and the cpp#168 `watchdog_error` hardening
(`except Exception` around the watchdog loop) are untouched by this change —
the fix is scoped to the single-line `_PRODUCTION_STREAM_EVENTS` definition
and its docstrings. The full existing guardrails suite — `watchdog_error`
(cpp#168), `rate_limited` (cpp#119/#133), `blocked_on_operator_input`
(cpp#144), `awaiting_tool`/`awaiting_model` (cpp#145) — runs unmodified
except for the two fixture corrections above, and all pass. See Verification
for the full-suite run.

## Implementation units

1. **`guardrails.py`** — `_PRODUCTION_STREAM_EVENTS` narrowed to
   `frozenset({"message_start"})`; its module-level comment and
   `note_stream_activity`'s docstring rewritten to name the cpp#177
   mechanism and the corrected condition precisely.
2. **`tests/test_guardrails.py`** —
   - Two new mandatory-gate tests (`test_cpp177_*`, described above), placed
     directly after the AC7 negative control
     (`test_model_wait_that_never_resumes_dies_at_the_ceiling`).
   - Two existing tests' fixtures corrected in place (`content_block_delta`
     → `message_start` in the AC7 trace test's closing assertion;
     `content_block_start` → `message_start` in
     `test_returning_to_idle_restores_the_tighter_idle_budget`).

## Acceptance Criteria

- AC1: `content_block_stop` of the current `tool_use` block no longer closes
  a model-wait window its own tool result just opened. Met —
  `test_cpp177_content_block_stop_after_tool_result_does_not_close_the_wait`,
  proved red-before/green-after.
- AC2 (mandatory negative control): a genuinely silent text-only turn still
  dies at `idleTimeoutMs`. Met, unchanged before/after —
  `test_cpp177_text_only_turn_with_no_tool_result_still_dies_at_idle_timeout`.
- AC3: no regression in the existing guardrails suite (cpp#54/#119/#133/
  #145/#168 populations). Met — full suite green, verbatim output in PR
  body.
- AC4: `idleTimeoutMs`/`modelWaitCeilingMs` values themselves are untouched.
  Met — this fix changes only which SSE events close the model-wait window,
  never the ceilings.

## Out of scope

- The origin of the fixed ~355s delay itself (mika#2029; requires a
  timestamped egress log the currently-stale proxy cannot provide).
- The unrelated "still-born" session class (`[init]` received, zero stream
  events, n=3 on 09-03, no recurrence since) — different mechanism, tracked
  on mika#2029 with its own wake condition.

## Verification

```
uv run pytest tests/test_guardrails.py -k cpp177 -v   # the mandatory gate, isolated
uv run pytest -q                                       # full suite
uv run ruff check .
uv run mypy src
./scripts/verify-pipeline.sh
```
→ verbatim output for all of the above in the PR body.

## Frères

- `senara-solutions/mika-platform#2029` — the mika-side substrate for the
  same kill class (stale egress proxy, `dispatch-lib.sh` false diagnosis,
  `NO_PR` pattern); origin of the fixed ~355s delay remains there.
- `senara-solutions/claude-pilot#145` — introduced `_WaitState`/
  `_PRODUCTION_STREAM_EVENTS`/the model-wait window this fix corrects.
- `senara-solutions/claude-pilot#168` — hardened the same `_idle_watchdog`
  loop against a silently-dying watchdog task; untouched here, regression-
  guarded by the full suite.
