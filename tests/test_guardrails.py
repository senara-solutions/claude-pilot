"""Guardrail tests — turn boundary detection across SDK content-block events.

The Python claude-agent-sdk emits one AssistantMessage per content block (Thinking,
Text, ToolUse, ...), all sharing the same `message_id` for a single logical Claude
turn. The TS SDK emits one SDKAssistantMessage per logical turn with all blocks
inside. The guardrail must group same-message_id events into a single turn or it
will mis-count stalls — exploding stall count for thinking-heavy turns and tripping
the abort prematurely (claude-pilot-py#4).
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock

from claude_pilot.guardrails import SessionGuardrails, TurnBoundaryEvent
from claude_pilot.types import ResolvedGuardrailConfig


def _config(stall: int = 5, min_turns: int = 0) -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        stallThreshold=stall,
        emptyResponseThreshold=5,
        idleTimeoutMs=300_000,
        minTurnsBeforeDetection=min_turns,
    )


def _think(text: str = "planning") -> ThinkingBlock:
    return ThinkingBlock(thinking=text, signature="sig")


def _text(t: str = "ok") -> TextBlock:
    return TextBlock(text=t)


def _tool(name: str = "Bash", input_data: dict | None = None) -> ToolUseBlock:
    return ToolUseBlock(id="t1", name=name, input=input_data or {"command": "ls"})


@pytest.fixture
def guardrails() -> SessionGuardrails:
    """Fresh guardrail with stall=5, no warmup."""
    return SessionGuardrails(_config())


# ── Turn-boundary tests (claude-pilot-py#4) ──────────────────────────────────


@pytest.mark.asyncio
async def test_consecutive_blocks_with_same_message_id_count_as_one_turn(
    guardrails: SessionGuardrails,
) -> None:
    """The SDK splits one Claude turn across multiple AssistantMessage events
    sharing the same message_id (Thinking, Text, ToolUse). The guardrail must
    treat them as ONE turn."""
    msg_id = "msg_abc"
    # Same-msg_id sequence: thinking → text → tool_use (all part of turn 1)
    guardrails.on_assistant_message([_think()], message_id=msg_id)
    guardrails.on_assistant_message([_text("here is the plan")], message_id=msg_id)
    guardrails.on_assistant_message([_tool()], message_id=msg_id)
    assert guardrails.turns == 1, "All same-message_id events form one turn"
    assert not guardrails.aborted


@pytest.mark.asyncio
async def test_thinking_only_blocks_within_a_turn_do_not_inflate_stall(
    guardrails: SessionGuardrails,
) -> None:
    """A turn containing thinking + text + tool_use should reset the stall
    counter once. Currently the buggy code increments stall for the thinking
    sub-event then text sub-event, then resets on tool_use — net wrong if any
    sub-event is missed."""
    # Five complete turns, each: thinking → text → tool_use (same msg_id within turn)
    for i in range(5):
        mid = f"msg_{i}"
        guardrails.on_assistant_message([_think()], message_id=mid)
        guardrails.on_assistant_message([_text(f"step {i}")], message_id=mid)
        guardrails.on_assistant_message([_tool()], message_id=mid)
    # Five productive turns: never stall
    assert guardrails.turns == 5
    assert not guardrails.aborted, "Productive thinking+text+tool turns must not stall"


@pytest.mark.asyncio
async def test_text_only_distinct_turns_still_trigger_stall(
    guardrails: SessionGuardrails,
) -> None:
    """Preserve existing behavior: 5 text-only turns with DIFFERENT message_ids
    indicate Claude has stopped using tools — stall trip is correct."""
    for i in range(5):
        guardrails.on_assistant_message([_text(f"narrating turn {i}")], message_id=f"msg_{i}")
    assert guardrails.aborted
    assert guardrails.abort_reason is not None
    assert guardrails.abort_reason.guardrail == "stall_detected"


@pytest.mark.asyncio
async def test_message_id_change_marks_new_turn_boundary(
    guardrails: SessionGuardrails,
) -> None:
    """Text-only across two different msg_ids = 2 turns, not 1."""
    guardrails.on_assistant_message([_text("first")], message_id="msg_1")
    guardrails.on_assistant_message([_text("second")], message_id="msg_2")
    assert guardrails.turns == 2


@pytest.mark.asyncio
async def test_missing_message_id_falls_back_to_per_message_turn_count(
    guardrails: SessionGuardrails,
) -> None:
    """Defensive: if the SDK doesn't provide message_id (older versions, edge cases),
    each call counts as its own turn. Backward-compatible with current behavior."""
    guardrails.on_assistant_message([_text("a")])  # no message_id
    guardrails.on_assistant_message([_text("b")])
    assert guardrails.turns == 2


# ── ToolUseBlock recognition (latent bug from the same root cause) ───────────


@pytest.mark.asyncio
async def test_sdk_tool_use_block_dataclass_is_recognized(
    guardrails: SessionGuardrails,
) -> None:
    """SDK dataclass ToolUseBlock has no `type` attribute — the guardrail must
    still recognize it and reset the stall counter. Regression guard for the
    class-name fallback returning `tooluse` (without underscore) which never
    matched `tool_use`."""
    # Prime stall counter with text-only turns
    guardrails.on_assistant_message([_text("a"), _text("b")], message_id="msg_1")
    guardrails.on_assistant_message([_text("c")], message_id="msg_2")
    assert guardrails._consecutive_stall_turns >= 1
    # Now a tool turn must reset it
    guardrails.on_assistant_message([_tool()], message_id="msg_3")
    assert guardrails._consecutive_stall_turns == 0


# ── mika#940: pipeline-completion PR-detection ──────────────────────────────


@pytest.mark.asyncio
async def test_pr_created_starts_false(guardrails: SessionGuardrails) -> None:
    """A fresh SessionGuardrails has pr_created == False (mika#940)."""
    assert guardrails.pr_created is False


@pytest.mark.asyncio
async def test_pr_created_set_by_bash_gh_pr_create(
    guardrails: SessionGuardrails,
) -> None:
    """A Bash tool_use containing `gh pr create` flips pr_created to True
    (mika#940). The dispatch-lib pipeline-completion contract reads this
    after CLAUDE_PILOT_REQUIRE_PR=1 sessions to detect premature-EndTurn."""
    guardrails.on_assistant_message(
        [_tool(name="Bash", input_data={"command": "gh pr create --fill"})],
        message_id="msg_1",
    )
    assert guardrails.pr_created is True


@pytest.mark.asyncio
async def test_pr_created_not_set_by_other_bash(
    guardrails: SessionGuardrails,
) -> None:
    """Bash tool_use without `gh pr create` substring does NOT flip
    pr_created (mika#940). False-negative coverage."""
    guardrails.on_assistant_message(
        [_tool(name="Bash", input_data={"command": "git add -A && git commit -m x"})],
        message_id="msg_1",
    )
    assert guardrails.pr_created is False


@pytest.mark.asyncio
async def test_pr_created_not_set_by_other_tool(
    guardrails: SessionGuardrails,
) -> None:
    """A non-Bash tool_use (e.g. Edit) does NOT flip pr_created even if its
    input string contains `gh pr create` (mika#940). Name-guard coverage."""
    guardrails.on_assistant_message(
        [_tool(name="Edit", input_data={"command": "gh pr create"})],
        message_id="msg_1",
    )
    assert guardrails.pr_created is False


@pytest.mark.asyncio
async def test_pr_created_is_sticky(guardrails: SessionGuardrails) -> None:
    """Once pr_created flips True, subsequent turns without `gh pr create`
    do not reset it (mika#940). The PR-creation contract is per-session,
    not per-turn."""
    guardrails.on_assistant_message(
        [_tool(name="Bash", input_data={"command": "gh pr create --fill"})],
        message_id="msg_1",
    )
    assert guardrails.pr_created is True
    guardrails.on_assistant_message(
        [_tool(name="Bash", input_data={"command": "echo done"})],
        message_id="msg_2",
    )
    assert guardrails.pr_created is True


# ── cpp#10: TurnBoundaryEvent return-value contract ─────────────────────────


@pytest.mark.asyncio
async def test_same_message_id_continuation_returns_none(
    guardrails: SessionGuardrails,
) -> None:
    """Same-msg_id continuation is NOT a turn boundary — returns None (cpp#10)."""
    first = guardrails.on_assistant_message([_think()], message_id="msg_1")
    assert first is None, "Very first turn has no prior turn to close"
    second = guardrails.on_assistant_message([_text("hi")], message_id="msg_1")
    assert second is None, "Same-msg_id continuation must not emit a boundary event"


@pytest.mark.asyncio
async def test_boundary_event_after_thinking_only_turn(
    guardrails: SessionGuardrails,
) -> None:
    """A new message_id after a thinking-only turn yields an event flagging
    `had_thinking_block=True, had_text=False, had_tool_use=False` (cpp#10)."""
    guardrails.on_assistant_message([_think()], message_id="msg_1")
    event = guardrails.on_assistant_message([_think()], message_id="msg_2")
    assert isinstance(event, TurnBoundaryEvent)
    assert event.just_closed_turn == 1
    assert event.had_thinking_block is True
    assert event.had_text is False
    assert event.had_tool_use is False


@pytest.mark.asyncio
async def test_boundary_event_after_text_and_tool_turn(
    guardrails: SessionGuardrails,
) -> None:
    """A new message_id after a text+tool turn yields an event with
    `had_text=True, had_tool_use=True` — _on_boundary will suppress the
    marker (cpp#10)."""
    guardrails.on_assistant_message(
        [_think(), _text("here is the plan"), _tool()],
        message_id="msg_1",
    )
    event = guardrails.on_assistant_message([_text("next")], message_id="msg_2")
    assert isinstance(event, TurnBoundaryEvent)
    assert event.just_closed_turn == 1
    assert event.had_text is True
    assert event.had_tool_use is True
    assert event.had_thinking_block is True


@pytest.mark.asyncio
async def test_close_final_turn_returns_event_then_none(
    guardrails: SessionGuardrails,
) -> None:
    """`close_final_turn()` emits the still-open final turn once, then is
    idempotent (cpp#10)."""
    guardrails.on_assistant_message([_think()], message_id="msg_1")
    first = guardrails.close_final_turn()
    assert isinstance(first, TurnBoundaryEvent)
    assert first.just_closed_turn == 1
    assert first.had_thinking_block is True
    assert first.had_text is False
    assert first.had_tool_use is False
    second = guardrails.close_final_turn()
    assert second is None, "close_final_turn must be idempotent"


@pytest.mark.asyncio
async def test_close_final_turn_with_no_turns(
    guardrails: SessionGuardrails,
) -> None:
    """`close_final_turn()` returns None when no turn has started (cpp#10)."""
    assert guardrails.close_final_turn() is None


@pytest.mark.asyncio
async def test_pr_created_substring_match(guardrails: SessionGuardrails) -> None:
    """Substring match is sufficient (mika#940 plan §Risks 1 accepts false
    positives). `gh pr create` embedded mid-command flips the flag."""
    guardrails.on_assistant_message(
        [
            _tool(
                name="Bash",
                input_data={
                    "command": "cd worktree && gh pr create --title foo && cd .."
                },
            )
        ],
        message_id="msg_1",
    )
    assert guardrails.pr_created is True


# ── Rate-limited stall classification (cpp#119) ──────────────────────────────


def _idle_config(idle_ms: int = 40, ceiling_ms: int = 1) -> ResolvedGuardrailConfig:
    """Config with a very short idle timeout so the watchdog fires promptly in
    tests, and stall/empty detection effectively disabled.

    cpp#133: `ceiling_ms` bounds the throttled-backoff wait. It defaults to 1ms
    so a rate-limited stall still terminates promptly (as `rate_limited`) in the
    cpp#119 classification tests; the cpp#133 tests that assert a session
    *survives* the backoff window pass a generous ceiling explicitly."""
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=idle_ms,
        minTurnsBeforeDetection=0,
        rateLimitCeilingMs=ceiling_ms,
    )


