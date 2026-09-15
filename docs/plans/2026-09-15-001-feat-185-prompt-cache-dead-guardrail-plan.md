---
title: Prompt-Cache-Dead Guardrail (D1) - Plan
type: fix
date: 2026-09-15
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# Prompt-Cache-Dead Guardrail (D1) - Plan

## Goal Capsule

Objective: `senara-solutions/claude-pilot#185` D1 ONLY. D2 (request/response
body capture for diffing two consecutive requests) is explicitly OUT OF
SCOPE — samidarko's latest comment on the issue confirms the P0 root cause
(#2313: an MPC-side relay bug forwarding `Connection: keep-alive`, causing
360s/turn hangs) is already fixed upstream (mika#2316, proven by 1-2s turns
and `cache_read>0` in the fixed sandbox). D1 survives that fix as
DEFENSE-IN-DEPTH: build a detector so that if a sandboxed pilot's prompt
cache dies for some OTHER, not-yet-seen reason, the session stops cleanly
instead of silently burning 100-250k uncached Opus tokens every turn.

Means: (1) per-response logging of `cache_read_input_tokens` /
`cache_creation_input_tokens`, reusing the EXISTING turn-boundary plumbing
`on_assistant_message` already has (cpp#10/#145/#177's `TurnBoundaryEvent`) —
no new stream path; (2) a new, additive `prompt_cache_dead` guardrail
reason, declared exactly like `watchdog_error` (cpp#168) /
`rate_limited` (cpp#119) / `awaiting_tool`/`awaiting_model` (cpp#145) before
it, in `guardrails.py`'s `_abort()` `Literal` and `types.py`'s
`GuardrailAbortReason.guardrail` `Literal`.

Authority hierarchy: `senara-solutions/claude-pilot#185` body + samidarko's
P0-resolved comment > this plan > implementer judgment. The issue's own
numbers ("3 tours consécutifs", "> ~50k") are taken as the working
thresholds; both are implemented as named constants (not config fields —
see Judgment calls below) so a future retune is a one-line change.

Stop conditions: none that halt. No pilot dispatch, no other repo touched
(mika untouched, per the dispatching instruction).

Execution profile: single-repo (`claude-pilot`), four source files
(`guardrails.py`, `types.py`, `agent.py`, `ui.py`) plus two test files (one
extended, one new).

Tail ownership: implementer opens the PR through green CI (gated, no
merge); MPC reviews/merges.

## Why this is additive, and why it does not chase the fixed root cause

The P0 investigation (issue body, samidarko's comment) already traced
`cache_read=0` + 100-250k `cache_creation` every turn to a SPECIFIC
mechanism (MPC relay forwarding `Connection: keep-alive` into the sandbox,
causing the SDK's own retry/reconnect path to re-send the full context) —
and that mechanism is fixed. D1 does not re-diagnose it. It exists because
"the cache died" is a SYMPTOM with more than one possible cause, and a
guardrail that only fires when the fixed cause recurs is not
defense-in-depth, it is a duplicate of the fix. So D1 is written against the
symptom (`cache_read==0` with substantial `cache_creation`, sustained), not
against the (now-closed) mechanism, and detects it regardless of which
future bug produces it.

## Guardrail design: threshold + reset-on-hit

Two conditions, BOTH required, for a turn to count as a "substantial miss":

1. `cache_read_input_tokens == 0` — a genuine cache miss, nothing was read.
2. `cache_creation_input_tokens > PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS`
   (50,000, a named constant in `guardrails.py`) — a real prefix that
   SHOULD have been cached, not a short prompt that was never going to
   create a cacheable prefix worth reading back.

The guardrail trips (`prompt_cache_dead`, status `error`) when
`PROMPT_CACHE_DEAD_CONSECUTIVE_MISSES` (3, a named constant) turns in a row
BOTH meet that bar. Any turn that does NOT — a genuine hit
(`cache_read_input_tokens > 0`), a miss too small to be evidence, or a turn
with no usage data at all — resets the streak to zero. This is one rule
covering all three reset cases, not three special cases: "the cache is
demonstrably NOT permanently dead" is exactly what a hit or a
too-small-to-matter miss both prove, whatever the actual reason.

Evaluation timing: cache token counts are input-side and known at a
response's FIRST token (unlike text length or tool-use, which genuinely
accumulate across the SDK's per-content-block `AssistantMessage` events
sharing one `message_id` — see `guardrails.py`'s existing continuation-
grouping doctrine, cpp#10/claude-pilot-py#4). So `_maybe_evaluate_cache_dead_
guardrail` evaluates the CURRENT turn as soon as both fields are known
(usually its first event) rather than deferring to the next turn's
boundary the way `had_text`/`had_tool_use` are — this bounds the worst case
to exactly 3 turns, not 4, and needs no rollback mechanism (unlike stall/
empty detection, which speculates and un-does).

## Critical correctness: the false-positive guards, and why they hold together

The issue names two false-positive risks explicitly, and this plan treats
both as hard requirements, verified by dedicated tests:

**Guard 1 — the mandatory first-turn miss.** Turn 1 always misses (there is
nothing to read yet). A LONE miss must never trip. Handled structurally by
requiring 3 CONSECUTIVE substantial misses — never by special-casing the
turn number — so a session that misses once and then hits is
indistinguishable, at the counter, from one that never missed at all.
Test: `test_first_turn_miss_then_hit_does_not_trip`.

**Guard 2 — compaction / warm-up shape.** The two "OK" (non-P0) sessions the
original investigation measured showed on the order of 29-30 consecutive
cache misses before hits started appearing from ~300k context onward — a
LOT more than 3. Read naively, a bare "3 consecutive misses" rule would have
tripped on one of those healthy sessions long before turn 30. The
resolution is the threshold gate, not the consecutive-count alone: early in
a growing-context session, a miss's `cache_creation_input_tokens` is small
(a short exchange has not yet built a 50k+-token prefix), so those early
misses fail condition 2 and are never counted as evidence either way. By
the time context is large enough that a miss WOULD create a substantial
`cache_creation`, a healthy session's caching has engaged (or engages within
a turn or two) and produces a hit — resetting the streak before it reaches
3. A session where it does NOT engage within 3 such turns is exactly the
population this guardrail exists to catch. Tests:
`test_healthy_session_survives_many_misses_broken_up_by_hits` (a
representative broken-up-by-hits shape) and
`test_three_consecutive_small_misses_do_not_trip` (the threshold gate in
isolation, arbitrarily many small misses, never trips).

Both guards reduce to the SAME implementation rule (reset-on-anything-that-
isn't-a-qualifying-miss), which is what makes them composable rather than
two bolted-on special cases.

## Implementation units

1. **`guardrails.py`** — module-level constants
   `PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS = 50_000` and
   `PROMPT_CACHE_DEAD_CONSECUTIVE_MISSES = 3`; `_usage_int()` helper (defensive
   extraction from the untyped SDK `usage` dict); `TurnBoundaryEvent` gains
   `cache_read_input_tokens` / `cache_creation_input_tokens` (both
   `int | None`, defaulted, so existing callers/tests keep constructing it
   unchanged); `SessionGuardrails.on_assistant_message` gains an optional
   `usage: dict[str, Any] | None = None` parameter and per-turn
   `_current_turn_cache_read` / `_current_turn_cache_creation` /
   `_cache_dead_evaluated_for_current_turn` state; new private method
   `_maybe_evaluate_cache_dead_guardrail`; `_abort`'s `guardrail` `Literal`
   gains `"prompt_cache_dead"`; `close_final_turn` also reports the final
   turn's cache reading (for the last turn's `[cache]` log line — it is
   NOT re-evaluated for the trip, since that already happened when the
   turn started).
2. **`types.py`** — `GuardrailAbortReason.guardrail` `Literal` gains
   `"prompt_cache_dead"`, additive per the existing pattern; `ResultJson`'s
   subtype-enumeration docstring updated to list it.
3. **`agent.py`** — the `AssistantMessage` branch passes
   `usage=getattr(message, "usage", None)` into `on_assistant_message`;
   both `TurnBoundaryEvent` call sites (the turn-boundary one and
   `close_final_turn`'s) call the new `log_cache_usage` on every closed
   turn, not just diagnostically silent ones (`_on_boundary`'s existing
   filter stays scoped to its own concern).
4. **`ui.py`** — `log_cache_usage(turn, cache_read, cache_creation)`: a
   stable `[cache]` line naming both raw fields verbatim (`?` for a missing
   reading, never `0`, so an absent reading is never confused with a
   genuine miss); `log_guardrail_config` gains an unconditional
   `promptCacheDead=3x>50000tok` segment — unlike every other segment in
   that header, this guardrail has no on/off switch, so it is always named.
5. **Tests** — `tests/test_guardrails.py` (extended): the 3-miss trip, the
   2-miss non-trip, both false-positive guards, a missing-usage defensive
   case, the one-evaluation-per-turn case (a turn spanning several
   content-block events must not be double-counted), and two tests pinning
   `TurnBoundaryEvent`/`close_final_turn`'s cache fields.
   `tests/test_ui.py` (new — no file of this name existed before): the
   `[cache]` line's exact shape, the `?`-for-missing-data rendering, and the
   config-header segment.

## Judgment calls (flagged, not hidden)

- **Threshold value (50,000) and consecutive-miss count (3) are the
  ISSUE'S OWN numbers**, taken verbatim rather than independently derived —
  the issue states them as the working values ("> ~50k", "3 tours
  consécutifs") and this plan implements exactly that, as named constants
  rather than `ResolvedGuardrailConfig` fields (the task instruction is
  explicit: "make it a named constant"). This is a smaller surface than the
  fully-configurable ceilings other guardrails carry
  (`toolWaitCeilingMs`/`modelWaitCeilingMs`), and deliberately so — D1 is a
  detector for a fixed root cause, not a tunable production knob yet. If
  production experience shows either number needs retuning (e.g. the
  29-30-miss healthy sessions turn out to sometimes carry a
  >50k-token miss without an intervening hit for 3+ turns), the fix is a
  one-line constant change, not a schema migration.
- **Evaluate-at-turn-start, not evaluate-at-turn-close.** Chosen over
  mirroring stall/empty detection's "speculate at start, roll back on a
  later same-turn tool_use" shape, because cache counts do not need
  rolling back (they are known, not accumulating) — evaluating at start
  bounds the worst case to 3 turns instead of 4 and is simpler. Flagged
  because it is a genuine architectural choice, not a forced one: the
  SDK's per-block `usage` re-attachment behavior for a single turn was
  inferred from `claude_agent_sdk._internal.message_parser` (`usage=
  data["message"].get("usage")`) rather than measured against a live
  multi-block turn, so `_maybe_evaluate_cache_dead_guardrail`'s
  once-per-turn latch is deliberately tolerant of usage arriving on ANY
  block of the turn, not just the first, in case that inference is wrong
  in some SDK version.
- **`close_final_turn` reports but does not re-trip.** The still-open final
  turn's cache reading was already evaluated for the trip when that turn
  started (`on_assistant_message`'s new-turn branch); `close_final_turn`
  only surfaces it for the log line. Re-running the trip check there would
  risk setting `abort_reason` on a session whose `ResultMessage` already
  reported ordinary success — a confusing, purposeless state — so it
  deliberately does not.
- **No config plumbing.** `GuardrailConfig`/`ResolvedGuardrailConfig` are
  untouched. Consistent with "make it a named constant," but worth flagging
  explicitly since every other post-cpp#119 guardrail in this file (rate
  limit, tool/model wait ceilings) IS configurable. A future ticket could
  promote these two constants to config fields with defaults equal to the
  values here — additive, not a rename — if operational experience calls
  for it.

## Out of scope

D2 (request/response body capture, e.g. `ANTHROPIC_LOG_FILE`-based) is
explicitly deferred per the issue body and the dispatching instruction: the
root cause is fixed, so the diff D2 would enable is no longer needed to
close this incident. Nothing in this PR captures request/response bodies.

## Verification

```
uv run pytest tests/test_guardrails.py tests/test_ui.py -q
```
→ 81 passed.

```
uv run pytest -q                 # full suite
uv run ruff check .
uv run mypy src
./scripts/verify-pipeline.sh
```

Anti-vacuity (verified by hand during implementation, not committed):
disabling the trip condition inside `_maybe_evaluate_cache_dead_guardrail`
makes `test_three_consecutive_substantial_misses_trip_prompt_cache_dead` and
`test_cache_dead_survives_across_content_blocks_of_one_turn` fail (`assert
guardrails.aborted` → `assert False`); restoring it makes both pass again.

## Frères

`senara-solutions/claude-pilot#54` (`GuardrailAbortReason` shape),
`#119`/`#133` (`rate_limited` + its ceiling — the pattern this PR's
`Literal` addition mirrors), `#144` (`blocked_on_operator_input` — another
additive `ResultJson` subtype), `#145`/`#177` (turn-boundary /
continuation-grouping plumbing this PR reuses unchanged), `#168`
(`watchdog_error` — the most recent precedent for "additive guardrail
reason, declared in exactly two `Literal`s"). `#2313`/mika#2316 (the P0 this
guardrail is defense-in-depth for, not a re-fix of).
