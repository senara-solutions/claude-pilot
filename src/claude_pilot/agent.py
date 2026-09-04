"""Agent runner. Port of src/agent.ts.

Uses `ClaudeSDKClient` because `can_use_tool` is only available on the
bidirectional client, not on the one-shot `query()` entrypoint. Streams
messages, feeds turn boundaries to SessionGuardrails, emits a ResultJson line
to stdout when the session ends (success, error, or guardrail abort).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    SystemPromptPreset,
    UserMessage,
)

from .guardrails import SessionGuardrails, TurnBoundaryEvent
from .heartbeat import emit_heartbeat, emit_heartbeat_throttled
from .inbox_writer import post_handoff
from .permissions import CanUseTool
from .tier1 import DENIED_BASH_PATTERNS_HINT
from .types import ResultJson
from .ui import (
    log_deny_resume,
    log_deny_resume_failed,
    log_done,
    log_error,
    log_guardrail,
    log_guardrail_config,
    log_init,
    log_prompt,
    log_reconnect,
    log_text,
    log_turn_summary,
    log_unhandled_message,
    log_verbose,
)

SDK_TERMINATION_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})

# ── cpp#151: the residual lethality of a refusal ─────────────────────────────
#
# cpp#128 split the DECISION (refuse) from the LETHALITY (`interrupt=True`), and
# the measured lethality of a refusal fell from 100% to 32%. The residue is this
# shape: a refusal `_denial_is_terminal` classified NON-terminal, delivered to
# the model as a tool_result, and then — with `stop_reason=tool_use` and no
# usable content on the last user message — an `error_during_execution` from the
# SDK's own bundled `claude` binary. Fifteen sessions with no refusal at all
# produced ZERO deaths of this shape; a refusal remains the necessary condition.
#
# Two things happen here, and only the first one is new information:
#   * B1/AC2 — NAME the shape. The `[ede_diagnostic] …` prose belongs to the
#     bundled binary (`site-packages/claude_agent_sdk/_bundled/claude`) and is
#     not ours to change, so claude-pilot classifies the upstream ResultMessage
#     against its OWN session state and emits its OWN subtype. Additive exactly
#     like cpp#144: a new `subtype` string, never a new `status`.
#   * B2/AC1 — SURVIVE it. NOT by re-querying the same client: when the CLI
#     emits an error result "it then exits non-zero on purpose"
#     (`claude_agent_sdk/_internal/query.py`, the SDK's own words, naming
#     `error_during_execution`), and the reader queues the trailing `ResultError`
#     for whoever reads next. Reading past that ResultMessage would raise it
#     into the message loop and lose the classification entirely. The recovery
#     is the SDK's own session-resume path: a NEW `ClaudeSDKClient` carrying
#     `ClaudeAgentOptions.resume=<session id>`, given one bounded, logged
#     follow-up prompt — instead of burying 4h35 of work over a refused `ls`.
EDE_SUBTYPE = "error_during_execution"

#: Emitted INSTEAD of a bare `error_during_execution` when the session had taken
#: a non-terminal refusal (cpp#151 AC2). Consumed opaquely by dispatch-lib's
#: `jq -r '.subtype // empty'`, so adding it is additive downstream.
EDE_AFTER_DENY_SUBTYPE = "error_during_execution:after_deny"

#: How many times one session may be resumed past an `error_during_execution`
#: that followed a non-terminal refusal. Two, not unbounded: a resume that
#: cannot terminate would convert this death into an infinite session, and
#: `maxTurns` is the only guardrail that reliably bounds a BUSY loop. Two covers
#: the observed shape (a single refused command mid-pipeline) with one spare,
#: and a session that dies three times this way has a different problem that
#: should reach the operator as a result line, not as more retries.
DEFAULT_MAX_DENY_RESUMES = 2

#: Operator override for the budget above. `0` disables the resume entirely,
#: leaving B0+B1 (observability + classification) — the documented rollback.
MAX_DENY_RESUMES_ENV = "CLAUDE_PILOT_MAX_DENY_RESUMES"

#: Hard ceiling on that override. Each resume starts a fresh CLI query loop with
#: its own `maxTurns` budget, so the effective turn ceiling of a run is
#: `(1 + resumes) x maxTurns`. `maxTurns` is the ONLY guardrail that bounds a
#: BUSY refusal loop (the stall / empty / idle detectors all structurally miss
#: it — see the doctrine block above `permissions._denial_is_terminal`), so an
#: unclamped env var would let a typo multiply the one bound that holds.
MAX_DENY_RESUMES_CEILING = 5

#: The follow-up prompt. It RELAXES NOTHING: it restates that the refusal stands
#: and that the command must not be retried as-is. The failure mode it guards
#: against is a resumed session immediately re-issuing the denied command and
#: spending the budget on a loop.
DENY_RESUME_NUDGE = (
    "The previous turn ended in an SDK error immediately after a permission "
    "denial. The denial STANDS and is a normal, expected result — the command "
    "was not run and will not be run. Do NOT retry that command as written, and "
    "do not ask for the permission to be widened. Continue the task from where "
    "it stopped, using a different approach: prefer the native READ tools (Read, "
    "Glob, Grep) over composed shell commands, and run one simple command at a "
    "time rather than chaining with ';', '&&' or a 'for' loop."
)
# NOTE (cpp#151 review): the write-side native tools are deliberately NOT named
# here. `Write`/`Edit` are tier1-approved on `is_within_project` alone
# (`tier1.py`), while a Bash write also passes `_destination_veto_reason` —
# worktree containment PLUS the cpp#42 control-plane denylist (`.git/`,
# `.claude/`, `.github/workflows/`, ...). Steering a model whose Bash write was
# just refused toward the surface with the weaker destination check would make
# this prompt a route around a containment boundary. The tier1 gap is
# pre-existing and belongs in its own ticket; this text must not recommend it.


def _resolve_max_deny_resumes() -> int:
    """Read the resume budget from the environment, fail-safe to the default.

    A malformed value degrades to :data:`DEFAULT_MAX_DENY_RESUMES` rather than
    raising into session startup; a negative value clamps to 0 (disabled).
    """
    raw = os.environ.get(MAX_DENY_RESUMES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_DENY_RESUMES
    try:
        parsed = int(raw)
    except ValueError:
        # Fail SAFE, not to the default: this variable is documented as the
        # rollback lever, and an operator who types `0.0` or `false` to turn the
        # resume OFF must not get it fully on. Anything unparseable disables.
        log_deny_resume_failed(
            f"{MAX_DENY_RESUMES_ENV}={raw!r} is not an integer — resume disabled"
        )
        return 0
    return max(0, min(parsed, MAX_DENY_RESUMES_CEILING))


#: `ResultMessage.terminal_reason` values that mean "this turn was killed by an
#: interrupt WE requested" — the SDK documents `aborted_tools` as the turn being
#: cancelled via an interrupt control request, which is exactly what a
#: `PermissionResultDeny(interrupt=True)` sends. A death carrying one of these
#: is a deliberate kill, never a resume candidate, and this check is positive
#: and upstream-sourced rather than inferred from our own bookkeeping.
ABORT_TERMINAL_REASONS = frozenset({"aborted_tools", "aborted_streaming"})


class _DenyResumeController:
    """Bounded, never-silent resume of an EDE that followed a survivable refusal.

    Split out of the message loop so the budget, the arming flag and the
    deferred classification are one object the ResultMessage branch and the
    session loop share.
    """

    def __init__(self, budget: int) -> None:
        self._budget = max(0, budget)
        self._used = 0
        self._armed = False
        #: Set when a resume was armed: the subtype we did NOT emit, so the
        #: session loop can still report the cpp#151 classification if the
        #: resumed client never comes up.
        self._deferred_subtype: str | None = None

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def used(self) -> int:
        return self._used

    @property
    def armed(self) -> bool:
        """True when a resume is pending and the session loop must reconnect."""
        return self._armed

    @property
    def deferred_subtype(self) -> str | None:
        return self._deferred_subtype

    def disarm(self) -> None:
        self._armed = False

    def should_resume(
        self,
        message: Any,
        subtype: str,
        guardrails: SessionGuardrails,
        session_id: str | None,
    ) -> bool:
        """Whether this terminal message is the cpp#151 shape AND may be resumed.

        Six conditions, and the two that exclude deliberate kills are the ones
        that matter. cpp#151's first draft gated on
        `guardrails.nonterminal_policy_deny` alone, which is STICKY — so a
        single harmless refusal early in a session (`echo probe; ls`) would arm
        the marker for good, and every later containment kill would have
        qualified. That is a resume handed to a session that just tried to write
        outside its worktree. Both halves are closed here:

        * `not guardrails.terminal_policy_deny` — a session that has taken ANY
          deliberately lethal refusal is out, permanently. Our own bookkeeping,
          and the conservative direction: it can only refuse a resume.
        * `terminal_reason not in ABORT_TERMINAL_REASONS` — the SDK's own report
          that this turn was killed by an interrupt control request. Independent
          of our bookkeeping, so the two would have to fail together.

        `getattr` guards the SDK field so a minor lacking it degrades to "not an
        abort" — which is safe here only because the marker check stands beside
        it; neither is load-bearing alone.
        """
        if subtype != EDE_SUBTYPE:
            return False
        if self._used >= self._budget:
            return False
        if session_id is None:
            # Nothing to resume FROM. The SDK's resume takes a session id; we
            # only ever have one after an `init`.
            return False
        if getattr(message, "terminal_reason", None) in ABORT_TERMINAL_REASONS:
            return False
        if guardrails.terminal_policy_deny:
            return False
        return guardrails.nonterminal_policy_deny

    def arm(self, subtype: str) -> None:
        """Spend one unit of budget and ask the session loop to reconnect."""
        self._used += 1
        self._armed = True
        self._deferred_subtype = subtype
        log_deny_resume(self._used, self._budget, subtype)


def _session_options(options: Any, resume_from: str | None) -> Any:
    """The SDK options for one session: the originals, or a resuming copy.

    Returns `options` UNCHANGED on the first session, so the ordinary path is
    byte-for-byte what it was before cpp#151 — including for callers (and
    tests) that hand in a non-dataclass stand-in.
    """
    if resume_from is None:
        return options
    return dataclasses.replace(options, resume=resume_from)


# cpp#123: raw Anthropic SSE event types that prove the model is still
# producing. `ping` is a connection keepalive and `error` is a failure signal —
# neither is progress, and rearming the idle guardrail on a keepalive would
# make it inert for as long as the socket stays open. An allow-list is used
# rather than excluding `ping` so a future keepalive under a new name cannot
# silently disarm the guardrail; `log_unhandled_message` keeps the blind spot
# observable if the SDK ever adds a top-level event type.
_PROGRESS_STREAM_EVENT_TYPES = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    }
)

# cpp#123: union members the loop ignores ON PURPOSE, so the unhandled-message
# branch below stays quiet about them. A `SystemMessage` whose subtype is not
# `init` is a deliberate skip. Without this set the branch would print a line
# every run, and a genuinely new union member would be indistinguishable from
# that standing baseline — which is the exact diagnosis cost it exists to
# prevent. Matched on the exact class name, so SDK SystemMessage SUBCLASSES
# (TaskProgressMessage, HookEventMessage, ...) still get reported.
# cpp#125: `UserMessage` used to live here too, but it now has an explicit
# isinstance branch (rearm + debug log) and never reaches the terminal branch,
# so it no longer needs a silent-ignore entry.
_KNOWN_IGNORED_MESSAGE_TYPES = frozenset({"SystemMessage"})

# cpp#111 D8-2: per-turn heartbeats are throttled to at most one per minute so
# a tool-heavy stream does not flood cm-api. Session-start / session-end /
# tool-recovery are all rare enough that they fire unthrottled.
_HEARTBEAT_TURN_THROTTLE_SECS: float = 60.0
_HEARTBEAT_TURN_KEY: str = "pilot:turn"


async def run_agent(
    *,
    prompt: str,
    cwd: str,
    verbose: bool,
    task_id: str | None,
    permission_handler: CanUseTool,
    guardrails: SessionGuardrails,
) -> int:
    """Run the agent session. Returns the intended process exit code.

    Thin wrapper around :func:`_run_agent_inner` that fires cm heartbeats at
    the session-lifecycle boundaries (cpp#111 D8-2 Transitions 1 and 4). The
    session-start emit fires before any SDK work so cm sees liveness the
    instant the subprocess begins — even a spawn that never manages to
    complete an ``init`` handshake still shows a fresh heartbeat. The
    session-end emit is in a ``finally`` so it fires on every exit path:
    natural completion, early return from a guardrail trip, and any
    exception propagating out of the SDK client.
    """
    reason_tag = task_id or "unknown"
    emit_heartbeat(f"session:{reason_tag}")
    exit_code = 1
    try:
        exit_code = await _run_agent_inner(
            prompt=prompt,
            cwd=cwd,
            verbose=verbose,
            task_id=task_id,
            permission_handler=permission_handler,
            guardrails=guardrails,
        )
        return exit_code
    finally:
        emit_heartbeat(
            f"complete:{reason_tag}",
            meta={"exit_code": exit_code},
        )


async def _run_agent_inner(
    *,
    prompt: str,
    cwd: str,
    verbose: bool,
    task_id: str | None,
    permission_handler: CanUseTool,
    guardrails: SessionGuardrails,
) -> int:
    """Actual agent session body. Extracted so :func:`run_agent` can wrap it
    with lifecycle heartbeats without reindenting the whole implementation."""
    start_time = time.monotonic()
    session_id: str | None = None
    seen_init: bool = False
    config = guardrails.config

    log_guardrail_config(config)

    options = ClaudeAgentOptions(
        permission_mode="default",
        cwd=cwd,
        setting_sources=["user", "project", "local"],
        can_use_tool=permission_handler,
        include_partial_messages=True,
        system_prompt=_system_prompt_with_hint(),
        # cpp#59 — defense-in-depth tool-surface exclusion. `ScheduleWakeup` is a
        # Claude Code harness/CLI runtime primitive (the `/loop` pacing tool),
        # NOT a permissionable SDK tool: the runtime handles it internally and
        # bypasses can_use_tool entirely, so a tier1/policy deny is structurally
        # inert (see DENIED_BASH_PATTERNS_HINT scope note). In headless SDK mode
        # it is a no-op that strands the session (mika#1652). `disallowed_tools`
        # maps to the CLI `--disallowedTools` flag; transcript evidence shows
        # ScheduleWakeup IS a real surfaced/executed tool, so a bare-name deny
        # SHOULD remove it from the request — but the SDK docs do not definitively
        # confirm --disallowedTools filters runtime primitives, so this is
        # best-effort. The LOAD-BEARING guard is the system-prompt hint above;
        # this is harmless if it no-ops and structural if the runtime honors it.
        disallowed_tools=["ScheduleWakeup"],
        **_sdk_guardrail_kwargs(config),
    )

    exit_code = 0
    # cpp#20 joint 2 synthetic-emit guard: flips True after any in-loop
    # terminal _emit_result call (guardrail trip or ResultMessage). Post-loop
    # check below uses this to decide whether to emit a synthetic terminal
    # ResultJson when the SDK stream ends without a ResultMessage -- the
    # Case-B failure mode introduced by PermissionResultDeny(interrupt=True)
    # at the can_use_tool boundary. Mutual exclusion proof + architect
    # verdict: cpp#20 body, "Friend-Claude review convergence" section.
    emitted_terminal = False

    # cpp#151 B2: bounded recovery from `error_during_execution` after a
    # survivable refusal. One controller per run, so the budget is per run and
    # not per resumed session.
    deny_resume = _DenyResumeController(_resolve_max_deny_resumes())

    # Hoisted out of the session loop below: the guardrails object spans the
    # whole run, so its abort watcher and its `unhandled_message_types` ledger
    # must not be rebuilt per resumed session (a rebuilt ledger would re-log
    # every already-reported SDK message type on each resume).
    guardrail_watcher = asyncio.create_task(guardrails.wait_aborted())
    unhandled_message_types: set[str] = set()

    #: `None` on the first session; the session id to resume on later ones.
    resume_from: str | None = None

    try:
        while True:
            # cpp#151: a RESUMED session is the one place claude-pilot opens a
            # client it did not open at startup, so it is the one place a
            # connect / resume-refused / transport failure can arrive mid-run.
            # Swallowing it here is not optional: an exception escaping
            # `run_agent` reaches `cli.py`'s `_emit_fatal`, which writes a bare
            # `subtype="fatal"` line with no task_id, no session_id and no
            # turns — trading the death we just classified for a less legible
            # one. The FIRST session re-raises, so every pre-cpp#151 failure
            # path keeps its exact behaviour.
            try:
                async with ClaudeSDKClient(
                    options=_session_options(options, resume_from)
                ) as client:
                    await client.query(prompt if resume_from is None else DENY_RESUME_NUDGE)

                    async for message in _merge_stream(client, guardrail_watcher):
                        if message is _GUARDRAIL_TRIP:
                            reason = guardrails.abort_reason
                            assert reason is not None
                            duration_ms = int((time.monotonic() - start_time) * 1000)
                            _emit_result(
                                ResultJson(
                                    status="terminated",
                                    subtype=reason.guardrail,
                                    task_id=task_id,
                                    session_id=session_id,
                                    turns=guardrails.turns,
                                    cost_usd=None,  # unknown — ResultMessage not yet received
                                    duration_ms=duration_ms,
                                    termination_reason=reason.detail,
                                    # cpp#119: surface the API-error status on the abort
                                    # path (429 for a `rate_limited` trip). None for the
                                    # other guardrail kinds — serialized absent via
                                    # exclude_none, so existing consumers are unaffected.
                                    api_error_status=reason.api_error_status,
                                )
                            )
                            emitted_terminal = True  # cpp#20 joint 2 mutual-exclusion guard (Site 1)
                            log_guardrail(reason.guardrail, reason.detail)
                            try:
                                await client.interrupt()
                            except Exception:
                                pass
                            return 1

                        # cpp#123: StreamEvent is the highest-volume message on the
                        # stream (`include_partial_messages=True`, agent.py options
                        # below) and carries the only evidence that a turn is still
                        # producing — the turn boundary AssistantMessage keys on does
                        # not arrive until the turn ENDS. Feed content-bearing events
                        # to the guardrail so a long turn is not killed mid-generation.
                        if isinstance(message, StreamEvent):
                            is_progress = _stream_event_is_progress(message)
                            if is_progress:
                                # cpp#145: pass the raw SSE name. The guardrail needs to
                                # tell production from the turn-closing trailers
                                # (`message_delta`/`message_stop`) — both are progress
                                # for rearming the deadline, but only production proves
                                # the model resumed. In three of the six sessions the
                                # ticket is about, those trailers arrive AFTER the tool
                                # result, and counting them as production is what kept
                                # killing them.
                                guardrails.note_stream_activity(_stream_event_type(message))
                            # cpp#125: a StreamEvent no longer falls through the loop
                            # silently. Progress rearms the idle timer above; here we
                            # also leave a debug trace so the highest-volume message on
                            # the stream is observable when the operator asks for it.
                            # Gated on `verbose` (the codebase's debug level) so the
                            # non-verbose path stays flood-free — a turn emits thousands
                            # of these — preserving cpp#123's anti-flood design.
                            if verbose:
                                log_verbose(
                                    f"stream event: {_stream_event_type(message)} "
                                    f"(progress={is_progress})"
                                )
                            continue

                        if isinstance(message, UserMessage):
                            # cpp#125: UserMessage carries tool results — inbound SDK
                            # traffic that proves the session is still alive. Rearm the
                            # idle deadline (without inflating the content-stream
                            # counter, since a tool result is not model production) and
                            # leave a debug trace, so it no longer falls through
                            # silently the way StreamEvent once did.
                            # cpp#145: report HOW MANY tool results this message carried
                            # so a batch of parallel tools is retired together. Counting
                            # one when several returned would leave phantom tools
                            # outstanding and hold the session to the tool ceiling.
                            guardrails.note_activity(_tool_result_count(message))
                            if verbose:
                                log_verbose("user message (tool result) received")
                            continue

                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            session_id = _extract_session_id(message)
                            model = _extract_model(message)
                            if not seen_init:
                                log_init(session_id or "", model or "unknown", task_id)
                                log_prompt(prompt)
                                seen_init = True
                            else:
                                log_reconnect(session_id or "", model or "unknown")
                            continue

                        if isinstance(message, RateLimitEvent):
                            # cpp#119: the CLI emits this whenever subscription rate-limit
                            # state transitions. `status == "rejected"` means the limit is
                            # hit and the SDK is (silently) backing off — arm the guardrail
                            # so a stall firing during that backoff is classified
                            # `rate_limited`, not `idle_timeout`. Any other status is a
                            # recovery/warning that clears the flag.
                            info = message.rate_limit_info
                            rejected = info.status == "rejected"
                            guardrails.note_rate_limit(
                                rejected=rejected,
                                detail=_rate_limit_detail(info) if rejected else None,
                            )
                            if rejected:
                                log_guardrail("rate_limited", _rate_limit_detail(info))
                            continue

                        if isinstance(message, AssistantMessage):
                            session_id = getattr(message, "session_id", session_id) or session_id
                            # cpp#119: an individual turn refused for throttling carries
                            # error=="rate_limit". Treat it as a rate-limit signal too, so
                            # a subsequent idle stall is attributed correctly even if no
                            # RateLimitEvent preceded it.
                            if getattr(message, "error", None) == "rate_limit":
                                guardrails.note_rate_limit(
                                    rejected=True,
                                    detail="Assistant turn refused: rate_limit (429)",
                                )
                            event = guardrails.on_assistant_message(
                                _content_blocks(message),
                                message_id=getattr(message, "message_id", None),
                            )
                            if event is not None:
                                # event.just_closed_turn is the turn that just ENDED;
                                # guardrails.turns now reflects the new turn that just
                                # started. cpp#10 — surface drift turns that produced
                                # no text and no tool calls.
                                _on_boundary(event)
                                # cpp#111 D8-2 Transition 2: turn completion. Throttled
                                # to 1/min so a tool-heavy stream does not flood cm-api.
                                emit_heartbeat_throttled(
                                    f"turn:{event.just_closed_turn}",
                                    throttle_key=_HEARTBEAT_TURN_KEY,
                                    min_interval_secs=_HEARTBEAT_TURN_THROTTLE_SECS,
                                    meta={"task_id": task_id} if task_id else None,
                                )
                            for block in _content_blocks(message):
                                text = _text_of(block)
                                if text:
                                    log_text(text)
                            continue

                        if isinstance(message, ResultMessage):
                            subtype = message.subtype

                            # cpp#151 B2 (AC1): a refusal that `_denial_is_terminal`
                            # judged survivable must not end the session. Decided BEFORE
                            # `close_final_turn` on purpose — that call is idempotent and
                            # latches `_final_turn_closed`, so firing it on a message we
                            # are about to resume past would suppress the marker for the
                            # turn that really is last.
                            #
                            # `break`, not `continue`: the CLI has ALREADY exited by the
                            # time this message is in hand (see the module block above),
                            # so the recovery is a fresh client resuming the session id,
                            # driven by the outer loop. Nothing terminal is emitted here
                            # — the deferred classification below covers the case where
                            # that fresh client never comes up.
                            if deny_resume.should_resume(message, subtype, guardrails, session_id):
                                deny_resume.arm(subtype)
                                break

                            # cpp#10: flush the marker for the still-open final turn
                            # BEFORE _emit_result writes the result JSON line, so the
                            # operator sees the last turn's shape if it was silent.
                            final_event = guardrails.close_final_turn()
                            if final_event is not None:
                                _on_boundary(final_event)

                            raw_errors = getattr(message, "errors", None)
                            errors = (
                                [str(e) for e in raw_errors]
                                if isinstance(raw_errors, list) and raw_errors
                                else None
                            )
                            is_sdk_termination = subtype in SDK_TERMINATION_SUBTYPES
                            status: Literal["success", "error", "terminated"] = (
                                "success"
                                if subtype == "success"
                                else "terminated"
                                if is_sdk_termination
                                else "error"
                            )

                            # mika#940: pipeline-completion contract. If
                            # CLAUDE_PILOT_REQUIRE_PR=1 (set by dispatch-lib for
                            # dev-pilot sessions) and the session completed
                            # "successfully" but never invoked `gh pr create`,
                            # override to a `pipeline_incomplete` failure shape.
                            # Catches the premature-EndTurn family observed on
                            # 2026-05-02 (mika#931, #938, #939) where the model
                            # emits `[done] Success` after Edit/Compound phases
                            # before reaching git push + gh pr create. Defense
                            # in depth with dispatch-lib's actual PR-existence
                            # check on GitHub.
                            termination_reason: str | None = (
                                f"SDK limit reached: {subtype}" if is_sdk_termination else None
                            )

                            # cpp#144: absent-operator question contract. A headless
                            # pilot has nobody to answer `AskUserQuestion`, so the
                            # policy layer (permissions.py) correctly refuses it — and
                            # correctly does NOT abort the run over an ordinary,
                            # non-Bash refusal (cpp#128). The gap this closes is what
                            # happens next: the model can bypass the refusal by
                            # rendering the same question as plain text and ending its
                            # turn there. The SDK sees a clean `ResultMessage`
                            # (subtype "success") — it has no notion that the text WAS
                            # the blocked question. Four of thirty sampled dispatches
                            # did exactly this and rendered `[done] Success` with
                            # nothing delivered (cpp#144 body).
                            #
                            # `guardrails.operator_question_denied` is the structural
                            # signal: it is set only when the SDK itself denied an
                            # AskUserQuestion call (permissions.py, cpp#128's
                            # `[policy:deny]` path) — never a guess about the text of
                            # the model's final turn. Reusing `pr_created` (mika#940)
                            # as the "delivered" check is what makes this additive
                            # rather than punitive: AC2's negative control — a session
                            # that takes the same denial, adapts, and goes on to open
                            # a PR — keeps `status=success` unchanged, because a
                            # refusal only weighs at exit, exactly like mika#940's
                            # existing `pipeline_incomplete` contract below.
                            #
                            # KNOWN LIMITATION (flagged, not fixed, by this change):
                            # two of the four sampled sessions never call
                            # `AskUserQuestion` at all — they emit the question as
                            # text from the start. That shape has no structural signal
                            # on the wire today; catching it would mean pattern-
                            # matching the model's final utterance, which is a
                            # heuristic this change deliberately does not ship
                            # unmeasured (see the accompanying plan doc, "Hors
                            # périmètre"). This branch fires only for the tool-call
                            # half of the ticket's evidence.
                            if (
                                status == "success"
                                and guardrails.operator_question_denied
                                and not guardrails.pr_created
                            ):
                                subtype = "blocked_on_operator_input"
                                status = "error"
                                question = guardrails.operator_question_summary or "(unrecorded)"
                                termination_reason = (
                                    "Session ended after an AskUserQuestion call was denied "
                                    "by policy (headless pilot, no operator present) and no "
                                    "'gh pr create' Bash call followed. Denied question: "
                                    f"{question}"
                                )

                            # cpp#151 B1 (AC2): name the death instead of letting it
                            # share a line with every other SDK-loop failure. Reached
                            # only when the resume above declined or was spent — a
                            # session that resumed and then finished never gets here.
                            #
                            # `status` is untouched: `error_during_execution` is not in
                            # SDK_TERMINATION_SUBTYPES, so it is already "error". Only
                            # the free-form `subtype` carries the new information, which
                            # is the cpp#144 shape exactly.
                            if subtype == EDE_SUBTYPE and guardrails.nonterminal_policy_deny:
                                subtype = EDE_AFTER_DENY_SUBTYPE
                                denied = (
                                    guardrails.nonterminal_policy_deny_summary or "(unrecorded)"
                                )
                                termination_reason = (
                                    "SDK loop ended in error_during_execution after a "
                                    "NON-TERMINAL policy denial the run was supposed to "
                                    f"survive (cpp#128). Last such refusal: {denied}. "
                                    f"Resume attempts spent: {deny_resume.used}/"
                                    f"{deny_resume.budget}."
                                )

                            require_pr = os.environ.get("CLAUDE_PILOT_REQUIRE_PR", "").lower() in (
                                "1",
                                "true",
                            )
                            if status == "success" and require_pr and not guardrails.pr_created:
                                subtype = "pipeline_incomplete"
                                status = "error"
                                termination_reason = (
                                    "Session completed without 'gh pr create' Bash call. "
                                    "CLAUDE_PILOT_REQUIRE_PR=1 was set. "
                                    "Work may be stranded in worktree."
                                )

                            result = ResultJson(
                                status=status,
                                subtype=subtype,
                                task_id=task_id,
                                session_id=session_id or getattr(message, "session_id", None),
                                turns=message.num_turns,
                                cost_usd=message.total_cost_usd or 0.0,
                                duration_ms=message.duration_ms,
                                errors=errors,
                                termination_reason=termination_reason,
                                # cpp#54: deterministic 429/500/529 signal for
                                # downstream classification. getattr-guarded so an SDK
                                # minor lacking the field degrades to None, not a crash.
                                api_error_status=getattr(message, "api_error_status", None),
                            )
                            _emit_result(result)
                            emitted_terminal = True  # cpp#20 joint 2 mutual-exclusion guard (Site 2)

                            # mika#1189: side-channel handoff to the gateway
                            # orchestrator inbox, alongside the existing
                            # mika-platform#100 filesystem-inbox write. Both no-op
                            # silently when their respective env vars are unset.
                            # Failures here MUST NOT change exit code — _emit_result
                            # is the canonical signal.
                            if status == "success":
                                post_handoff(result)

                            if status == "success":
                                log_done(message.num_turns, result.cost_usd, message.duration_ms)
                            elif is_sdk_termination:
                                log_guardrail(
                                    subtype,
                                    f"SDK limit reached after {message.num_turns} turns",
                                )
                                exit_code = 1
                            else:
                                log_error(subtype, errors or [])
                                exit_code = 1
                            continue

                        # cpp#123: terminal branch. Every message type above ends in a
                        # `continue`, so anything reaching here matched no branch. This
                        # bug existed because StreamEvent was discarded silently right
                        # at this spot; name the type once per session so the next
                        # union member the SDK adds is visible on its first occurrence
                        # instead of costing another round of diagnosis.
                        type_name = type(message).__name__
                        if (
                            type_name not in _KNOWN_IGNORED_MESSAGE_TYPES
                            and type_name not in unhandled_message_types
                        ):
                            unhandled_message_types.add(type_name)
                            log_unhandled_message(type_name)

            except Exception as exc:
                if resume_from is None:
                    raise
                log_deny_resume_failed(
                    f"resumed session {resume_from[:8]} did not come up "
                    f"({type(exc).__name__}: {exc}); reporting the deferred result"
                )
                break

            if not deny_resume.armed:
                break
            deny_resume.disarm()
            resume_from = session_id
    finally:
        guardrail_watcher.cancel()
        guardrails.dispose()


    # cpp#20 joint 2 synthetic terminal emit. Fires only when the SDK message
    # stream ended cleanly without yielding either a guardrail trip (Site 1)
    # or a ResultMessage (Site 2). Triggered by:
    #   * `PermissionResultDeny(interrupt=True)` at the can_use_tool boundary
    #     causing the Claude Code CLI to close its stdio pipe without a
    #     terminal ResultMessage (the Case-B failure mode the friend-Claude
    #     review converged on; architect verdict READY).
    #     NARROWED by cpp#128: `interrupt=True` is no longer returned for every
    #     policy denial, only for a destination veto or a tier3-dangerous Bash
    #     command (`permissions._denial_is_terminal`). This guard STAYS — those
    #     two classes still reach it, and so does every transport drop. What
    #     changed is that an ordinary refusal (`echo "label"; cmd`) no longer
    #     arrives here at all: the SDK hands it to the model as a tool_result
    #     error and the run continues to a real ResultMessage.
    #     CAVEAT (cpp#151 review, unverified against a live CLI): an
    #     interrupt-abort may ALSO surface as a `ResultMessage` carrying
    #     `terminal_reason="aborted_tools"` rather than as a silent stream end,
    #     in which case it lands in Site 2 instead of here. Either way it is a
    #     deliberate kill — `_DenyResumeController.should_resume` refuses both
    #     shapes (terminal marker AND `terminal_reason`), so which of the two
    #     paths the CLI takes changes the reported subtype, never the lethality.
    #   * Transport drop / clean upstream close for any other reason.
    # Without this guard cpp would exit silently with empty stdout, and
    # dispatch-lib's `jq -r '.status // empty'` extraction would yield an
    # empty string. With it, downstream parsers always see a `^{` JSON line
    # with status="error" and a non-success subtype.
    if not emitted_terminal and deny_resume.deferred_subtype is not None:
        # cpp#151: a resume was armed, so the ResultMessage branch emitted
        # nothing — and then the resumed session produced no terminal message of
        # its own (the CLI would not come back up, the resume was refused, the
        # transport dropped). Report the death we DEFERRED rather than the
        # generic stream-end below: losing the classification because the
        # recovery failed would trade one invisible death for another.
        duration_ms = int((time.monotonic() - start_time) * 1000)
        denied = guardrails.nonterminal_policy_deny_summary or "(unrecorded)"
        _emit_result(
            ResultJson(
                status="error",
                subtype=EDE_AFTER_DENY_SUBTYPE,
                task_id=task_id,
                session_id=session_id,
                turns=guardrails.turns,
                cost_usd=None,
                duration_ms=duration_ms,
                termination_reason=(
                    "SDK loop ended in error_during_execution after a "
                    "NON-TERMINAL policy denial the run was supposed to survive "
                    f"(cpp#128), and the resumed session produced no terminal "
                    f"message. Last such refusal: {denied}. Resume attempts "
                    f"spent: {deny_resume.used}/{deny_resume.budget}."
                ),
            )
        )
        emitted_terminal = True
        exit_code = 1

    if not emitted_terminal:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        _emit_result(
            ResultJson(
                status="error",
                subtype="stream_ended_without_result",
                task_id=task_id,
                session_id=session_id,
                turns=guardrails.turns,
                cost_usd=None,
                duration_ms=duration_ms,
                termination_reason=(
                    "SDK message stream ended without a terminal ResultMessage. "
                    "Likely caused by permission denial with interrupt=True or "
                    "transport close upstream."
                ),
            )
        )
        exit_code = 1

    return exit_code


_GUARDRAIL_TRIP: Any = object()


def _stream_event_is_progress(message: StreamEvent) -> bool:
    """cpp#123: True when a StreamEvent carries model production.

    `StreamEvent.event` is the raw Anthropic stream event dict. Reads are
    guarded so an SDK shape change degrades to "not progress" — which keeps the
    guardrail conservative — rather than raising inside the message loop.
    """
    event = getattr(message, "event", None)
    if not isinstance(event, dict):
        return False
    return event.get("type") in _PROGRESS_STREAM_EVENT_TYPES


def _tool_result_count(message: UserMessage) -> int:
    """cpp#145: how many tool_result blocks a `UserMessage` carries.

    Guarded the same way as `_stream_event_is_progress`: an unexpected shape
    degrades to 1 rather than raising inside the message loop. 1 is the
    conservative floor — under-counting leaves a tool outstanding and holds the
    session to the (generous) tool ceiling, while over-counting would retire a
    tool that never returned and hand the wait back to the 300s idle budget.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return 1
    count = sum(1 for block in content if _tool_result_block(block))
    return count if count > 0 else 1


def _tool_result_block(block: Any) -> bool:
    """True for a ToolResultBlock, dict-shaped or dataclass (cpp#145)."""
    block_type = getattr(block, "type", None)
    if isinstance(block, dict):
        block_type = block.get("type")
    if isinstance(block_type, str):
        return block_type == "tool_result"
    return type(block).__name__ == "ToolResultBlock"


def _stream_event_type(message: StreamEvent) -> str:
    """cpp#125: the raw Anthropic SSE event type for the debug log line.

    Guarded the same way as `_stream_event_is_progress` so a shape change
    degrades to a readable placeholder instead of raising in the loop.
    """
    event = getattr(message, "event", None)
    if isinstance(event, dict):
        t = event.get("type")
        if isinstance(t, str):
            return t
    return "unknown"


def _rate_limit_detail(info: Any) -> str:
    """cpp#119: human-readable one-liner for a rejected rate-limit signal,
    used both for the operator log line and as the guardrail abort detail.
    Reads only status-level fields (no message content) — safe to log."""
    parts = ["Anthropic rate limit rejected (429)"]
    rl_type = getattr(info, "rate_limit_type", None)
    if rl_type:
        parts.append(f"window={rl_type}")
    resets_at = getattr(info, "resets_at", None)
    if resets_at:
        parts.append(f"resets_at={resets_at}")
    return "; ".join(parts)


async def _merge_stream(
    client: ClaudeSDKClient,
    guardrail_watcher: asyncio.Task[Any],
) -> Any:
    """Yield SDK messages, plus _GUARDRAIL_TRIP sentinel if a guardrail fires.

    Deliberately unchanged by cpp#151, and the reason is worth keeping: this
    generator must never read PAST the ResultMessage that ends a turn.
    `receive_response()` stops there by design, and the SDK's reader queues the
    trailing `ResultError` for whoever reads next
    (`claude_agent_sdk/_internal/query.py`, "it then exits non-zero on
    purpose"). Reopening the iterator on the same client would therefore raise
    that error into the message loop instead of resuming anything. cpp#151's
    recovery is a NEW client resuming the session id, driven by the session
    loop in :func:`_run_agent_inner`.
    """
    stream = client.receive_response().__aiter__()
    while True:
        next_msg = asyncio.ensure_future(stream.__anext__())
        done, _pending = await asyncio.wait(
            {next_msg, guardrail_watcher},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if guardrail_watcher in done and next_msg not in done:
            next_msg.cancel()
            yield _GUARDRAIL_TRIP
            return
        try:
            msg = next_msg.result()
        except StopAsyncIteration:
            return
        yield msg


def _system_prompt_with_hint() -> SystemPromptPreset:
    """System-prompt option that PRESERVES the Claude Code preset and appends
    the mika#1409 denied-Bash prevention hint.

    The preset+append shape is load-bearing: a plain-string ``system_prompt``
    would REPLACE the Claude Code preset, breaking the headless ``/mika`` +
    ``/ce:*`` pipeline that depends on it. ``SystemPromptPreset`` keeps the
    preset and only adds the hint.
    """
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": DENIED_BASH_PATTERNS_HINT,
    }


def _sdk_guardrail_kwargs(config: Any) -> dict[str, Any]:
    """Pass SDK-native guardrails only when > 0."""
    kwargs: dict[str, Any] = {}
    if config.maxTurns > 0:
        kwargs["max_turns"] = config.maxTurns
    # maxBudgetUsd is TS-SDK-specific; the Python SDK accepts it via
    # permission_mode/options extras if exposed. Include defensively.
    if config.maxBudgetUsd > 0:
        # Attribute name varies by SDK version; set if the option exists.
        # Leaving it out is safe — application-level guardrails still apply.
        pass
    return kwargs


def _on_boundary(event: TurnBoundaryEvent) -> None:
    """cpp#10: log a marker for diagnostically silent turns.

    A turn that produced text or a tool_use is already visible in the log via
    `log_text` / `permissions.py`. A turn that produced neither leaves the
    operator with nothing to read — branch the marker on `had_thinking_block`
    so the line accurately names what the model DID do (think) instead of
    falsely claiming silence.
    """
    if event.had_text or event.had_tool_use:
        return
    if event.had_thinking_block:
        log_turn_summary(event.just_closed_turn, "thinking-only, no actions")
    else:
        log_turn_summary(event.just_closed_turn, "no observable output")


def _content_blocks(message: AssistantMessage) -> list[Any]:
    msg = getattr(message, "message", message)
    content = getattr(msg, "content", None) or getattr(message, "content", None)
    return content if isinstance(content, list) else []


def _text_of(block: Any) -> str | None:
    """Extract text from a content block.

    Mirrors `guardrails._block_type` dual-shape handling: SDK dataclass
    instances (TextBlock, etc.) do NOT carry a `type` attribute — the
    wire-format `type` field is consumed by the SDK parser. Fall back on
    class name for dataclass-shaped blocks (cpp#12).
    """
    if isinstance(block, dict):
        if block.get("type") == "text":
            text = block.get("text")
            return text if isinstance(text, str) else None
        return None
    t = getattr(block, "type", None)
    if not isinstance(t, str) and type(block).__name__ == "TextBlock":
        t = "text"
    if t == "text":
        text = getattr(block, "text", None)
        return text if isinstance(text, str) else None
    return None


def _extract_session_id(message: SystemMessage) -> str | None:
    # SDK 0.2.x nests session_id under SystemMessage.data (cpp#55). Read the
    # nested location first, then fall back to a top-level attr so mocks and a
    # future SDK that reverts the nesting both keep working.
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        sid = data.get("session_id")
        if isinstance(sid, str):
            return sid
    sid = getattr(message, "session_id", None)
    return sid if isinstance(sid, str) else None


def _extract_model(message: SystemMessage) -> str | None:
    # SDK 0.2.x nests model under SystemMessage.data (cpp#55); see
    # _extract_session_id for the guarded-access rationale.
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        model = data.get("model")
        if isinstance(model, str):
            return model
    model = getattr(message, "model", None)
    return model if isinstance(model, str) else None


def _emit_result(result: ResultJson) -> None:
    sys.stdout.write(result.to_line() + "\n")
    sys.stdout.flush()