@pytest.mark.asyncio
async def test_rate_limited_stall_classified_as_rate_limited_not_idle_timeout() -> None:
    """cpp#119: when a rate-limit signal is active and the idle watchdog fires,
    the abort is classified `rate_limited` (with api_error_status 429), NOT
    `idle_timeout`. The two conditions were previously collapsed."""
    guardrails = SessionGuardrails(_idle_config())
    # A CLI RateLimitEvent(status="rejected") arrived on the stream — the SDK is
    # now backing off between throttled retries and produces nothing.
    guardrails.note_rate_limit(rejected=True, detail="Anthropic rate limit rejected (429)")

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    # `rate_limited` and `idle_timeout` are mutually exclusive literals — this
    # equality proves the throttled stall is NOT misattributed to idle_timeout.
    assert reason.guardrail == "rate_limited"
    assert reason.api_error_status == 429
    guardrails.dispose()


@pytest.mark.asyncio
async def test_genuine_idle_stall_still_classifies_as_idle_timeout() -> None:
    """cpp#119: with no rate-limit signal active, a stalled session still
    classifies as `idle_timeout` and carries no api_error_status — the existing
    behaviour is preserved."""
    guardrails = SessionGuardrails(_idle_config())

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    assert reason.api_error_status is None
    guardrails.dispose()


