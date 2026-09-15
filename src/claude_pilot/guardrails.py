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
    2026-08-31 to 09-01. `3d5fe1ec` had made thirty-three tool calls; it was
    not stuck, it was killed.

    **What the logs actually show, and why the first version of this fix was
    only half a fix.** The ticket says all six sessions end on the same line,
    `user message (tool result) received`. That is true for three of them
    (`c56a973e`, `c5201301`, `aae80d84`). In the other three (`3d5fe1ec`,
    `f26add11`, `e2f0ef97`) the SDK delivers the OLD turn's closing trailers —
    `message_delta`, `message_stop` — AFTER the tool result. Both are members
    of `_PROGRESS_STREAM_EVENT_TYPES`, so a naive "any stream event means the
    model is producing" rule closes the model-wait window at the exact instant
    it should open, and those three sessions still die at 300s.

    A trailer is not production. `message_stop` means "the turn ENDED"; taken
    as proof that nobody is being waited on, it says the opposite of the truth.
    So the state machine keys on which SSE event arrived
    (`_PRODUCTION_STREAM_EVENTS`), not on the coarse progress flag.

    The tool wait needs more than that. Measured over 177 real
    dispatch-to-result pairs in `/var/log/claude-pilot`, **67 genuine
    production events arrive while a tool is still outstanding** — so a tool
    wait held as a scalar state gets cleared mid-flight by ordinary generation,
    and `toolWaitCeilingMs` becomes dead configuration. The outstanding tools
    are therefore COUNTED (`_pending_tool_uses`), and no stream event of any
    kind can retire a tool that has not returned.
    """

    IDLE = "idle"
    AWAITING_TOOL = "awaiting_tool"
    AWAITING_MODEL = "awaiting_model"


# cpp#145/cpp#177: the SSE events that prove the model is PRODUCING, as opposed
# to everything else agent.py forwards here for LIVENESS (rearming the idle
# deadline via `_bump_idle_deadline()`, unconditionally, regardless of this
# set). agent.py's `_PROGRESS_STREAM_EVENT_TYPES` (cpp#123) deliberately
# includes all six raw SSE event types — for rearming the deadline they are
# all liveness, and that stays true. Only `message_start` is production HERE,
# because "the deadline may move" and "the next turn started" are different
# claims, and only `message_start` proves the second one.
#
# cpp#145 first cut this down from "any stream event" to excluding the
# turn-closing trailers `message_delta` / `message_stop` (a trailer of the
# turn that just ENDED is not evidence the next one began). cpp#177 measured
# that the cut stopped one event short: `content_block_stop` of the CURRENT
# `tool_use` block reliably arrives on the wire AFTER that block's own tool
# result — the CLI starts running the tool as soon as its input JSON is
# complete, before the SSE `content_block_stop` is relayed — so it was still
# misclassified as "next turn producing" and reclosed the model-wait window
# the tool result had just opened. `content_block_start` / `content_block_delta`
# are excluded for the same reason: between a `message_stop` and the next
# `message_start`, nothing the wire delivers is the next turn's production —
# only `message_start` itself is. All three stay LIVENESS (they still rearm
# the deadline, unconditionally, via `note_stream_activity`'s unconditional
# `_bump_idle_deadline()` call) — they simply no longer close the window.
_PRODUCTION_STREAM_EVENTS = frozenset({"message_start"})


# ── cpp#185 D1: prompt-cache-dead detector ───────────────────────────────────
#
# P0 #2313's root cause (MPC-side relay forwarding `Connection: keep-alive`)
# is fixed (mika#2316). This guardrail is defense-in-depth, not the fix: if a
# sandboxed pilot's prompt cache dies for ANY other reason, `cache_read=0`
# with a large `cache_creation` on every turn means the full 100-250k token
# context is being re-sent and re-written EVERY turn instead of read from
# cache — a groom burns 10-12M uncached Opus tokens before the weekly ceiling
# catches it. This does not diagnose the cause; it bounds the damage.
#
# Two false-positive guards, both required (cpp#185 body, verbatim):
#   1. Turn 1 legitimately misses (there is nothing to read yet) — a LONE
#      miss must never trip. Handled by requiring 3 CONSECUTIVE misses, never
#      by special-casing turn number.
#   2. A miss on a small prompt (a short tool result, a brief reply) is not
#      evidence of a broken cache — it was never going to create a
#      cacheable prefix worth reading back. Gated on `cache_creation_input_tokens`
#      exceeding a substantial-prefix threshold, so routine small turns
#      never count as evidence either way.
#: Minimum `cache_creation_input_tokens` on a `cache_read_input_tokens == 0`
#: turn for that miss to count as EVIDENCE the cache is dead, rather than an
#: ordinary short prompt that was never going to be cached. Sandboxed dead-cache
#: sessions rewrite 100k-250k tokens per turn (cpp#185 body); 50k sits well
#: below that floor and well above what a routine short exchange creates.
PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS = 50_000

#: Consecutive substantial-miss turns before the session is judged cache-dead
#: and aborted (cpp#185: "3 tours consécutifs"). Any turn that does NOT meet
#: both conditions above — a genuine hit (`cache_read>0`) OR a miss too small
#: to be evidence — resets this counter to zero, so a session that only
#: intermittently misses (the healthy warm-up shape the plan measured: many
#: small-prefix misses while context is still growing, then a hit once caching
#: engages) never trips.
PROMPT_CACHE_DEAD_CONSECUTIVE_MISSES = 3


def _usage_int(usage: dict[str, Any] | None, key: str) -> int | None:
    """Extract an integer usage field, or None if absent/malformed (cpp#185).

    `AssistantMessage.usage` is an untyped `dict[str, Any] | None` taken
    straight off the wire (`claude_agent_sdk._internal.message_parser`:
    `usage=data["message"].get("usage")`) — never trust its shape. A missing
    key, a non-dict `usage`, or a non-int value all yield None, which the
    caller treats as "no data this call" rather than as a false zero.
    """
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    # bool is a subclass of int in Python; explicitly excluded so a stray
    # `True`/`False` in a malformed payload cannot masquerade as a token count.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True)
class TurnBoundaryEvent:
    """Emitted when a logical turn just closed (cpp#10).

    `just_closed_turn` is the 1-indexed turn number that just ENDED (not the
    new turn that's starting). `had_text` / `had_tool_use` / `had_thinking_block`
    summarize what the just-closed turn produced — agent.py reads these to
    decide whether the turn was diagnostically silent and worth logging a
    marker for.

    `cache_read_input_tokens` / `cache_creation_input_tokens` (cpp#185 D1) are
    the closed turn's cache usage, straight from the SDK `usage` dict, for
    per-response cache observability. `None` when no `AssistantMessage` in the
    turn carried usage data (older SDK, malformed payload).
    """

    just_closed_turn: int
    had_text: bool
    had_tool_use: bool
    had_thinking_block: bool
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


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
        # cpp#144: sticky "an AskUserQuestion aimed at an absent operator was
        # refused" flag. Set by permissions.py whenever the policy layer
        # denies an AskUserQuestion call (the pilot is headless — there is
        # nobody to answer, so the refusal is correct; see cpp#144 body). Read
        # by agent.py at the ResultMessage boundary: a session that later
        # completes "successfully" with this flag armed and no `gh pr create`
        # observed (the same `_pr_created` signal mika#940 already tracks) is
        # reclassified from `success` to `blocked_on_operator_input` — the
        # model bypassed the refusal by rendering the question as plain text
        # and ending the turn, which the SDK sees as a clean completion.
        # Sticky and never cleared: a session that asked once and was refused
        # once still has no operator to answer it, no matter how many turns
        # follow, unless it goes on to actually deliver (pr_created).
        self._operator_question_denied: bool = False
        # Summary of the (last) denied AskUserQuestion call, reproduced in the
        # ResultJson.termination_reason so the exit message names the question
        # nobody could answer (cpp#144 AC1), not just the fact of a denial.
        self._operator_question_summary: str | None = None
        # cpp#151: sticky "a NON-TERMINAL policy refusal happened this session"
        # flag. Set by permissions.py at every `[policy:deny]` site whose
        # `_denial_is_terminal` verdict was False — i.e. exactly the refusals
        # cpp#128 made survivable, and exactly the population the cpp#151
        # measurement is about. Read by agent.py at the ResultMessage boundary
        # for two things: (1) classify a terminal `error_during_execution` as
        # `error_during_execution:after_deny` (AC2), and (2) decide whether that
        # EDE is worth one bounded resume instead of a burial (AC1).
        #
        # DELIBERATELY NOT set for a terminal refusal. A destination veto or a
        # tier3-dangerous Bash command is a kill claude-pilot ASKED for; folding
        # it in here would re-merge the two populations B0 exists to separate,
        # and would hand a containment breach a free resume.
        self._nonterminal_policy_deny: bool = False
        # Summary of the (last) non-terminal refusal, reproduced in
        # ResultJson.termination_reason so the exit line names the command that
        # preceded the death rather than only the fact of a refusal.
        self._nonterminal_policy_deny_summary: str | None = None
        # cpp#151: the mirror flag, and the one that VETOES a resume. Set when a
        # refusal claude-pilot ASKED to be fatal happens — destination veto,
        # tier3-dangerous Bash, deny-with-notify. Sticky for the same reason the
        # other one is: a session that once tried to write outside its worktree
        # does not become trustworthy again three turns later, and the marker
        # above being sticky too means that WITHOUT this flag one harmless early
        # refusal would have made every later containment kill resumable.
        self._terminal_policy_deny: bool = False
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
        # and since when. All of it lives on the INSTANCE, never in the
        # watchdog task's closure: `_reset_idle_timer` cancels and recreates
        # that task on every turn boundary, so a fresh task must be able to
        # read a transition decided before it was created.
        #
        # Outstanding tools are COUNTED rather than folded into a single state
        # scalar. Two reasons, both measured: ordinary generation events arrive
        # while a tool is in flight (67 of them across 177 real
        # dispatch-to-result pairs), so a scalar gets cleared mid-flight; and a
        # turn may dispatch several tools at once, where the first result must
        # not declare the others finished.
        self._pending_tool_uses: int = 0
        self._tool_wait_started_at: float | None = None
        # A tool result was delivered and the next turn has not produced its
        # first token yet. This is the window that killed the six sessions.
        self._awaiting_model: bool = False
        self._model_wait_started_at: float | None = None
        # Name of the tool the current tool wait is anchored on, for the abort
        # message. Best-effort; None renders as "a tool".
        self._wait_detail: str | None = None
        # cpp#145: what the last observed signal WAS, and how many content
        # stream events arrived since the current wait window opened. Both are
        # reported in the abort detail (AC5): a message that says "no
        # meaningful progress" while citing 1722 progress events contradicts
        # itself inside its own sentence, and that message is what made
        # mika#2029 take six rounds to read.
        self._last_signal: str | None = None
        self._window_stream_count: int = 0
        # cpp#185 D1: cache usage for the CURRENT (in-progress) turn. Unlike
        # `_current_turn_text_len` these do NOT accumulate across
        # continuation blocks — `cache_read_input_tokens` /
        # `cache_creation_input_tokens` are input-side counts fixed at the
        # turn's first token, so a later non-None reading simply confirms
        # (never revises) an earlier one. Overwritten wholesale at each new
        # turn boundary in `on_assistant_message`.
        self._current_turn_cache_read: int | None = None
        self._current_turn_cache_creation: int | None = None
        # Guards `_maybe_evaluate_cache_dead_guardrail` to exactly one
        # evaluation per logical turn (a turn spans several content-block
        # events sharing one message_id) — set False on every new turn
        # boundary, True the first time that turn supplies both cache fields.
        self._cache_dead_evaluated_for_current_turn: bool = False
        # cpp#185 D1: consecutive turns whose cache reading met BOTH
        # conditions of the `prompt_cache_dead` guardrail (see the module-level
        # docstring above `PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS`). Reset to
        # 0 by ANY turn that does not — a genuine hit or a too-small miss.
        self._consecutive_cache_dead_misses: int = 0
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
    def operator_question_denied(self) -> bool:
        """True if a policy denial of `AskUserQuestion` was observed this
        session (cpp#144). Read by agent.py post-ResultMessage to decide
        whether a "successful" completion actually ended on a question with
        nobody present to answer it."""
        return self._operator_question_denied

    @property
    def operator_question_summary(self) -> str | None:
        """Human-readable summary of the (last) denied `AskUserQuestion` call,
        or None if none was denied this session (cpp#144)."""
        return self._operator_question_summary

    @property
    def nonterminal_policy_deny(self) -> bool:
        """True if a NON-TERMINAL policy refusal was observed this session
        (cpp#151). See `note_policy_deny` for why terminal refusals are
        excluded."""
        return self._nonterminal_policy_deny

    @property
    def nonterminal_policy_deny_summary(self) -> str | None:
        """Summary of the (last) non-terminal policy refusal, or None."""
        return self._nonterminal_policy_deny_summary

    @property
    def terminal_policy_deny(self) -> bool:
        """True if a DELIBERATELY lethal policy refusal was observed this
        session (cpp#151). Read by agent.py to veto the resume outright."""
        return self._terminal_policy_deny

    def note_policy_deny(self, summary: str | None, *, terminal: bool) -> None:
        """Record a policy refusal and its lethality (cpp#151 B0/B1).

        Called from permissions.py at every `[policy:deny]` return site, with
        the SAME `terminal` value that was handed to
        `PermissionResultDeny(interrupt=…)` and to `ui.log_policy_deny` — one
        computation, three consumers, so the log line, the audit event and the
        session marker can never disagree about a given refusal.

        The two flags are separate and BOTH sticky. `terminal=True` arms
        `terminal_policy_deny`, which vetoes the resume for the rest of the
        session; `terminal=False` arms `nonterminal_policy_deny`, which is what
        makes a resume eligible in the first place. A session that took both
        ends up ineligible — the conservative resolution, and the one that
        closes the "one harmless refusal early on makes every later kill
        resumable" hole.

        `summary` overwrites on each survivable refusal so the message names the
        one CLOSEST to whatever the session did next.
        """
        if terminal:
            self._terminal_policy_deny = True
            return
        self._nonterminal_policy_deny = True
        self._nonterminal_policy_deny_summary = summary

    def note_operator_question_denied(self, summary: str | None) -> None:
        """Record that an `AskUserQuestion` call was refused by policy
        (cpp#144). Called from permissions.py at every deny return site that
        can carry `tool_name == "AskUserQuestion"`.

        Sticky: once set, stays set for the rest of the session — refusing a
        second question does not undo the first refusal. `summary` overwrites
        on each call so the message names the MOST RECENT denied question,
        which is the one closest to whatever the session did next.
        """
        self._operator_question_denied = True
        self._operator_question_summary = summary

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

    @property
    def _wait_state(self) -> _WaitState:
        """What the session is waiting for right now (cpp#145), derived.

        Tool wait outranks model wait: if a tool has not returned, that is what
        we are waiting on, whatever else is also true. Derived rather than
        stored so no single write can leave the machine claiming a wait that
        the outstanding-tool count contradicts.
        """
        if self._pending_tool_uses > 0:
            return _WaitState.AWAITING_TOOL
        if self._awaiting_model:
            return _WaitState.AWAITING_MODEL
        return _WaitState.IDLE

    def note_stream_activity(self, event_type: str | None = None) -> None:
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

        cpp#145/cpp#177: `event_type` is the raw SSE event name. Only
        `message_start` — the one event that can only ever begin a NEW turn —
        closes the model-wait window. Every other event in
        `_PROGRESS_STREAM_EVENT_TYPES` (`content_block_start`,
        `content_block_delta`, `content_block_stop`, `message_delta`,
        `message_stop`) rearms the deadline and counts, exactly as before, but
        does NOT claim the model has resumed. `message_delta` / `message_stop`
        are the CURRENT turn's own closing trailers; `content_block_start` /
        `content_block_delta` / `content_block_stop` are its content blocks —
        `content_block_stop` of the tool_use block that triggered the tool
        result arrives on the wire AFTER that result (cpp#177: the CLI runs
        the tool before the SSE close is relayed). None of the five are
        evidence that a NEXT turn exists yet; only `message_start` is. `None`
        (an older caller, or a test that does not care) is treated as
        production, preserving the previous signature's behaviour.

        A stream event NEVER retires an outstanding tool. Generation and tool
        execution overlap on the real wire; only the tool's result ends its
        wait.
        """
        self._stream_activity_count += 1
        self._clear_rate_limit()
        self._last_signal = "stream event"
        self._window_stream_count += 1
        if event_type is None or event_type in _PRODUCTION_STREAM_EVENTS:
            # The next turn is producing → the model is no longer being waited
            # on. Outstanding tools are untouched: they are counted, not stated.
            self._awaiting_model = False
            self._model_wait_started_at = None
        self._bump_idle_deadline()

    def note_activity(self, tool_results: int = 1) -> None:
        """Record non-generation SDK liveness on the message stream (cpp#125).

        A `UserMessage` carries tool results — inbound traffic that proves the
        session is still alive, but is NOT model production. Unlike
        `note_stream_activity` this moves the idle deadline forward WITHOUT
        incrementing the content-stream counter (whose reported meaning is
        "content stream events this session") and WITHOUT clearing the sticky
        rate-limit flag: a tool result is no evidence that a throttled
        generation retry has succeeded. Deliberately cheap — it only moves a
        deadline and never touches the watchdog task.

        cpp#145: it retires `tool_results` outstanding tools and, once none
        remain, OPENS the model-wait window. This is the line that ends the
        killed sessions — the tool result was delivered, the deadline was
        pushed, and then 300s of nothing while the session waited for the first
        token of the next turn. That silence is a wait, not an idle, and from
        here it is measured against `modelWaitCeilingMs`.

        `tool_results` is how many tool_result blocks the `UserMessage` carried,
        so a batch of parallel tools retires all of them at once. It never goes
        below zero: an unmatched result (a tool dispatched before this guardrail
        existed, a shape the block walker did not recognise) must not leave a
        phantom tool outstanding and hold the session to the tool ceiling.
        """
        self._last_signal = "tool result"
        self._window_stream_count = 0
        self._pending_tool_uses = max(0, self._pending_tool_uses - max(1, tool_results))
        if self._pending_tool_uses == 0:
            self._tool_wait_started_at = None
            self._wait_detail = None
            if not self._awaiting_model:
                # Anchor on the TRANSITION only. Re-anchoring on every result
                # would push the ceiling out indefinitely — the mistake cpp#133
                # already avoided for `_rate_limit_started_at`.
                self._awaiting_model = True
                self._model_wait_started_at = self._now()
        self._bump_idle_deadline()

    def _note_tool_uses(self, count: int, first_name: str | None) -> None:
        """A turn dispatched `count` tools; they are now outstanding (cpp#145).

        Anchors the tool wait on the first outstanding tool and leaves it there
        while any remain, so a turn that keeps dispatching cannot push its own
        ceiling out of reach. Dispatching a tool also ends any model wait: the
        model demonstrably produced this turn.
        """
        if count <= 0:
            return
        if self._pending_tool_uses == 0:
            self._tool_wait_started_at = self._now()
            self._wait_detail = first_name
        self._pending_tool_uses += count
        self._awaiting_model = False
        self._model_wait_started_at = None

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
        usage: dict[str, Any] | None = None,
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

        cpp#185 D1: `usage` is the SDK `AssistantMessage.usage` dict for THIS
        call, straight off the wire. Unlike the content-block evidence above,
        cache token counts are fixed at the turn's first token rather than
        accumulating, so they are evaluated (once per turn — see
        `_maybe_evaluate_cache_dead_guardrail`) as soon as they are known,
        which may be on this call or a later continuation of the same turn.
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

        # cpp#145: tools dispatched by this turn are now outstanding. Recorded
        # HERE rather than in permissions.py on purpose: the relay callback
        # (`pause_idle_timer` / `resume_idle_timer`, permissions.py:1112/:1177)
        # brackets only the relay round-trip, so it sees neither auto-approved
        # tools nor the tool's own execution time — the very window AC3 is
        # about. The blocks are already walked just above for `has_tool_use`.
        self._last_signal = "turn boundary"
        self._window_stream_count = 0
        if has_tool_use:
            self._note_tool_uses(
                sum(1 for b in blocks if _block_type(b) == "tool_use"),
                _first_tool_use_name(blocks),
            )
        else:
            # A turn that produced no tool call is waiting for nobody: the model
            # spoke and stopped. If nothing follows, that is genuine silence and
            # the original 300s budget is the right one.
            self._awaiting_model = False
            self._model_wait_started_at = None

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

        cache_read = _usage_int(usage, "cache_read_input_tokens")
        cache_creation = _usage_int(usage, "cache_creation_input_tokens")

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
            # cpp#185 D1: a later block of the SAME turn may be the first one
            # to carry usage (defensive — see `_usage_int`); keep accumulating
            # and evaluate as soon as both fields are known.
            if cache_read is not None:
                self._current_turn_cache_read = cache_read
            if cache_creation is not None:
                self._current_turn_cache_creation = cache_creation
            self._maybe_evaluate_cache_dead_guardrail()
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
                cache_read_input_tokens=self._current_turn_cache_read,
                cache_creation_input_tokens=self._current_turn_cache_creation,
            )

        self._turn_count += 1
        self._current_message_id = message_id
        self._current_turn_has_tool = has_tool_use
        self._current_turn_text_len = text_len
        self._current_turn_had_thinking_block = has_thinking
        self._stall_incremented_for_current_turn = False
        self._empty_incremented_for_current_turn = False
        # cpp#185 D1: this call opens the NEW turn — its cache reading (if any
        # arrived already) replaces the closed turn's, and the guardrail is
        # evaluated for it exactly once, as soon as both fields are known.
        self._current_turn_cache_read = cache_read
        self._current_turn_cache_creation = cache_creation
        self._cache_dead_evaluated_for_current_turn = False
        self._maybe_evaluate_cache_dead_guardrail()
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

    def _maybe_evaluate_cache_dead_guardrail(self) -> None:
        """cpp#185 D1: evaluate `prompt_cache_dead` for the CURRENT turn.

        Cache token counts are input-side and fixed at the turn's first
        token (unlike `_current_turn_text_len`, which genuinely accumulates
        across continuation blocks) — so there is nothing to roll back the
        way stall/empty detection rolls back a speculative increment: the
        first call that supplies BOTH `cache_read_input_tokens` and
        `cache_creation_input_tokens` settles this turn's classification for
        good. `_cache_dead_evaluated_for_current_turn` makes that evaluation
        happen exactly once per logical turn, however many content-block
        events (thinking → text → tool_use) the turn spans.

        A turn counts as a substantial miss only when BOTH hold: a genuine
        cache miss (`cache_read_input_tokens == 0`) AND a real prefix that
        should have been cached (`cache_creation_input_tokens` over
        `PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS`). Any other turn — a hit,
        a too-small miss, or one with no usage data at all — resets the
        streak: a single intervening turn that does not match is proof the
        cache is not PERMANENTLY dead, which is exactly what cpp#185's
        false-positive guards require (the mandatory first-turn miss, and a
        session ramping from small to large context before caching engages).
        """
        if self._cache_dead_evaluated_for_current_turn:
            return
        cache_read = self._current_turn_cache_read
        cache_creation = self._current_turn_cache_creation
        if cache_read is None or cache_creation is None:
            return  # no usage data yet this turn — nothing to classify
        self._cache_dead_evaluated_for_current_turn = True
        if (
            cache_read == 0
            and cache_creation > PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS
        ):
            self._consecutive_cache_dead_misses += 1
            if self._consecutive_cache_dead_misses >= PROMPT_CACHE_DEAD_CONSECUTIVE_MISSES:
                self._abort(
                    "prompt_cache_dead",
                    f"cache_read_input_tokens==0 with cache_creation_input_tokens>"
                    f"{PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS} for "
                    f"{self._consecutive_cache_dead_misses} consecutive turns — "
                    "the prompt cache appears dead; aborting rather than "
                    "re-writing the full context every turn",
                )
        else:
            self._consecutive_cache_dead_misses = 0

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
            cache_read_input_tokens=self._current_turn_cache_read,
            cache_creation_input_tokens=self._current_turn_cache_creation,
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
        """Start a fresh full-duration idle timer (called after relay).

        cpp#145: an open wait window is re-anchored too. The relay round-trip
        is time the guardrail explicitly does NOT measure — that is what the
        pause is for — so charging it against a wait ceiling would contradict
        the same contract this method exists to honour. Bounded by construction:
        the relay fires once per tool permission decision, and each decision
        belongs to a tool that is separately accounted for.
        """
        now = self._now()
        if self._tool_wait_started_at is not None:
            self._tool_wait_started_at = now
        if self._model_wait_started_at is not None:
            self._model_wait_started_at = now
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
                    if self._deferral_expired(started, ceiling, loop.time()):
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
                wait_started = self._wait_anchor(state)
                if wait_started is None:
                    # Entered outside a running loop (defensive); anchor now.
                    wait_started = loop.time()
                    self._set_wait_anchor(state, wait_started)
                if self._deferral_expired(
                    wait_started, self._wait_ceiling_secs(state), loop.time()
                ):
                    self._abort_wait_ceiling(state, wait_started, loop.time())
                    return
                await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # cpp#168: the ticket's "alternative cause" — a fire-and-forget
            # `create_task` (`_reset_idle_timer` above) whose body dies on a
            # swallowed exception leaves NOTHING watching the session: nobody
            # awaits this task's result, so a bare `raise` here would only
            # ever surface as "Task exception was never retrieved" on
            # asyncio's own default handler (if that), never as a terminated
            # session. A dead watchdog must not be a SILENT dead watchdog —
            # abort explicitly, with a reason distinct from `idle_timeout` so
            # the population "the watchdog itself broke" stays visible instead
            # of laundering into "the model went silent" (see
            # `GuardrailAbortReason.guardrail` docstring in types.py).
            self._abort(
                "watchdog_error",
                f"idle watchdog task crashed ({type(exc).__name__}: {exc}); "
                "terminating rather than leaving the session unmonitored",
            )
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

    @staticmethod
    def _deferral_expired(started: float, ceiling_secs: float, now: float) -> bool:
        """Has a deferral outlived its ceiling? (cpp#133 shape, cpp#145 reuse.)

        `ceiling_secs <= 0` means "no ceiling — defer indefinitely". That is a
        deliberate, documented mode inherited from `rateLimitCeilingMs`, and it
        is the one configuration in which the watchdog cannot terminate this
        state at all — which is why `log_guardrail_config` names it explicitly
        rather than omitting it.
        """
        return ceiling_secs > 0 and now - started >= ceiling_secs

    def _wait_ceiling_secs(self, state: _WaitState) -> float:
        """Ceiling, in seconds, for a waiting state (cpp#145)."""
        if state is _WaitState.AWAITING_TOOL:
            return self._config.toolWaitCeilingMs / 1000.0
        return self._config.modelWaitCeilingMs / 1000.0

    def _wait_anchor(self, state: _WaitState) -> float | None:
        """When the current wait window opened (cpp#145)."""
        if state is _WaitState.AWAITING_TOOL:
            return self._tool_wait_started_at
        return self._model_wait_started_at

    def _set_wait_anchor(self, state: _WaitState, at: float) -> None:
        if state is _WaitState.AWAITING_TOOL:
            self._tool_wait_started_at = at
        else:
            self._model_wait_started_at = at

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
            outstanding = self._pending_tool_uses
            detail = (
                f"Tool wait exceeded ceiling: no result from {who} for ~{waited}s "
                f"(ceiling {ceiling_secs}s, {outstanding} tool call(s) still "
                f"outstanding); last signal was a "
                f"{self._last_signal or 'turn boundary'} "
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
            # cpp#168: the watchdog task itself crashed. See `_idle_watchdog`.
            "watchdog_error",
            # cpp#185 D1: the prompt cache is dead — see the module docstring
            # above `PROMPT_CACHE_CREATION_SUBSTANTIAL_TOKENS`.
            "prompt_cache_dead",
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
