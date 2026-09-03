"""Session-level termination guardrails. Port of src/guardrails.ts.

Tracks per-turn state and triggers an abort when stall / empty-response /
idle-timeout thresholds are crossed. Uses a dedicated asyncio Event + Task for
the idle timer so it can be cleanly paused during `can_use_tool` (relay may
take 60-120s) and resumed afterwards.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from .types import (
    GUARDRAIL_DEFAULTS,
    GuardrailAbortReason,
    GuardrailConfig,
    ResolvedGuardrailConfig,
)


class _WaitState(Enum):
    """What the session is waiting for during a silence (cpp#145).

    The watchdog does not measure inactivity: it measures the ABSENCE OF A
    SIGNAL IT EXPECTS. Three different causes produce that silence, and only
    one of them justifies aborting at `idleTimeoutMs`:

    | state           | who is being waited on | abort at idleTimeoutMs? |
    |-----------------|------------------------|-------------------------|
    | IDLE            | nobody                 | yes — the original case |
    | AWAITING_TOOL   | a running tool         | no — its own ceiling    |
    | AWAITING_MODEL  | the next turn's first token | no — its own ceiling |

    Collapsing the three killed six productive sessions on the night of
    2026-08-31 to 09-01. All six share the same last line before the guardrail
    fired — `user message (tool result) received` — i.e. the tool had already
    returned and the session was waiting on the model. `3d5fe1ec` had made
    thirty-three tool calls; it was not stuck, it was killed.
    """

    IDLE = "idle"
    AWAITING_TOOL = "awaiting_tool"
    AWAITING_MODEL = "awaiting_model"


@dataclass(frozen=True)
class TurnBoundaryEvent:
    """Emitted when a logical turn just closed (cpp#10).

    `just_closed_turn` is the 1-indexed turn number that just ENDED (not the
    new turn that's starting). `had_text` / `had_tool_use` / `had_thinking_block`
    summarize what the just-closed turn produced — agent.py reads these to
    decide whether the turn was diagnostically silent and worth logging a
    marker for.
    """

    just_closed_turn: int
    had_text: bool
    had_tool_use: bool
    had_thinking_block: bool


def resolve_guardrail_defaults(config: GuardrailConfig | None) -> ResolvedGuardrailConfig:
    if config is None:
        return GUARDRAIL_DEFAULTS.model_copy()
    return ResolvedGuardrailConfig(
        maxTurns=config.maxTurns if config.maxTurns is not None else GUARDRAIL_DEFAULTS.maxTurns,
        maxBudgetUsd=config.maxBudgetUsd if config.maxBudgetUsd is not None else GUARDRAIL_DEFAULTS.maxBudgetUsd,
        stallThreshold=config.stallThreshold if config.stallThreshold is not None else GUARDRAIL_DEFAULTS.stallThreshold,
        emptyResponseThreshold=config.emptyResponseThreshold if config.emptyResponseThreshold is not None else GUARDRAIL_DEFAULTS.emptyResponseThreshold,
        idleTimeoutMs=config.idleTimeoutMs if config.idleTimeoutMs is not None else GUARDRAIL_DEFAULTS.idleTimeoutMs,
        minTurnsBeforeDetection=config.minTurnsBeforeDetection if config.minTurnsBeforeDetection is not None else GUARDRAIL_DEFAULTS.minTurnsBeforeDetection,
        rateLimitCeilingMs=config.rateLimitCeilingMs if config.rateLimitCeilingMs is not None else GUARDRAIL_DEFAULTS.rateLimitCeilingMs,
        toolWaitCeilingMs=config.toolWaitCeilingMs if config.toolWaitCeilingMs is not None else GUARDRAIL_DEFAULTS.toolWaitCeilingMs,
        modelWaitCeilingMs=config.modelWaitCeilingMs if config.modelWaitCeilingMs is not None else GUARDRAIL_DEFAULTS.modelWaitCeilingMs,
    )


class SessionGuardrails:
    """Turn-boundary and idle-timeout detector.

    The caller must call `dispose()` on session end to cancel pending timers.
    `aborted` is set when any guardrail trips; the caller should check it on
    each loop iteration or propagate cancellation through the SDK client.
    """

    def __init__(self, config: ResolvedGuardrailConfig) -> None:
        self._config = config
        self._turn_count = 0
        self._consecutive_stall_turns = 0
        self._consecutive_empty_turns = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._abort_event = asyncio.Event()
        self._abort_reason: GuardrailAbortReason | None = None
        # Per-turn accumulators for the in-progress turn. The Python claude-agent-sdk
        # emits one AssistantMessage per *content block* (Thinking, Text, ToolUse...),
        # all sharing the same `message_id`. A logical turn is the union of all events
        # carrying the same message_id. Without this grouping, thinking-heavy turns
        # inflate the stall count (claude-pilot-py#4).
        self._current_message_id: str | None = None
        self._current_turn_has_tool: bool = False
        self._current_turn_text_len: int = 0
        # cpp#10: track whether the in-progress turn observed any ThinkingBlock,
        # so the TurnBoundaryEvent for that turn can distinguish "model thought
        # but didn't act" from "SDK emitted a truly empty turn".
        self._current_turn_had_thinking_block: bool = False
        # Tracks whether we speculatively incremented stall for the current turn
        # so we can roll it back if a later content block (same message_id) brings
        # a tool_use.
        self._stall_incremented_for_current_turn: bool = False
        self._empty_incremented_for_current_turn: bool = False
        # cpp#10: guards `close_final_turn()` idempotency — once the final-turn
        # event has been emitted, subsequent calls return None.
        self._final_turn_closed: bool = False
        # mika#940: track whether a `gh pr create` Bash invocation was observed
        # in this session. Read by agent.py post-ResultMessage when
        # CLAUDE_PILOT_REQUIRE_PR=1 (set by dispatch-lib for dev-pilot sessions);
        # absence flips ResultJson.subtype to `pipeline_incomplete` and exits 1.
        # Detection: any ToolUseBlock where name=="Bash" and command contains
        # "gh pr create" (substring match, false-positives accepted per plan).
        self._pr_created: bool = False
        # cpp#119: sticky "currently rate-limited" flag. Set when a rate-limit
        # signal is observed on the stream (a CLI RateLimitEvent with
        # status=="rejected", or an AssistantMessage carrying error=="rate_limit")
        # and reported to `note_rate_limit`. Cleared when the model produces a
        # fresh turn (the retry succeeded → we are producing again) or when a
        # recovered rate-limit signal arrives. Read by `_idle_watchdog`: a stall
        # that fires WHILE we are throttled does not kill the session (cpp#119
        # named it `rate_limited`; cpp#133 makes it non-fatal) — the watchdog
        # instead defers to the SDK's backoff up to `rateLimitCeilingMs`, and
        # only terminates (as `rate_limited`) if the throttle outlasts it.
        self._rate_limited: bool = False
        self._rate_limit_detail: str | None = None
        self._rate_limit_api_status: int | None = None
        # cpp#133: event-loop timestamp when the current throttle window began
        # (first `note_rate_limit(rejected=True)` since the flag was last clear).
        # The idle watchdog measures the backoff wait against it to enforce
        # `rateLimitCeilingMs`. Kept at the EARLIEST arming — re-arming while
        # already throttled does not push the ceiling out — and reset to None
        # whenever the flag clears (progress resumed → a fresh window).
        self._rate_limit_started_at: float | None = None
        # cpp#123: intra-turn liveness. `_last_activity_at` is the event-loop
        # timestamp of the most recent progress signal — a turn boundary, a
        # relay resume, or a content-bearing SDK StreamEvent. `_idle_watchdog`
        # measures against it instead of sleeping a fixed span, so activity
        # rearms the timer at O(1) cost with no task churn (a turn produces
        # thousands of deltas). `_stream_activity_count` is reported in the
        # idle abort detail so a silent session is distinguishable from a
        # producing one straight from the log.
        self._last_activity_at: float = 0.0
        self._stream_activity_count: int = 0
        # cpp#145: what the session is waiting for during the current silence,
        # and since when. The state lives on the INSTANCE, never in the
        # watchdog task's closure: `_reset_idle_timer` cancels and recreates
        # that task on every turn boundary, so a fresh task must be able to
        # read a transition decided before it was created.
        self._wait_state: _WaitState = _WaitState.IDLE
        self._wait_started_at: float | None = None
        self._wait_detail: str | None = None
        # cpp#145: what the last observed signal WAS, and how many content
        # stream events arrived since the current wait window opened. Both are
        # reported in the abort detail (AC5): a message that says "no
        # meaningful progress" while citing 1722 progress events contradicts
        # itself inside its own sentence, and that message is what made
        # mika#2029 take six rounds to read.
        self._last_signal: str | None = None
        self._window_stream_count: int = 0
        self._reset_idle_timer()

    @property
    def config(self) -> ResolvedGuardrailConfig:
        return self._config

    @property
    def turns(self) -> int:
        return self._turn_count

    @property
    def pr_created(self) -> bool:
        """True if any Bash tool_use with `gh pr create` substring was observed
        this session. mika#940 pipeline-completion contract — read by agent.py
        post-ResultMessage when CLAUDE_PILOT_REQUIRE_PR=1."""
        return self._pr_created

    @property
    def aborted(self) -> bool:
        return self._abort_event.is_set()

    @property
    def abort_reason(self) -> GuardrailAbortReason | None:
        return self._abort_reason

    async def wait_aborted(self) -> GuardrailAbortReason:
        """Suspend until a guardrail trips; return the reason."""
        await self._abort_event.wait()
        assert self._abort_reason is not None
        return self._abort_reason

    @property
    def rate_limited(self) -> bool:
        """True while a rate-limit signal is active (observed and not yet
        cleared by resumed progress). cpp#119 — read by the idle watchdog to
        classify a throttled stall distinctly."""
        return self._rate_limited

    @property
    def stream_activity_count(self) -> int:
        """Number of content-bearing SDK stream events observed this session
        (cpp#123). Reported in the `idle_timeout` abort detail."""
        return self._stream_activity_count

    def note_stream_activity(self) -> None:
        """Record intra-turn progress from the SDK message stream (cpp#123).

        `agent.py` sets `include_partial_messages=True`, so the SDK delivers a
        `StreamEvent` per raw Anthropic SSE event throughout a turn. Those
        events are the only evidence that a turn is still producing: the turn
        boundary that `on_assistant_message` keys on does not arrive until the
        turn ENDS.

        Without this signal the idle timer measured "no new turn boundary",
        not "nothing at all from the SDK" as its own contract claims, and a
        turn whose generation ran past `idleTimeoutMs` was aborted mid-flight.

        Deliberately cheap: it moves a deadline and does not touch the
        watchdog task. `agent.py` calls it once per content-bearing stream
        event; keepalives are filtered there, not here.

        Also clears any armed rate-limit flag, for the same reason
        `on_assistant_message` does: content on the wire means a throttle-retry
        succeeded. Stream events carry that proof strictly EARLIER than the
        completed turn does, and rearming the idle timer now keeps a long
        content block alive where the 300s cap used to end it — so without this
        clear, a stream that dies mid-block would be reported `rate_limited`
        with a `resets_at` already in the past.

        The counter is incremented unconditionally; only the deadline needs a
        running loop.
        """
        self._stream_activity_count += 1
        self._clear_rate_limit()
        # cpp#145: content on the wire is the proof that closes BOTH waiting
        # states. A model producing tokens is not waiting on its own first
        # token (`AWAITING_MODEL`), and the SDK does not resume generation
        # while a tool result is still outstanding, so a delta also retires
        # `AWAITING_TOOL`. Nobody is being waited on → back to plain IDLE, and
        # the 300s budget applies again from here.
        self._enter_wait(_WaitState.IDLE, signal="stream event")
        self._window_stream_count += 1
        self._bump_idle_deadline()

    def note_activity(self) -> None:
        """Record non-generation SDK liveness on the message stream (cpp#125).

        A `UserMessage` carries tool results — inbound traffic that proves the
        session is still alive, but is NOT model production. Unlike
        `note_stream_activity` this moves the idle deadline forward WITHOUT
        incrementing the content-stream counter (whose reported meaning is
        "content stream events this session") and WITHOUT clearing the sticky
        rate-limit flag: a tool result is no evidence that a throttled
        generation retry has succeeded. Deliberately cheap — it only moves a
        deadline and never touches the watchdog task.

        cpp#145: it also OPENS the model-wait window. This exact line is the
        last one in all six sessions killed on the night of 2026-08-31 — the
        tool result was delivered, the deadline was pushed, and then 300s of
        nothing while the session waited for the first token of the next turn.
        That silence is a wait, not an idle, and from here it is measured
        against `modelWaitCeilingMs` instead.
        """
        self._enter_wait(_WaitState.AWAITING_MODEL, signal="tool result")
        self._bump_idle_deadline()

    def _enter_wait(
        self, state: _WaitState, *, signal: str, detail: str | None = None
    ) -> None:
        """Record what the session is now waiting for, and on what evidence
        (cpp#145).

        Re-entering the SAME state does not restart its window: a turn that
        emits several tool_use blocks is one wait, and re-anchoring on each
        would push the ceiling out indefinitely — the mistake cpp#133 already
        avoided for the throttle window (`_rate_limit_started_at` is stamped
        only on the transition INTO the state).

        `_window_stream_count` resets on every genuine transition so the abort
        detail can report what arrived in THIS window, distinctly from the
        session cumulative count.
        """
        self._last_signal = signal
        if state is self._wait_state:
            return
        self._wait_state = state
        self._window_stream_count = 0
        self._wait_detail = detail
        self._wait_started_at = None if state is _WaitState.IDLE else self._now()

    def _bump_idle_deadline(self) -> None:
        """Push the idle deadline to now, at O(1) cost (cpp#123/#125).

        Shared by `note_stream_activity` and `note_activity`. The watchdog task
        recomputes its remaining budget against `_last_activity_at` on each wake,
        so moving this timestamp rearms the timer without cancelling/recreating
        the task — no churn across the thousands of deltas a turn emits. A no-op
        outside a running loop (constructor-time defensive guard).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_activity_at = loop.time()

    def note_rate_limit(
        self,
        *,
        rejected: bool,
        detail: str | None = None,
        api_error_status: int = 429,
    ) -> None:
        """Record a rate-limit signal observed on the SDK message stream (cpp#119).

        The Claude Code CLI surfaces throttling on the wire — a `RateLimitEvent`
        whose `rate_limit_info.status == "rejected"` means the subscription
        limit has been hit, and an `AssistantMessage.error == "rate_limit"`
        marks an individual turn refused for the same reason. agent.py observes
        those as they arrive and reports them here, because the terminal
        `ResultMessage.api_error_status` (cpp#54) never arrives when the idle
        guardrail fires between the SDK's silent retries.

        `rejected=True` arms the sticky flag; `rejected=False` (a recovered
        signal — status back to `allowed` / `allowed_warning`) clears it. The
        flag is also cleared whenever the model produces a fresh turn, since
        that means a retry succeeded and we are producing output again.
        """
        if rejected:
            if not self._rate_limited:
                # cpp#133: stamp the start of the throttle window only on the
                # transition into it, so a burst of `rejected` signals during
                # one backoff does not keep pushing the ceiling out.
                self._rate_limit_started_at = self._now()
            self._rate_limited = True
            self._rate_limit_detail = detail
            self._rate_limit_api_status = api_error_status
        else:
            self._clear_rate_limit()

    def _clear_rate_limit(self) -> None:
        self._rate_limited = False
        self._rate_limit_detail = None
        self._rate_limit_api_status = None
        self._rate_limit_started_at = None

    def _now(self) -> float | None:
        """Event-loop clock, or None outside a running loop (cpp#133)."""
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return None

    def on_assistant_message(
        self,
        content: list[dict[str, Any]] | Any,
        message_id: str | None = None,
    ) -> TurnBoundaryEvent | None:
        """Called on each AssistantMessage from the SDK.

        The Python claude-agent-sdk splits a single Claude turn into one event per
        content block, all sharing the same `message_id`. We group by message_id
        to count logical turns correctly (claude-pilot-py#4). When `message_id` is
        None — older SDKs or callers without the field — each call counts as its
        own turn (backward-compatible).

        Stall/empty are evaluated speculatively at turn start (so a 5-turn run of
        text-only events still trips at turn 5, not turn 6). When a later content
        block in the same turn brings a `tool_use`, the speculative increment is
        rolled back.

        cpp#10: returns a `TurnBoundaryEvent` describing the just-closed turn
        whenever this call CROSSES a turn boundary (`message_id` changed from the
        previously-seen one). Returns `None` on same-turn continuations and on
        the very first turn (no prior turn to close). Agent.py uses this to emit
        a per-turn marker so thinking-only turns are still visible in the log.
        """
        blocks = content if isinstance(content, list) else []
        has_tool_use = any(_block_type(b) == "tool_use" for b in blocks)
        has_thinking = any(_block_type(b) == "thinking" for b in blocks)
        text_len = sum(
            len((_block_text(b) or "").strip()) for b in blocks if _block_type(b) == "text"
        )

        # cpp#119: productive output means a throttle-retry succeeded — clear
        # any armed rate-limit flag so a LATER genuine idle stall is not
        # misclassified as `rate_limited`. A refused/empty turn (no text, no
        # tool_use) leaves the flag intact.
        if has_tool_use or text_len > 0:
            self._clear_rate_limit()

        # cpp#145: a turn that ends on a tool_use is waiting for that tool to
        # return; a turn that ends without one is waiting for nobody. Armed
        # HERE rather than in permissions.py on purpose: the relay callback
        # (`pause_idle_timer` / `resume_idle_timer`, permissions.py:1112/:1177)
        # brackets only the relay round-trip, so it sees neither auto-approved
        # tools nor the tool's own execution time — the very window AC3 is
        # about. `has_tool_use` is already computed just above, so this costs
        # one branch and no second walk of the content blocks.
        if has_tool_use:
            self._enter_wait(
                _WaitState.AWAITING_TOOL,
                signal="turn boundary",
                detail=_first_tool_use_name(blocks),
            )
        else:
            self._enter_wait(_WaitState.IDLE, signal="turn boundary")

        # mika#940: PR-creation detection. Scan tool_use blocks for Bash
        # invocations whose command substring includes `gh pr create`. Set
        # once and sticky for the rest of the session. False positives on
        # `gh pr create --help` or string-literal occurrences are accepted
        # per plan §Risks 1 — defense in depth from dispatch-lib's actual
        # PR-existence check on GitHub.
        if not self._pr_created:
            for block in blocks:
                if _block_type(block) != "tool_use":
                    continue
                if _tool_use_name(block) != "Bash":
                    continue
                command = _tool_use_command(block)
                if command and "gh pr create" in command:
                    self._pr_created = True
                    break

        is_continuation = (
            message_id is not None and message_id == self._current_message_id
        )

        if is_continuation:
            # Same logical turn — accumulate evidence about its productivity.
            self._current_turn_text_len += text_len
            if has_thinking and not self._current_turn_had_thinking_block:
                self._current_turn_had_thinking_block = True
            if has_tool_use and not self._current_turn_has_tool:
                # tool_use just arrived in this turn — roll back any speculative
                # stall/empty increments we made when the turn started no-tool.
                self._current_turn_has_tool = True
                if self._stall_incremented_for_current_turn:
                    self._consecutive_stall_turns = max(0, self._consecutive_stall_turns - 1)
                    self._stall_incremented_for_current_turn = False
                if self._empty_incremented_for_current_turn:
                    self._consecutive_empty_turns = max(0, self._consecutive_empty_turns - 1)
                    self._empty_incremented_for_current_turn = False
            return None

        # New turn boundary. Capture the just-closed turn's summary before
        # resetting accumulators (cpp#10). The very first call has nothing to
        # close — `_turn_count == 0` skips event emission.
        boundary_event: TurnBoundaryEvent | None = None
        if self._turn_count > 0:
            boundary_event = TurnBoundaryEvent(
                just_closed_turn=self._turn_count,
                had_text=self._current_turn_text_len > 0,
                had_tool_use=self._current_turn_has_tool,
                had_thinking_block=self._current_turn_had_thinking_block,
            )

        self._turn_count += 1
        self._current_message_id = message_id
        self._current_turn_has_tool = has_tool_use
        self._current_turn_text_len = text_len
        self._current_turn_had_thinking_block = has_thinking
        self._stall_incremented_for_current_turn = False
        self._empty_incremented_for_current_turn = False
        # Reset idle timer on each new turn — even empty ones.
        # Stall/empty detection handles degenerate-content cases. idle_timeout
        # now fires only on GENUINE SDK silence: no stream deltas AND no new
        # turn boundary for the whole idleTimeoutMs window. cpp#123 wired
        # generation deltas to `note_stream_activity`, so this is no longer the
        # weaker "no new turn boundary" / "between turns" predicate that killed
        # a turn mid-generation — the comment now matches what the code does.
        self._reset_idle_timer()

        if self._turn_count < self._config.minTurnsBeforeDetection:
            return boundary_event

        if has_tool_use:
            self._consecutive_stall_turns = 0
            self._consecutive_empty_turns = 0
            return boundary_event

        # No tool use yet → speculative stall increment (may be rolled back if
        # a same-message_id continuation brings tool_use).
        self._consecutive_stall_turns += 1
        self._stall_incremented_for_current_turn = True
        if (
            self._config.stallThreshold > 0
            and self._consecutive_stall_turns >= self._config.stallThreshold
        ):
            self._abort(
                "stall_detected",
                f"{self._consecutive_stall_turns} consecutive turns with no tool calls",
            )
            return boundary_event

        # Empty / trivial text
        if text_len < 10:
            self._consecutive_empty_turns += 1
            self._empty_incremented_for_current_turn = True
            if (
                self._config.emptyResponseThreshold > 0
                and self._consecutive_empty_turns >= self._config.emptyResponseThreshold
            ):
                self._abort(
                    "empty_response",
                    f"{self._consecutive_empty_turns} consecutive trivial responses (<10 chars)",
                )
        else:
            self._consecutive_empty_turns = 0

        return boundary_event

    def close_final_turn(self) -> TurnBoundaryEvent | None:
        """Emit a boundary event for the still-open final turn at session end
        (cpp#10). Called by agent.py from the ResultMessage branch BEFORE
        `_emit_result` so the marker for the last turn lands in the log if it
        was diagnostically silent.

        Idempotent: subsequent calls return `None`.
        """
        if self._final_turn_closed or self._turn_count == 0:
            return None
        event = TurnBoundaryEvent(
            just_closed_turn=self._turn_count,
            had_text=self._current_turn_text_len > 0,
            had_tool_use=self._current_turn_has_tool,
            had_thinking_block=self._current_turn_had_thinking_block,
        )
        self._final_turn_closed = True
        self._current_message_id = None
        return event

    def pause_idle_timer(self) -> None:
        """Cancel any pending idle-timeout task (called before relay)."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def resume_idle_timer(self) -> None:
        """Start a fresh full-duration idle timer (called after relay)."""
        self._reset_idle_timer()

    def dispose(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _reset_idle_timer(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self._config.idleTimeoutMs <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet (constructor called outside async context).
            # The SessionGuardrails is expected to be constructed inside
            # asyncio.run(); this is a defensive no-op.
            return
        self._last_activity_at = loop.time()
        self._idle_task = loop.create_task(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        # cpp#123: deadline-driven rather than a single fixed sleep. Activity
        # pushes `_last_activity_at` forward while this task sleeps, so on wake
        # the remaining budget is recomputed and the task sleeps again. One
        # task per timer arming, regardless of how many deltas arrive.
        #
        # cpp#133: when the idle budget is exhausted WHILE a rate-limit signal
        # (cpp#119) is armed, the session is silent because the bundled SDK is
        # backing off between throttled retries — not because the model stopped
        # producing. Killing it here loses in-flight work to a quota wait (the
        # 2026-08-06 founding incident). So instead of aborting at the idle
        # deadline, the watchdog DEFERS to the SDK's backoff: it keeps the
        # session alive and re-checks each idle window. A resumed stream/turn
        # clears the flag (and rearms the deadline), and the session continues.
        # Only if the throttle wait exceeds `rateLimitCeilingMs` does it finally
        # terminate — as `rate_limited`, never as a misattributed idle_timeout —
        # so a permanently-throttled loop cannot leave a zombie alive forever.
        #
        # cpp#145: the same shape extends to the two WAITING states. When the
        # idle budget is exhausted while the session is waiting on a tool that
        # has not returned, or on the first token of the next turn, that
        # silence is not idleness — it is a wait, and it gets its own, more
        # generous ceiling and its own abort reason. The rate-limit branch is
        # checked FIRST and keeps its behaviour unchanged: a throttled session
        # is better described as `rate_limited` than as waiting on the model,
        # since the reason it produces nothing is the quota wall, not the turn.
        timeout = self._config.idleTimeoutMs / 1000.0
        ceiling = self._config.rateLimitCeilingMs / 1000.0
        try:
            loop = asyncio.get_running_loop()
            while True:
                remaining = self._last_activity_at + timeout - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                if self._rate_limited:
                    # cpp#133: throttled backoff. Defer to the SDK unless the
                    # wait has run past the ceiling, then terminate distinctly.
                    started = self._rate_limit_started_at
                    if started is None:
                        # Armed outside a running loop (defensive); anchor now.
                        started = loop.time()
                        self._rate_limit_started_at = started
                    if ceiling > 0 and loop.time() - started >= ceiling:
                        self._abort_rate_limit_ceiling(started, loop.time())
                        return
                    # Re-check after one idle window: cheap, and bounds how long
                    # a resumed-then-stalled session waits before genuine idle
                    # detection resumes (the flag having cleared on the resumed
                    # activity).
                    await asyncio.sleep(timeout)
                    continue
                state = self._wait_state
                if state is _WaitState.IDLE:
                    break  # genuine idle silence → abort below
                # cpp#145: someone is being waited on. Same deferral shape as
                # the throttle branch — keep the session alive, re-check each
                # idle window, and terminate only at this state's own ceiling,
                # with its own reason. Without the ceiling the wait would be an
                # exemption, and a model that never resumes would leave a
                # session immortal; that is the zombie cpp#133 established we
                # must not create.
                wait_ceiling = self._wait_ceiling_secs(state)
                wait_started = self._wait_started_at
                if wait_started is None:
                    # Entered outside a running loop (defensive); anchor now.
                    wait_started = loop.time()
                    self._wait_started_at = wait_started
                if wait_ceiling > 0 and loop.time() - wait_started >= wait_ceiling:
                    self._abort_wait_ceiling(state, wait_started, loop.time())
                    return
                await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        secs = round(self._config.idleTimeoutMs / 1000)
        # cpp#123: name the observed stream-event count. A session that produced
        # nothing and one that streamed for hours used to render the same line,
        # which is what made mika#2029 take six rounds to diagnose.
        #
        # cpp#145 (AC5): and say what was NOT seen. The old line announced "No
        # meaningful progress" while quoting 1722 progress events — a sentence
        # that contradicts itself. That phrasing is now reserved for the case it
        # actually describes: nothing observed at all since the session started.
        session_total = (
            f"{self._stream_activity_count} content stream events this session"
        )
        if self._last_signal is None:
            detail = f"No meaningful progress for {secs}s: nothing observed since the session started ({session_total})"
        else:
            detail = (
                f"Silent for {secs}s since the last {self._last_signal}; "
                f"nobody outstanding (waiting: none) — "
                f"{self._window_stream_count} content stream events in this window, "
                f"{session_total}"
            )
        self._abort("idle_timeout", detail)

    def _wait_ceiling_secs(self, state: _WaitState) -> float:
        """Ceiling, in seconds, for a waiting state (cpp#145)."""
        if state is _WaitState.AWAITING_TOOL:
            return self._config.toolWaitCeilingMs / 1000.0
        return self._config.modelWaitCeilingMs / 1000.0

    def _abort_wait_ceiling(
        self, state: _WaitState, started: float, now: float
    ) -> None:
        """Terminate a session whose wait outlasted its ceiling (cpp#145).

        The negative control of AC2 and AC7 lands here: a guardrail that can no
        longer kill has been removed, not repaired. A tool that never returns
        and a model that never produces its first token both still die — but as
        `awaiting_tool` / `awaiting_model`, so the operator and dispatch-lib
        read "we waited for X and X never came" instead of the false "the
        session went silent".
        """
        waited = round(now - started)
        ceiling_secs = round(self._wait_ceiling_secs(state))
        session_total = (
            f"{self._stream_activity_count} content stream events this session"
        )
        reason: Literal["awaiting_tool", "awaiting_model"]
        if state is _WaitState.AWAITING_TOOL:
            reason = "awaiting_tool"
            who = f"tool `{self._wait_detail}`" if self._wait_detail else "a tool"
            detail = (
                f"Tool wait exceeded ceiling: no result from {who} for ~{waited}s "
                f"(ceiling {ceiling_secs}s); last signal was a "
                f"{self._last_signal or 'turn boundary'} and nothing has arrived since "
                f"({self._window_stream_count} content stream events in this window, "
                f"{session_total})"
            )
        else:
            reason = "awaiting_model"
            detail = (
                f"Model wait exceeded ceiling: no first token of the next turn for "
                f"~{waited}s (ceiling {ceiling_secs}s); last signal was a "
                f"{self._last_signal or 'tool result'} and the turn never resumed "
                f"({self._window_stream_count} content stream events in this window, "
                f"{session_total})"
            )
        self._abort(reason, detail)

    def _abort_rate_limit_ceiling(self, started: float, now: float) -> None:
        """Terminate a session that has stayed throttled past the ceiling (cpp#133).

        Distinct from a genuine `idle_timeout`: the abort reason is
        `rate_limited` and carries the 429 `api_error_status`, so the operator
        and dispatch-lib see "Anthropic throttled us for longer than we were
        willing to wait", not "the model went silent".
        """
        waited = round(now - started)
        ceiling_secs = round(self._config.rateLimitCeilingMs / 1000)
        detail = (
            f"Rate-limited beyond ceiling: throttled ~{waited}s "
            f"(ceiling {ceiling_secs}s) with no progress; the SDK's backoff "
            "outlasted the pilot's wait budget"
        )
        if self._rate_limit_detail:
            detail = f"{self._rate_limit_detail}; {detail}"
        self._abort("rate_limited", detail)

    def _abort(
        self,
        guardrail: Literal[
            "stall_detected",
            "empty_response",
            "idle_timeout",
            "rate_limited",
            # cpp#145: the two waiting states, reached only at their ceilings.
            "awaiting_tool",
            "awaiting_model",
        ],
        detail: str,
    ) -> None:
        if self._abort_event.is_set():
            return
        self._abort_reason = GuardrailAbortReason(
            guardrail=guardrail,
            turns=self._turn_count,
            detail=detail,
            # cpp#119: carry the API status onto the abort path so agent.py can
            # populate ResultJson.api_error_status even though no terminal
            # ResultMessage (cpp#54's source) ever arrives here.
            api_error_status=(
                self._rate_limit_api_status if guardrail == "rate_limited" else None
            ),
        )
        self.dispose()
        self._abort_event.set()


def _first_tool_use_name(blocks: list[Any]) -> str | None:
    """Name of the first tool_use block in a turn, for the wait detail (cpp#145).

    Best-effort: an unnamed block yields None and the abort message simply says
    "a tool" instead. Never raises — a malformed block must not be able to take
    down the guardrail that exists to bound the session.
    """
    for block in blocks:
        if _block_type(block) == "tool_use":
            return _tool_use_name(block)
    return None


_SDK_BLOCK_CLASS_TO_TYPE: dict[str, str] = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
}


def _block_type(block: Any) -> str | None:
    """Extract a content-block discriminator that works for both dict-shaped
    SDK messages and dataclass / object instances.

    The claude-agent-sdk dataclasses (TextBlock, ThinkingBlock, ToolUseBlock) do
    NOT carry a `type` attribute — the wire-format `type` field is consumed by
    the parser. We map class names back to the Anthropic API type strings.
    """
    if isinstance(block, dict):
        t = block.get("type")
        return t if isinstance(t, str) else None
    t = getattr(block, "type", None)
    if isinstance(t, str):
        return t
    return _SDK_BLOCK_CLASS_TO_TYPE.get(type(block).__name__)


def _block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        text = block.get("text")
        return text if isinstance(text, str) else None
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else None


def _tool_use_name(block: Any) -> str | None:
    """Extract tool name from a tool_use block (mika#940).

    Mirrors `_block_type` / `_block_text` dual-shape handling for dict-shaped
    SDK messages and dataclass / object instances (ToolUseBlock).
    """
    if isinstance(block, dict):
        name = block.get("name")
        return name if isinstance(name, str) else None
    name = getattr(block, "name", None)
    return name if isinstance(name, str) else None


def _tool_use_command(block: Any) -> str | None:
    """Extract the `command` field from a Bash tool_use block's input (mika#940).

    The SDK normalizes Bash tool inputs to `{"command": "..."}` (string),
    matching the documented schema. Returns None if input is missing or not a
    string command.
    """
    input_obj: Any
    if isinstance(block, dict):
        input_obj = block.get("input")
    else:
        input_obj = getattr(block, "input", None)
    if not isinstance(input_obj, dict):
        return None
    command = input_obj.get("command")
    return command if isinstance(command, str) else None