@pytest.mark.asyncio
async def test_productive_turn_clears_rate_limit_flag_before_idle() -> None:
    """cpp#119: a productive turn means a throttle-retry succeeded — the sticky
    flag clears, so a LATER genuine idle stall is `idle_timeout`, not
    `rate_limited`."""
    guardrails = SessionGuardrails(_idle_config())
    guardrails.note_rate_limit(rejected=True)
    assert guardrails.rate_limited is True

    # Real output arrives → throttle cleared (and idle timer reset).
    guardrails.on_assistant_message([_text("back to work")], message_id="msg_1")
    assert guardrails.rate_limited is False

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)
    assert reason.guardrail == "idle_timeout"
    assert reason.api_error_status is None
    guardrails.dispose()


@pytest.mark.asyncio
async def test_recovered_rate_limit_signal_clears_flag() -> None:
    """cpp#119: a recovered rate-limit signal (status back to allowed) clears
    the sticky flag."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=10_000))
    guardrails.note_rate_limit(rejected=True)
    assert guardrails.rate_limited is True
    guardrails.note_rate_limit(rejected=False)
    assert guardrails.rate_limited is False
    guardrails.dispose()


# ── Throttled backoff is non-fatal, bounded by a ceiling (cpp#133) ───────────
#
# cpp#119 gave a throttled stall its own NAME (`rate_limited`); the session
# still died. cpp#133 changes the BEHAVIOUR: while the rate-limit flag is armed
# the idle watchdog defers to the SDK's own backoff instead of killing the
# session at the idle deadline, up to `rateLimitCeilingMs`. Genuine no-progress
# stalls (no signal) must still die at `idleTimeoutMs`, and a throttle that
# outlasts the ceiling must still terminate — as `rate_limited`, not idle.


@pytest.mark.asyncio
async def test_throttled_session_not_killed_during_backoff_window() -> None:
    """cpp#133 (AE1): a session with an armed rate-limit signal must NOT be
    aborted when the idle budget elapses — the SDK is backing off between
    throttled retries, not idle. The session stays alive across several idle
    windows and then continues once a retry produces content."""
    # Idle budget 40ms, ceiling far larger than the test span.
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, ceiling_ms=10_000))
    guardrails.note_rate_limit(
        rejected=True, detail="Anthropic rate limit rejected (429)"
    )

    # Wait ~6x the idle budget: the old behaviour would have aborted at 1x. That
    # this session is still alive proves the flag was armed and honoured — with
    # no signal, a 40ms-idle session would already have tripped idle_timeout.
    await asyncio.sleep(0.25)
    assert guardrails.aborted is False, (
        "a session under classified throttling must not be killed by idle_timeout "
        "during the SDK's backoff window"
    )

    # A retry succeeds — content on the wire clears the throttle and rearms the
    # idle timer. The session survives to continue.
    guardrails.note_stream_activity()
    assert guardrails.rate_limited is False
    await asyncio.sleep(0.02)
    assert guardrails.aborted is False, "session must continue after throttle clears"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_genuine_stall_without_signal_still_dies_at_idle_timeout() -> None:
    """cpp#133 back-compat guard: with NO rate-limit signal, a real no-progress
    stall must STILL trip `idle_timeout` promptly — the deferral applies only
    while the throttle flag is armed, never to genuine silence."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, ceiling_ms=10_000))
    # No note_rate_limit call — this is genuine silence, not a backoff.

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    assert reason.api_error_status is None
    guardrails.dispose()


