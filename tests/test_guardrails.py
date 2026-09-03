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

from claude_pilot.guardrails import SessionGuardrails, TurnBoundaryEvent, _WaitState
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


def _idle_config(
    idle_ms: int = 40,
    ceiling_ms: int = 1,
    tool_ceiling_ms: int = 10_000,
    model_ceiling_ms: int = 10_000,
) -> ResolvedGuardrailConfig:
    """Config with a very short idle timeout so the watchdog fires promptly in
    tests, and stall/empty detection effectively disabled.

    cpp#133: `ceiling_ms` bounds the throttled-backoff wait. It defaults to 1ms
    so a rate-limited stall still terminates promptly (as `rate_limited`) in the
    cpp#119 classification tests; the cpp#133 tests that assert a session
    *survives* the backoff window pass a generous ceiling explicitly.

    cpp#145: `tool_ceiling_ms` / `model_ceiling_ms` bound the two waiting
    states. They default GENEROUSLY (10s against millisecond idle budgets) so a
    test that means to exercise a wait is never cut short by its own ceiling;
    the tests that assert a wait finally DIES pass a tight ceiling explicitly.
    """
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=idle_ms,
        minTurnsBeforeDetection=0,
        rateLimitCeilingMs=ceiling_ms,
        toolWaitCeilingMs=tool_ceiling_ms,
        modelWaitCeilingMs=model_ceiling_ms,
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
    """cpp#125 anti-vacuity, updated by cpp#145: `note_activity` extends the
    deadline, it does not disarm the watchdog — a session that never resumes
    after a tool result is STILL killed.

    What cpp#145 changes is the reason and the budget, not the fact. A tool
    result opens the model-wait window, so the abort is `awaiting_model` at
    `modelWaitCeilingMs` instead of `idle_timeout` at `idleTimeoutMs`. Asserted
    with a tight model ceiling so the death is observable in test time; the
    negative control of AC7 (a session that never resumes must remain killable)
    lives in `test_model_wait_that_never_resumes_dies_at_the_ceiling` below.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, model_ceiling_ms=20))
    guardrails.note_activity()

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_model", (
        "a tool result opens the model-wait window; the abort must name it"
    )
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


# ── Waiting is not idling (cpp#145) ──────────────────────────────────────────
#
# The watchdog owned ONE counter — "nothing seen for N seconds" — and three
# very different things fed it: the model is mute, a tool is running, the model
# has not yet produced the first token of the next turn. The third killed six
# productive sessions on the night of 2026-08-31 to 09-01.
#
# The fix does not relax anything. `idleTimeoutMs` is unchanged at 300s for
# genuine silence. The two waiting states are told apart, given their own named
# budgets, and given their own abort reasons — each of which must still be able
# to kill, or the guardrail was removed rather than repaired.
#
# TWO MEASURED FACTS shape these tests, and both falsified an earlier design:
#
#   1. In three of the six killed sessions (`3d5fe1ec`, `f26add11`, `e2f0ef97`)
#      the turn-closing trailers `message_delta` / `message_stop` arrive AFTER
#      the tool result. They are members of agent.py's progress set, so a rule
#      of "any progress event means the model resumed" closes the model-wait
#      window at the instant it should open — and those three still die.
#   2. Across 177 real dispatch-to-result pairs in /var/log/claude-pilot, 67
#      genuine production events arrive while a tool is still outstanding. A
#      tool wait held as a scalar state is therefore cleared mid-flight by
#      ordinary generation, and its ceiling becomes dead configuration.
#
# Hence: trailers never claim the model resumed, and outstanding tools are
# COUNTED rather than stated.


def _assert_deadline_advanced(
    guardrails: SessionGuardrails, before: float, signal: str
) -> None:
    """Assert the idle deadline actually moved (cpp#125 lock, restored).

    Mutation testing found that asserting only "the session is still alive"
    stopped proving the rearm once the wait states existed: deleting
    `_bump_idle_deadline()` from `note_activity` left the session alive anyway,
    on the model-wait ceiling, and the whole suite stayed green. The rearm is a
    separate contract from the wait, so it gets a separate, direct assertion.
    The file already asserts on internals for the same reason (see the
    watchdog-task identity check in the cpp#123 block).
    """
    assert guardrails._last_activity_at > before, (
        f"{signal} must move the idle deadline, not merely leave the session alive"
    )


# --- AC1: the cpp#123/#125 rearm signals still work, one test per signal -----
#
# No new production line satisfies AC1: `note_stream_activity` (cpp#123) and
# `note_activity` (cpp#125) already rearm the deadline. What IS new is that
# both now also drive a state machine, so these two tests are the regression
# lock on the rearm surviving that — asserted directly, because the wait states
# would otherwise mask its removal.


@pytest.mark.asyncio
async def test_ac1_stream_event_still_rearms_the_deadline_under_the_state_machine() -> None:
    """AC1, signal 1 of 2 (content stream). Deltas across ~1.7x the idle budget
    keep the session alive, and the deadline demonstrably moves each time."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=300))

    for _ in range(100):
        before = guardrails._last_activity_at
        await asyncio.sleep(0.005)
        guardrails.note_stream_activity("content_block_delta")
        _assert_deadline_advanced(guardrails, before, "a content stream event")

    assert guardrails.aborted is False
    assert guardrails.stream_activity_count == 100
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac1_tool_result_still_rearms_the_deadline_under_the_state_machine() -> None:
    """AC1, signal 2 of 2 (turn advance / tool result). Repeated tool results
    across ~1.7x the idle budget move the deadline every time, without
    inflating the content-stream counter — a tool result is liveness, not model
    production."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=300))

    for _ in range(100):
        before = guardrails._last_activity_at
        await asyncio.sleep(0.005)
        guardrails.note_activity()
        _assert_deadline_advanced(guardrails, before, "a tool result")

    assert guardrails.aborted is False
    assert guardrails.stream_activity_count == 0
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac1_turn_boundary_still_rearms_the_deadline() -> None:
    """AC1 names 'turn advancement' as a progress signal. A bare turn boundary
    with no tool call rearms via `_reset_idle_timer` — covered nowhere else."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=300))
    guardrails.on_assistant_message([_text("first")], message_id="msg_1")
    await asyncio.sleep(0.01)

    before = guardrails._last_activity_at
    guardrails.on_assistant_message([_text("second")], message_id="msg_2")

    _assert_deadline_advanced(guardrails, before, "a turn boundary")
    guardrails.dispose()


# --- AC2: the mandatory negative control ------------------------------------


@pytest.mark.asyncio
async def test_ac2_a_truly_idle_session_is_still_killed_at_the_threshold() -> None:
    """AC2 (negative control, no exception offered). A session with NO stream,
    NO turn, NO tool — nobody outstanding — must still die at `idleTimeoutMs`,
    as `idle_timeout`.

    HOW TO SEE THIS TEST GO RED, as AC2 requires: neutralise the threshold.
    Set `idleTimeoutMs=0` in the config below (`_reset_idle_timer` returns
    early and never arms the watchdog) or make `_idle_watchdog` treat
    `_WaitState.IDLE` like the waiting states instead of breaking out of the
    loop. Either change makes `wait_aborted()` hang and this test fail on the
    2s timeout. Both manipulations were run during review and both are red.
    A guardrail that can no longer kill has been removed, not repaired, and no
    allowlist is offered here.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout"
    assert reason.api_error_status is None
    guardrails.dispose()


# --- AC3: time inside a tool is not idle time -------------------------------


@pytest.mark.asyncio
async def test_ac3_a_running_tool_does_not_feed_the_idle_counter() -> None:
    """AC3: a turn that dispatches a tool is waiting for that tool. Silence for
    2x the idle budget while it runs must NOT kill the session — a six-minute
    `cargo build` is not an idle session."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=10_000))
    guardrails.on_assistant_message([_tool(name="Bash")], message_id="msg_1")

    await asyncio.sleep(0.08)  # 2x the idle budget, with no event of any kind

    assert guardrails.aborted is False, (
        "a session waiting on a running tool must not trip idle_timeout"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac3_production_during_the_tool_window_does_not_retire_the_tool() -> None:
    """AC3, the case a scalar wait state gets wrong — and the one the logs
    forced. Across 177 real dispatch-to-result pairs, 67 production events
    arrive while a tool is still outstanding, so generation and tool execution
    demonstrably overlap on the wire.

    If a delta could clear the tool wait, the tool's remaining runtime would
    fall back to the 300s idle budget and `toolWaitCeilingMs` would be dead
    configuration. Only the tool's own result ends its wait.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=10_000))
    guardrails.on_assistant_message([_tool(name="Bash")], message_id="msg_1")
    # Ordinary generation continues while the tool runs.
    for _ in range(5):
        guardrails.note_stream_activity("content_block_delta")

    await asyncio.sleep(0.08)

    assert guardrails.aborted is False
    assert guardrails._wait_state is _WaitState.AWAITING_TOOL, (
        "a stream event must never retire a tool that has not returned"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac3_negative_a_tool_that_never_returns_dies_at_the_ceiling() -> None:
    """AC3 negative control: the tool wait is a budget, never an exemption. A
    tool that never returns its result must still terminate the session — as
    `awaiting_tool`, so the reader knows what was waited on, and never as a
    misattributed `idle_timeout`."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=20))
    guardrails.on_assistant_message([_tool(name="Bash")], message_id="msg_1")

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_tool"
    assert "ceiling" in reason.detail.lower()
    assert "Bash" in reason.detail, "the message must name the tool it waited on"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac3_a_parallel_tool_batch_is_retired_together() -> None:
    """One turn, three tools, one `UserMessage` carrying three results. The
    batch must retire as a batch: counting one would leave two phantom tools
    outstanding and hold a finished session to the 30-minute tool ceiling."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, model_ceiling_ms=10_000))
    guardrails.on_assistant_message(
        [_tool(name="Bash"), _tool(name="Read"), _tool(name="Edit")],
        message_id="msg_1",
    )
    assert guardrails._wait_state is _WaitState.AWAITING_TOOL

    guardrails.note_activity(tool_results=3)

    assert guardrails._wait_state is _WaitState.AWAITING_MODEL, (
        "all three results arrived; nothing is outstanding but the next turn"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac3_a_partial_batch_keeps_the_remaining_tools_outstanding() -> None:
    """The other half of the batch control. If results trickle back one message
    at a time, a fast tool must not declare a slow sibling finished — that
    would charge the slow one to the model ceiling and mislabel its abort."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=10_000))
    guardrails.on_assistant_message(
        [_tool(name="Bash"), _tool(name="Read")], message_id="msg_1"
    )

    guardrails.note_activity(tool_results=1)

    assert guardrails._wait_state is _WaitState.AWAITING_TOOL
    guardrails.note_activity(tool_results=1)
    assert guardrails._wait_state is _WaitState.AWAITING_MODEL
    guardrails.dispose()


# --- AC7: the lethal window — tool result → first token of the next turn -----


@pytest.mark.asyncio
async def test_ac7_the_aae80d84_trace_no_longer_kills_the_session() -> None:
    """AC7, replaying the trace of `aae80d84` verbatim from the ticket:

        [debug] stream event: message_stop (progress=True)
        [tool:request] Edit: .../send_message.rs
        [tool] Edit: ... -> AUTO
        [debug] user message (tool result) received
        [guardrail] idle_timeout: No meaningful progress for 300s (3844 ...)

    The `Edit` is instantaneous. What took 300s was the FIRST TOKEN OF THE NEXT
    TURN.

    `tool_ceiling_ms` is deliberately TIGHT here. Mutation testing caught the
    earlier version of this test passing with the model-wait window removed
    entirely — the preceding `tool_use` left the session on a generous tool
    ceiling, so survival proved nothing about AC7. With the tool ceiling at
    20ms, survival past the idle budget can only come from AWAITING_MODEL.
    """
    guardrails = SessionGuardrails(
        _idle_config(idle_ms=40, tool_ceiling_ms=20, model_ceiling_ms=10_000)
    )
    guardrails.note_stream_activity("message_stop")
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()  # user message (tool result) received

    await asyncio.sleep(0.08)  # 2x the idle budget of pure silence

    assert guardrails.aborted is False, (
        "the window between a tool result and the next turn's first token is a "
        "wait on the model, not idleness — this is the exact 2026-08-31 mechanism"
    )
    assert guardrails._wait_state is _WaitState.AWAITING_MODEL

    # And the wait closes on the proof that the model resumed.
    guardrails.note_stream_activity("content_block_delta")
    assert guardrails._wait_state is _WaitState.IDLE
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac7_trailers_arriving_after_the_tool_result_do_not_close_the_wait() -> None:
    """The half of AC7 the first implementation got wrong, and the reason three
    of the six sessions would still have died.

    In `3d5fe1ec`, `f26add11` and `e2f0ef97` the SDK delivers the old turn's
    `message_delta` / `message_stop` AFTER the tool result. Both are progress
    events for the purpose of rearming the deadline (cpp#123, unchanged), but
    neither is evidence that the NEXT turn started. `message_stop` means the
    turn ended; reading it as "the model is producing" states the opposite of
    what it says.
    """
    guardrails = SessionGuardrails(
        _idle_config(idle_ms=40, tool_ceiling_ms=20, model_ceiling_ms=10_000)
    )
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()  # tool result
    # The exact tail of 3d5fe1ec, in order.
    guardrails.note_stream_activity("message_delta")
    guardrails.note_stream_activity("message_stop")

    assert guardrails._wait_state is _WaitState.AWAITING_MODEL, (
        "turn-closing trailers must not claim the next turn began"
    )

    await asyncio.sleep(0.08)

    assert guardrails.aborted is False
    guardrails.dispose()


@pytest.mark.asyncio
async def test_model_wait_that_never_resumes_dies_at_the_ceiling() -> None:
    """AC7 negative control: a session whose last event is a tool result and
    which NEVER resumes must remain killable. It dies at `modelWaitCeilingMs`,
    with the reason `awaiting_model` — so dispatch-lib and the operator read
    "the model never came back", not "the session went quiet"."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, model_ceiling_ms=20))
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_model"
    assert "ceiling" in reason.detail.lower()
    guardrails.dispose()


@pytest.mark.asyncio
async def test_returning_to_idle_restores_the_tighter_idle_budget() -> None:
    """A session that leaves a wait must go back onto the 300s budget, not stay
    on the generous ceiling. Without this, a session stuck in AWAITING_MODEL
    would look identical to a healthy one for fifteen minutes."""
    guardrails = SessionGuardrails(
        _idle_config(idle_ms=40, tool_ceiling_ms=10_000, model_ceiling_ms=10_000)
    )
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()
    assert guardrails._wait_state is _WaitState.AWAITING_MODEL

    guardrails.note_stream_activity("content_block_start")  # the next turn begins
    assert guardrails._wait_state is _WaitState.IDLE

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "idle_timeout", (
        "back in IDLE, silence is silence again and the 300s budget applies"
    )
    guardrails.dispose()


# --- AC4: replay of the five named sessions, fixtures frozen in this file ----
#
# Sequences are reconstructed from `/var/log/claude-pilot/*.stderr` and FROZEN
# HERE — never re-read at run time, because the logs rotate. Each entry is
# (session, tool calls, content stream events, trailing trailers after the last
# tool result), the first three taken from the counts in the ticket body and
# the fourth from the sessions' actual tails.

_KILLED_SESSIONS: list[tuple[str, int, int, tuple[str, ...]]] = [
    # message_delta + message_stop arrive AFTER the tool result.
    ("3d5fe1ec", 33, 1722, ("message_delta", "message_stop")),
    ("f26add11", 24, 1422, ("message_stop",)),
    ("e2f0ef97", 16, 651, ("message_delta", "message_stop")),
    # These three end ON the tool result, as the ticket body describes.
    ("c56a973e", 15, 1031, ()),
    # c5201301 is the boundary case AC4 asks to settle explicitly. Its 72
    # stream events and 2 tool calls make it look inert next to the others —
    # but inertness is not what killed it. It died in the same model-wait
    # window. A barely-productive session that is waiting on the model is still
    # waiting on the model. It is SAVED, for the same reason they are.
    ("c5201301", 2, 72, ()),
]


@pytest.mark.parametrize(
    ("session", "tool_calls", "stream_events", "trailers"), _KILLED_SESSIONS
)
@pytest.mark.asyncio
async def test_ac4_the_five_killed_sessions_survive_the_revised_heuristic(
    session: str, tool_calls: int, stream_events: int, trailers: tuple[str, ...]
) -> None:
    """AC4: replay each killed session's real shape — N tool cycles carrying M
    content stream events, ending on a tool result and that session's actual
    trailers — then hold silence for 2x the idle budget. None may be killed.

    Two deliberate anti-vacuity measures, both added after mutation testing
    showed the first version passing with the fix removed:
      * the tool ceiling is TIGHT, so survival cannot come from a lingering
        tool wait;
      * real idle windows elapse DURING the replay, so the watchdog actually
        wakes into the wait branch instead of the whole history collapsing into
        one event-loop tick.
    """
    guardrails = SessionGuardrails(
        _idle_config(idle_ms=20, tool_ceiling_ms=40, model_ceiling_ms=10_000)
    )

    per_cycle = max(1, stream_events // tool_calls)
    emitted = 0
    # Cycles are capped so the test stays under a second; the REAL counts are
    # still emitted in full below, they simply do not each get their own sleep.
    sleeping_cycles = min(tool_calls, 8)
    for i in range(tool_calls):
        for _ in range(per_cycle):
            if emitted < stream_events:
                guardrails.note_stream_activity("content_block_delta")
                emitted += 1
        guardrails.on_assistant_message([_tool(name="Bash")], message_id=f"msg_{i}")
        guardrails.note_activity()
        if i < sleeping_cycles:
            # Let a full idle window expire so the watchdog wakes, finds a wait
            # state, and defers — the behaviour under test.
            await asyncio.sleep(0.03)
    for _ in range(stream_events - emitted):
        guardrails.note_stream_activity("content_block_delta")
    # The session's real final state: a delivered tool result, then whatever
    # trailers the SDK still had queued for the turn that is ending.
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_final")
    guardrails.note_activity()
    for trailer in trailers:
        guardrails.note_stream_activity(trailer)

    await asyncio.sleep(0.06)

    assert guardrails.aborted is False, (
        f"session {session} made {tool_calls} tool calls and produced "
        f"{stream_events} stream events — it was working, not idling"
    )
    assert guardrails._wait_state is _WaitState.AWAITING_MODEL
    assert guardrails.stream_activity_count == stream_events + len(trailers)
    guardrails.dispose()


# --- AC5: the message says what it measured AND what it did not see ---------


@pytest.mark.asyncio
async def test_ac5_the_idle_message_still_names_the_session_stream_count() -> None:
    """AC5: the cumulative counter stays — it is useful. What changes is that
    it stops being the only thing quoted next to the word "no"."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert "0 content stream events this session" in reason.detail
    assert "No meaningful progress" in reason.detail, (
        "with nothing ever observed, 'no meaningful progress' is the honest line"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac5_the_message_names_reason_elapsed_last_signal_and_window() -> None:
    """AC5, the four positive elements plus the negative one.

    The budget is a full second, not milliseconds, so the elapsed figure is a
    non-degenerate "1s": at millisecond budgets every elapsed value rendered
    "0s" and the assertion passed on a literal that would survive any bug.
    The last-signal assertion quotes the whole phrase for the same reason — a
    bare "stream event" is also a substring of "content stream events this
    session", so it could never fail.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=1_000))
    guardrails.on_assistant_message([_text("producing")], message_id="msg_1")
    for _ in range(7):
        guardrails.note_stream_activity("content_block_delta")

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=5.0)

    # 1. the reason
    assert reason.guardrail == "idle_timeout"
    # 2. the time since the last signal
    assert "Silent for 1s" in reason.detail
    # 3. the NATURE of that last signal
    assert "since the last stream event" in reason.detail
    # 4. the window count alongside the session cumulative
    assert "7 content stream events in this window" in reason.detail
    assert "7 content stream events this session" in reason.detail
    # 5. the negative assertion: a session that produced must not be told it
    #    made "no meaningful progress" — that is the self-contradicting line
    #    that announced "no progress" while quoting 1722 progress events.
    assert "No meaningful progress" not in reason.detail
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac5_a_wait_abort_names_what_it_waited_for() -> None:
    """AC5 for the two new reasons: a ceiling abort must say who was waited on
    and that they never came, not merely that time passed."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, model_ceiling_ms=20))
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_model"
    assert "first token" in reason.detail
    # The whole phrase, not the bare words: "tool result" alone also appears in
    # the template's own fallback literal, so the loose form could not fail.
    assert "last signal was a tool result" in reason.detail
    assert "content stream events this session" in reason.detail
    assert "No meaningful progress" not in reason.detail
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac5_a_tool_ceiling_abort_names_how_many_are_outstanding() -> None:
    """An operator reading `awaiting_tool` needs to know whether one tool hung
    or a batch did — the difference between a slow build and a lost batch."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=20))
    guardrails.on_assistant_message(
        [_tool(name="Bash"), _tool(name="Read")], message_id="msg_1"
    )

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_tool"
    assert "2 tool call(s) still outstanding" in reason.detail
    assert "Bash" in reason.detail, "anchored on the first tool of the turn"
    guardrails.dispose()


@pytest.mark.asyncio
async def test_ac5_an_unnamed_tool_falls_back_without_breaking_the_message() -> None:
    """A tool_use block the walker cannot name must degrade to "a tool", never
    to a crash inside the guardrail that exists to bound the session."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, tool_ceiling_ms=20))
    guardrails.on_assistant_message([{"type": "tool_use"}], message_id="msg_1")

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "awaiting_tool"
    assert "a tool" in reason.detail
    guardrails.dispose()


# --- Precedence and anchoring ------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_still_outranks_the_model_wait() -> None:
    """cpp#133 must keep its diagnosis. When a session is BOTH throttled and
    waiting on the model, the reason it produces nothing is the quota wall —
    `rate_limited` (with its 429) is the more useful classification, and the
    cpp#133 branch is therefore checked first in the watchdog."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=40, ceiling_ms=20))
    guardrails.note_rate_limit(rejected=True, detail="Anthropic rate limit rejected (429)")
    guardrails.note_activity()  # also opens the model-wait window

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=2.0)

    assert reason.guardrail == "rate_limited"
    assert reason.api_error_status == 429
    guardrails.dispose()


@pytest.mark.asyncio
async def test_a_second_tool_in_the_same_wait_does_not_push_the_ceiling_out() -> None:
    """The window is anchored on the first outstanding tool and stays there —
    the same discipline cpp#133 applied to `_rate_limit_started_at`. Otherwise
    a turn dispatching tools in a loop would keep its own ceiling permanently
    out of reach and recreate the zombie.

    Margins follow this file's convention (see the cpp#123 block): the elapsed
    span before the second dispatch is a small fraction of the ceiling, so a
    loaded runner cannot make the test pass for the wrong reason, while the
    ceiling is still crossed well within the timeout.
    """
    guardrails = SessionGuardrails(_idle_config(idle_ms=20, tool_ceiling_ms=600))
    guardrails.on_assistant_message([_tool(name="Bash")], message_id="msg_1")
    await asyncio.sleep(0.01)
    # Same turn, another tool_use block: must NOT re-anchor the wait.
    guardrails.on_assistant_message([_tool(name="Read")], message_id="msg_1")

    reason = await asyncio.wait_for(guardrails.wait_aborted(), timeout=3.0)

    assert reason.guardrail == "awaiting_tool"
    assert "Bash" in reason.detail, (
        "the wait is still the one opened by the first tool_use of the turn"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_the_relay_window_is_not_charged_to_an_open_wait() -> None:
    """The relay round-trip is time the guardrail explicitly does not measure —
    that is what `pause_idle_timer` is for. Charging it against a wait ceiling
    would contradict the contract the pause exists to honour."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=20, tool_ceiling_ms=200))
    guardrails.on_assistant_message([_tool(name="Bash")], message_id="msg_1")

    guardrails.pause_idle_timer()
    await asyncio.sleep(0.15)  # a slow relay, most of the tool ceiling
    guardrails.resume_idle_timer()

    await asyncio.sleep(0.1)
    assert guardrails.aborted is False, (
        "the paused relay window must not have consumed the tool's budget"
    )
    guardrails.dispose()


@pytest.mark.asyncio
async def test_a_zero_ceiling_is_documented_as_unbounded_and_behaves_that_way() -> None:
    """`0` means "no ceiling", inherited from `rateLimitCeilingMs`. It is the
    ONE configuration in which the watchdog cannot terminate a waiting session,
    so it is pinned by a test and named explicitly in the session header rather
    than left to be discovered in production."""
    guardrails = SessionGuardrails(_idle_config(idle_ms=20, model_ceiling_ms=0))
    guardrails.on_assistant_message([_tool(name="Edit")], message_id="msg_1")
    guardrails.note_activity()

    await asyncio.sleep(0.15)  # many idle windows

    assert guardrails.aborted is False, "0 means wait indefinitely, by contract"
    guardrails.dispose()