@pytest.mark.asyncio
async def test_throttle_beyond_ceiling_terminates_as_rate_limited() -> None:
    """cpp#133 (AE3): the ceiling bounds the wait. A throttle that outlasts
    `rateLimitCeilingMs` with no progress terminates the session — but with the
    distinct `rate_limited` reason and the 429 status, never a misattributed
    `idle_timeout`, so dispatch-lib can tell a quota wall from a dead model."""
    # Idle 40ms, ceiling 20ms: the throttle wait crosses the ceiling on the
    # first idle deadline.
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, ceiling_ms=20))
    guardrails.note_rate_limit(
        rejected=True, detail="Anthropic rate limit rejected (429)"
    )

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "rate_limited"
    assert reason.api_error_status == 429
    assert "ceiling" in reason.detail.lower(), (
        "the ceiling abort must be self-describing, distinct from a plain stall"
    )
    guardrails.dispose()


# ── Intra-turn stream activity rearms the idle timer (cpp#123) ───────────────
#
# Before cpp#123 the idle timer was rearmed only at a turn boundary (a
# `message_id` change) and around the relay window. `include_partial_messages=
# True` means the SDK delivers StreamEvents throughout a turn, but nothing fed
# them to the guardrail, so a turn whose generation ran past idleTimeoutMs was
# aborted while the model was still producing. Measured signature: session
# duration tracked `turns x ~301s` — every terminal turn burned the whole
# budget (mika#2029).


@pytest.mark.asyncio
async def test_stream_activity_rearms_idle_timer_within_one_turn() -> None:
    """AE1 / R1: a single turn that keeps streaming content past the idle
    budget must NOT abort. No turn boundary occurs for the whole window — only
    the intra-turn activity signal keeps the session alive."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=300))
    guardrails.on_assistant_message([_text("starting the plan")], message_id="msg_1")

    # Stream deltas for ~1.7x the idle budget without ever closing the turn.
    # The per-delta gap (5ms) is 60x under the budget so an event-loop
    # scheduling hiccup on a loaded runner cannot trip the watchdog, while the
    # total span still exceeds it — widening the budget instead would make the
    # test vacuous, since the timer would never expire even with no activity.
    for _ in range(100):
        await asyncio.sleep(0.005)
        guardrails.note_stream_activity()

    assert guardrails.aborted is False, (
        "a turn that is still streaming content must not trip idle_timeout"
    )
    assert guardrails.stream_activity_count == 100
    guardrails.dispose()


@pytest.mark.asyncio
async def test_silence_after_stream_activity_still_aborts() -> None:
    """AE2 / R2: the guardrail must keep detecting true silence. Activity
    extends the deadline; it does not disarm the watchdog."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))
    guardrails.on_assistant_message([_text("starting")], message_id="msg_1")
    guardrails.note_stream_activity()

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_idle_abort_detail_reports_stream_activity_count() -> None:
    """R7: the abort detail names how many content-bearing stream events the
    session saw, so the next reader can tell a producing session from a silent
    one without adding instrumentation. This is the line mika#2029 spent six
    diagnostic rounds not having."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    assert "0 content stream events" in reason.detail
    guardrails.dispose()


@pytest.mark.asyncio
async def test_stream_activity_while_paused_does_not_resurrect_timer() -> None:
    """AE4 / R4: the relay window pauses the timer. Activity arriving while
    paused updates the deadline but must not start a watchdog, and the resume
    grants a full fresh budget rather than inheriting the paused remainder."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=80))
    guardrails.pause_idle_timer()
    guardrails.note_stream_activity()

    # Sleep well past the budget while paused — a paused guardrail never fires.
    await asyncio.sleep(0.25)
    assert guardrails.aborted is False

    guardrails.resume_idle_timer()
    # Resume grants a full budget, so nothing has fired yet a moment later.
    await asyncio.sleep(0.02)
    assert guardrails.aborted is False

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)
    assert reason.guardrail == "idle_timeout"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_note_stream_activity_creates_no_asyncio_task() -> None:
    """R5: rearming is O(1). A turn produces thousands of deltas — cancelling
    and recreating the watchdog task per delta would be task churn, so the
    activity signal only moves a deadline. Asserted structurally rather than by
    timing: the watchdog task object must be identical across many signals."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=10_000))
    tasks_before = len(asyncio.all_tasks())
    watchdog_before = guardrails._idle_task

    for _ in range(200):
        guardrails.note_stream_activity()

    assert len(asyncio.all_tasks()) == tasks_before
    assert guardrails._idle_task is watchdog_before
    guardrails.dispose()


@pytest.mark.asyncio
async def test_stream_activity_clears_the_rate_limit_flag() -> None:
    """Content on the wire means a throttle-retry succeeded, so the sticky
    rate-limit flag must clear — stream events carry that proof strictly
    earlier than a completed turn does.

    Without this, a stream that dies mid-content-block after a recovered
    throttle is reported `rate_limited` (api_error_status 429, a `resets_at`
    already in the past) instead of `idle_timeout`, telling dispatch-lib to
    wait out a session that is actually dead. Rearming the idle timer widens
    that window, since a long content block is no longer capped at 300s.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))
    guardrails.note_rate_limit(rejected=True)
    assert guardrails.rate_limited is True

    guardrails.note_stream_activity()
    assert guardrails.rate_limited is False

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)
    assert reason.guardrail == "idle_timeout"
    assert reason.api_error_status is None
    guardrails.dispose()


# ── Non-generation liveness rearms the idle deadline (cpp#125) ───────────────
#
# `note_activity` is the UserMessage path: a tool result is inbound SDK traffic
# that proves the session is alive, so it rearms the idle deadline — but it is
# NOT model production, so it must not inflate the content-stream counter or
# clear the sticky rate-limit flag the way `note_stream_activity` does.


@pytest.mark.asyncio
async def test_note_activity_rearms_idle_without_counting_as_stream() -> None:
    """cpp#125: repeated `note_activity` past the idle budget keeps the session
    alive (rearm works), yet the content-stream counter stays 0 — a tool result
    is liveness, not model production."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=300))

    # Rearm for ~1.7x the idle budget via the UserMessage path only.
    for _ in range(100):
        await asyncio.sleep(0.005)
        guardrails.note_activity()

    assert guardrails.aborted is False, (
        "inbound tool-result liveness must keep the idle timer rearmed"
    )
    assert guardrails.stream_activity_count == 0, (
        "note_activity must NOT count as a content stream event"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_note_activity_does_not_disarm_the_watchdog() -> None:
    """cpp#125 anti-vacuity: `note_activity` extends the deadline, it does not
    disarm the watchdog. After a single tool result, genuine silence still
    fires `idle_timeout`."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))
    guardrails.note_activity()

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_note_activity_does_not_clear_rate_limit_flag() -> None:
    """cpp#125: unlike `note_stream_activity`, a tool result is no proof a
    throttled generation retry has succeeded — so `note_activity` leaves the
    sticky rate-limit flag intact, and a stall during backoff is still
    classified `rate_limited`."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))
    guardrails.note_rate_limit(rejected=True)
    assert guardrails.rate_limited is True

    guardrails.note_activity()
    assert guardrails.rate_limited is True

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)
    assert reason.guardrail == "rate_limited"
    guardrails.dispose()
