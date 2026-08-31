"""Agent integration tests — turn-boundary marker logging (cpp#10).

Drives `run_agent` via a fake `ClaudeSDKClient` that yields a scripted sequence
of SDK messages. The seam exercised is `run_agent` ↔ SDK messages ↔ guardrails
↔ ui — the surface that actually breaks in production when a thinking-only
turn leaves the log empty.

Fake-stream over helper extraction: extracting the AssistantMessage loop into a
testable helper would add indirection with one caller. Driving the public
entrypoint with a fake stream keeps production code unchanged and validates
the integrated behavior (cpp#10 plan §Test strategy).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_pilot import agent as agent_module
from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.types import ResolvedGuardrailConfig


def _config() -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        # Disable stall/empty detection so thinking-only runs don't abort early.
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=0,
        minTurnsBeforeDetection=0,
    )


def _assistant(blocks: list[Any], message_id: str) -> AssistantMessage:
    return AssistantMessage(content=blocks, model="claude-test", message_id=message_id)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=1,
        session_id="sess_test",
        total_cost_usd=0.0,
    )


def _init() -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": "sess_test", "model": "claude-test"})


class _FakeClient:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    def receive_response(self) -> Any:
        async def gen() -> Any:
            for m in self._messages:
                yield m

        return gen()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    """Replace ClaudeSDKClient in agent.py with a constructor that returns a
    FakeClient yielding the scripted message sequence."""

    def _factory(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(messages)

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)


async def _noop_permission(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
    raise AssertionError("permission handler must not be invoked in these tests")


@pytest.mark.asyncio
async def test_thinking_only_turns_emit_one_marker_each(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Feed N thinking-only turns (each a distinct message_id) followed by a
    ResultMessage. Expect N `[turn k] thinking-only, no actions` markers — k-1
    from boundary events + 1 from `close_final_turn` (cpp#10 AC 1)."""
    n_turns = 3
    messages: list[Any] = [_init()]
    for i in range(n_turns):
        messages.append(
            _assistant([ThinkingBlock(thinking="planning", signature="sig")], f"msg_{i}")
        )
    messages.append(_result())

    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    captured = capsys.readouterr()
    err = captured.err
    for k in range(1, n_turns + 1):
        assert f"[turn {k}]" in err, f"missing marker for turn {k}; stderr was:\n{err}"
        assert "thinking-only, no actions" in err
    assert exit_code == 0


@pytest.mark.asyncio
async def test_text_and_tool_turn_emits_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A turn carrying [ThinkingBlock, TextBlock, ToolUseBlock] is productive —
    `_on_boundary` suppresses the marker for both the AssistantMessage-driven
    boundary event AND `close_final_turn` (cpp#10 AC 2, marker-suppression
    half).

    NOTE: AC 2 in the plan also reads "assert log contains the text line".
    That assertion exercises `_text_of` in agent.py, which has the same
    SDK-dataclass `type`-attribute gap that `_block_type` worked around in
    cpp#4. That latent bug is out of scope for cpp#10 (the silent-turn marker
    fix) and is left for a follow-up. Only the marker-suppression behavior is
    asserted here.
    """
    text = "here is the plan with enough content to clear text-len threshold"
    messages: list[Any] = [
        _init(),
        _assistant(
            [
                ThinkingBlock(thinking="x", signature="sig"),
                TextBlock(text=text),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
            "msg_1",
        ),
        _assistant(
            [
                ThinkingBlock(thinking="y", signature="sig"),
                TextBlock(text=text),
                ToolUseBlock(id="t2", name="Bash", input={"command": "pwd"}),
            ],
            "msg_2",
        ),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert "[turn 1]" not in err, "productive turn must not emit a marker"
    assert "[turn 2]" not in err, "productive final turn must not emit a marker via close_final_turn"
    assert "thinking-only" not in err
    assert "no observable output" not in err


@pytest.mark.asyncio
async def test_final_thinking_only_turn_marker_fires_via_close_final_turn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single thinking-only AssistantMessage followed immediately by a
    ResultMessage. No subsequent AssistantMessage means the boundary event is
    never emitted from `on_assistant_message` — `close_final_turn()` is the
    only path that fires the marker (cpp#10 AC 1 final-turn coverage)."""
    messages: list[Any] = [
        _init(),
        _assistant([ThinkingBlock(thinking="planning", signature="sig")], "msg_1"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert "[turn 1]" in err, (
        f"close_final_turn must emit the marker for the unclosed final turn; stderr was:\n{err}"
    )
    assert "thinking-only, no actions" in err


@pytest.mark.asyncio
async def test_text_only_final_turn_emits_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NF7 guard: if `close_final_turn` ever forgot to populate `had_text` /
    `had_tool_use`, `_on_boundary` would defensively print 'no observable
    output' for a text+tool final turn. Lock that out (cpp#10 plan NF7)."""
    text = "final productive turn with sufficient observable content"
    messages: list[Any] = [
        _init(),
        _assistant(
            [
                TextBlock(text=text),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
            "msg_1",
        ),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert "[turn 1]" not in err
    assert "no observable output" not in err
    assert "thinking-only" not in err


# ---- cpp#12: _text_of must handle SDK dataclass TextBlock (no `type` attr) ----


def test_text_of_returns_text_for_sdk_dataclass_textblock() -> None:
    """SDK dataclass `TextBlock` instances do not carry a `type` attribute —
    the wire-format `type` is consumed by the parser. `_text_of` must fall
    back on class name so `log_text` fires for production text-emitting turns.
    Regression for cpp#12 (production pilot logs emitting zero [text] lines).
    """
    block = TextBlock(text="hello world")
    assert agent_module._text_of(block) == "hello world"


def test_text_of_returns_text_for_dict_shaped_block() -> None:
    """Dict-shaped blocks (legacy wire-format) must continue to work."""
    block = {"type": "text", "text": "hello world"}
    assert agent_module._text_of(block) == "hello world"


def test_text_of_returns_none_for_non_text_block() -> None:
    """Non-text blocks (ThinkingBlock, ToolUseBlock) must return None."""
    assert agent_module._text_of(ThinkingBlock(thinking="x", signature="sig")) is None
    assert agent_module._text_of(ToolUseBlock(id="t1", name="Bash", input={})) is None


@pytest.mark.asyncio
async def test_single_init_no_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression guard for cpp#7: a normal session with one init must emit
    exactly one `[init]` line and zero `[reconnect]` lines."""
    messages: list[Any] = [_init(), _result()]

    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert err.count("[init]") == 1
    assert err.count("[reconnect]") == 0
    assert exit_code == 0


@pytest.mark.asyncio
async def test_multi_init_logs_reconnect_after_first(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#7: when the SDK emits multiple `SystemMessage(subtype="init")`
    events for a single session (transient reconnects), only the first should
    log `[init]` + `[prompt]`. Subsequent inits log `[reconnect]` instead so
    audits don't see fake re-dispatches.

    Mirrors the original incident shape: three rapid inits in a row.

    Also pins the invariant that `log_prompt` (file-log sink, invisible to
    capsys) is called exactly once across the reconnect sequence — guards
    against a future refactor that moves the prompt emission out of the
    `if not seen_init` branch.
    """
    prompt_calls: list[str] = []

    def _record_prompt(prompt: str) -> None:
        prompt_calls.append(prompt)

    monkeypatch.setattr(agent_module, "log_prompt", _record_prompt)

    messages: list[Any] = [_init(), _init(), _init(), _result()]

    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert err.count("[init]") == 1, f"expected one [init], got:\n{err}"
    assert err.count("[reconnect]") == 2, f"expected two [reconnect], got:\n{err}"
    assert err.index("[init]") < err.index("[reconnect]")
    assert prompt_calls == ["test"], f"expected one log_prompt call, got: {prompt_calls}"
    assert exit_code == 0


# ────────────────────────────────────────────────────────────────────────────
# cpp#20 joint 2 synthetic-emit regression test
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_emits_synthetic_terminal_on_silent_stream_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#20 joint 2 safety contract: when the SDK message stream ends
    without yielding a ResultMessage (the Case-B failure mode introduced
    by ``PermissionResultDeny(interrupt=True)`` at the can_use_tool
    boundary), ``run_agent`` MUST emit a synthetic terminal ResultJson
    to stdout so dispatch-lib's ``grep -m1 '^{' | jq -r '.status'``
    parsing always sees a non-success status. Without this guard the
    pilot would exit silently with empty stdout — the seam joint 2's
    safety story rests on.

    Mock the SDK client to yield only init + assistant messages (no
    ResultMessage), simulating the CLI closing its stdio pipe cleanly
    after the SDK relays interrupt=True. Capture stdout, parse the
    first ``^{`` line, assert it has status="error" and the
    cpp#20-defined subtype.
    """
    import json

    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="going to run a denied tool now")], "msg1"),
        # No ResultMessage — stream just ends. This is the Case-B trigger.
    ]

    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task_synthetic_test",
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    captured = capsys.readouterr()
    # Exactly one terminal JSON line on stdout — no double-emit, no silent exit.
    json_lines = [line for line in captured.out.splitlines() if line.startswith("{")]
    assert len(json_lines) == 1, (
        f"expected exactly one terminal JSON line, got {len(json_lines)}:\n{captured.out!r}"
    )

    payload = json.loads(json_lines[0])
    # dispatch-lib parses .status with `jq -r '.status // empty'` — assert the
    # value is a non-success that maps cleanly.
    assert payload["status"] == "error", (
        f"expected status=error for silent stream end, got {payload!r}"
    )
    assert payload["subtype"] == "stream_ended_without_result", (
        f"expected subtype=stream_ended_without_result, got {payload!r}"
    )
    assert payload["task_id"] == "task_synthetic_test"
    # exit code reflects the silent-stream-end as a non-success run.
    assert exit_code == 1


# ── mika#1409: denied-Bash prevention hint is injected into the system prompt ─


def test_1409_system_prompt_helper_is_preset_append_with_hint() -> None:
    """`_system_prompt_with_hint()` must PRESERVE the claude_code preset and
    append the denied-Bash hint — a plain string would wipe the preset and
    break the headless /mika pipeline."""
    from claude_pilot.tier1 import DENIED_BASH_PATTERNS_HINT

    sp = agent_module._system_prompt_with_hint()
    assert sp["type"] == "preset"
    assert sp["preset"] == "claude_code"
    assert sp["append"] == DENIED_BASH_PATTERNS_HINT
    assert "-exec" in sp["append"] and "Grep" in sp["append"]


@pytest.mark.asyncio
async def test_1409_run_agent_passes_system_prompt_into_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end wiring: run_agent constructs ClaudeAgentOptions with the
    preset-append system_prompt, so every pilot session actually sees the hint.
    """
    captured: dict[str, Any] = {}

    def _capturing_options(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()  # FakeClient ignores options

    monkeypatch.setattr(agent_module, "ClaudeAgentOptions", _capturing_options)
    _install_fake_client(monkeypatch, [_init(), _result()])

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    sp = captured.get("system_prompt")
    assert isinstance(sp, dict)
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert "-exec" in sp["append"] and "Grep" in sp["append"]


# ── cpp#59: ScheduleWakeup headless no-op prevention ──────────────────────────


def test_59_system_prompt_hint_warns_against_schedulewakeup() -> None:
    """The appended system-prompt hint must name ScheduleWakeup, explain the
    headless no-op failure mode, and tell the model not to "wait" for a
    synchronous subagent result. This is the LOAD-BEARING guard (the SDK runtime
    handles ScheduleWakeup internally, bypassing the permission layer — a
    tier1/policy deny cannot catch it)."""
    from claude_pilot.tier1 import DENIED_BASH_PATTERNS_HINT

    sp = agent_module._system_prompt_with_hint()
    append = sp["append"]
    # Regression guard — the preset-append wiring still carries the constant verbatim.
    assert append == DENIED_BASH_PATTERNS_HINT
    # The headless no-op section is present and names the tool.
    assert "ScheduleWakeup" in append
    assert "no-ops in headless mode" in append
    # The "don't wait for a synchronous subagent" guidance is present.
    assert "synchronous" in append
    assert "Agent" in append


@pytest.mark.asyncio
async def test_59_run_agent_disallows_schedulewakeup_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: run_agent constructs ClaudeAgentOptions with
    ScheduleWakeup in disallowed_tools, so where the runtime honors
    --disallowedTools the model never sees the tool."""
    captured: dict[str, Any] = {}

    def _capturing_options(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()  # FakeClient ignores options

    monkeypatch.setattr(agent_module, "ClaudeAgentOptions", _capturing_options)
    _install_fake_client(monkeypatch, [_init(), _result()])

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    disallowed = captured.get("disallowed_tools")
    assert isinstance(disallowed, list)
    assert "ScheduleWakeup" in disallowed


# ── cpp#55: _extract_session_id / _extract_model read SystemMessage.data ──────


def test_extract_session_id_reads_nested_data() -> None:
    """SDK 0.2.x nests session_id under .data — the extractor must read it."""
    msg = SystemMessage(subtype="init", data={"session_id": "abc-123"})
    assert agent_module._extract_session_id(msg) == "abc-123"


def test_extract_model_reads_nested_data() -> None:
    msg = SystemMessage(subtype="init", data={"model": "claude-x"})
    assert agent_module._extract_model(msg) == "claude-x"


def test_extract_falls_back_to_top_level_attr() -> None:
    """Back-compat: an object exposing top-level session_id/model attrs and no
    usable .data still resolves (guards a future SDK that reverts the nesting,
    and any mock built on the pre-0.2 shape)."""
    from types import SimpleNamespace

    stub = SimpleNamespace(session_id="top-sess", model="top-model")
    assert agent_module._extract_session_id(stub) == "top-sess"  # type: ignore[arg-type]
    assert agent_module._extract_model(stub) == "top-model"  # type: ignore[arg-type]


def test_extract_missing_both_returns_none() -> None:
    msg = SystemMessage(subtype="init", data={})
    assert agent_module._extract_session_id(msg) is None
    assert agent_module._extract_model(msg) is None


def test_extract_non_string_nested_value_returns_none() -> None:
    """Type narrowing holds: a non-string nested value yields None, not the int."""
    msg = SystemMessage(subtype="init", data={"session_id": 42, "model": 7})
    assert agent_module._extract_session_id(msg) is None
    assert agent_module._extract_model(msg) is None


# ── cpp#54: ResultMessage.api_error_status flows into ResultJson ──────────────


def _result_with_api_error(status: int) -> ResultMessage:
    return ResultMessage(
        subtype="error",
        duration_ms=100,
        duration_api_ms=50,
        is_error=True,
        num_turns=1,
        session_id="sess_test",
        total_cost_usd=0.0,
        api_error_status=status,
    )


@pytest.mark.asyncio
async def test_api_error_status_flows_to_result_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#54: a ResultMessage carrying api_error_status=429 must surface that
    value on the emitted ResultJson line for deterministic downstream
    classification."""
    import json

    _install_fake_client(monkeypatch, [_init(), _result_with_api_error(429)])

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task_api_err",
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    captured = capsys.readouterr()
    json_lines = [line for line in captured.out.splitlines() if line.startswith("{")]
    payload = json.loads(json_lines[0])
    assert payload["api_error_status"] == 429, payload


@pytest.mark.asyncio
async def test_api_error_status_absent_omits_field(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absent path: the default _result() fixture has no api_error_status, so
    the field is omitted from the JSON line (exclude_none back-compat)."""
    import json

    _install_fake_client(monkeypatch, [_init(), _result()])

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task_no_api_err",
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    captured = capsys.readouterr()
    json_lines = [line for line in captured.out.splitlines() if line.startswith("{")]
    payload = json.loads(json_lines[0])
    assert "api_error_status" not in payload, payload


# ── StreamEvent wiring and the silent-branch closure (cpp#123) ───────────────
#
# `include_partial_messages=True` makes the SDK deliver StreamEvents, but the
# message loop branched on only four of the six `Message` union members.
# StreamEvent fell through every isinstance check and was discarded with no log
# line and no counter — a failure path indistinguishable from a path never
# taken, which is why mika#2029 eliminated six other hypotheses first.


def _stream(event_type: str) -> StreamEvent:
    return StreamEvent(
        uuid="evt_1",
        session_id="sess_test",
        event={"type": event_type},
    )


@pytest.mark.asyncio
async def test_content_stream_events_reach_the_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: content-bearing StreamEvents are fed to the guardrail as intra-turn
    progress. Without this the timer only ever saw turn boundaries."""
    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="I'll start by reading the plan.")], "msg_1"),
        _stream("content_block_start"),
        _stream("content_block_delta"),
        _stream("content_block_delta"),
        _stream("content_block_stop"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert guardrails.stream_activity_count == 4


@pytest.mark.asyncio
async def test_ping_stream_events_do_not_count_as_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AE3 / R3: a keepalive proves the socket is open, not that the model is
    producing. Rearming on `ping` would make the guardrail inert for as long as
    the connection lives — a worse failure than the one being fixed."""
    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="thinking about it")], "msg_1"),
        _stream("ping"),
        _stream("ping"),
        _stream("ping"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert guardrails.stream_activity_count == 0


@pytest.mark.asyncio
async def test_error_stream_event_is_not_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: an SSE `error` event is not production either."""
    messages: list[Any] = [_init(), _stream("error"), _result()]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert guardrails.stream_activity_count == 0


@pytest.mark.asyncio
async def test_unhandled_message_type_is_logged_once_per_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R6: this is the fix for the defect CLASS, not just its instance. A
    message type the loop does not handle must leave a trace on first sight,
    and must not flood the log on repeats."""

    class _UnknownMessage:
        pass

    messages: list[Any] = [
        _init(),
        _UnknownMessage(),
        _UnknownMessage(),
        _UnknownMessage(),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert err.count("_UnknownMessage") == 1, (
        "an unhandled message type logs on first sight, once per type"
    )


@pytest.mark.asyncio
async def test_deliberately_ignored_message_types_stay_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R6 regression: the unhandled-message branch must not fire on the two
    union members the loop ignores on purpose.

    `UserMessage` carries tool results and arrives on essentially every real
    session; a `SystemMessage` with a non-`init` subtype is likewise a
    deliberate skip. If these printed, every session would carry the same two
    lines and a genuinely new union member would be invisible against that
    baseline — costing exactly the diagnosis rounds this branch exists to save.
    """
    messages: list[Any] = [
        _init(),
        UserMessage(content="tool result"),
        SystemMessage(subtype="compact_boundary", data={}),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert "[unhandled]" not in err, (
        "deliberately-ignored union members must not trip the unhandled branch"
    )


@pytest.mark.asyncio
async def test_system_message_subclass_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R6: the ignore-list matches the exact class name, so an SDK
    SystemMessage SUBCLASS (TaskProgressMessage, HookEventMessage, ...) is
    still surfaced — that is a real new member, not a known skip."""

    class TaskProgressMessage(SystemMessage):
        pass

    messages: list[Any] = [
        _init(),
        TaskProgressMessage(subtype="task_progress", data={}),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert "TaskProgressMessage" in capsys.readouterr().err


# ── StreamEvent / UserMessage debug branches + idle wiring (cpp#125) ──────────
#
# cpp#123 wired StreamEvent to the guardrail and closed the silent fall-through
# as a CLASS via the terminal unhandled-message branch. cpp#125 goes one step
# further: StreamEvent and UserMessage each get an explicit branch that leaves a
# debug trace (gated on `verbose`, the codebase's debug level) and UserMessage —
# a tool result, inbound liveness — rearms the idle deadline. The debug gate
# keeps the non-verbose path flood-free (a turn emits thousands of deltas),
# preserving cpp#123's anti-flood design.


class _DelayedClient:
    """Fake client whose stream yields each (delay_secs, message) pair after
    sleeping `delay_secs` first — lets a test span real wall-clock time so the
    idle watchdog can race the message loop the way it does in production."""

    def __init__(self, script: list[tuple[float, Any]]) -> None:
        self._script = script

    async def __aenter__(self) -> _DelayedClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    def receive_response(self) -> Any:
        async def gen() -> Any:
            for delay, msg in self._script:
                if delay:
                    await asyncio.sleep(delay)
                yield msg

        return gen()


def _install_delayed_client(
    monkeypatch: pytest.MonkeyPatch, script: list[tuple[float, Any]]
) -> None:
    def _factory(*_args: Any, **_kwargs: Any) -> _DelayedClient:
        return _DelayedClient(script)

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)


def _idle_config(idle_ms: int, ceiling_ms: int = 1_800_000) -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=idle_ms,
        minTurnsBeforeDetection=0,
        # cpp#133: the throttled-backoff ceiling. Rate-limit end-to-end tests
        # pass a short value so the abort path (which carries api_error_status
        # into ResultJson) still fires; non-throttle idle tests never arm the
        # flag, so the ceiling is irrelevant to them.
        rateLimitCeilingMs=ceiling_ms,
    )


@pytest.mark.asyncio
async def test_stream_event_leaves_a_debug_trace_when_verbose(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#125: a StreamEvent no longer falls through silently — with
    `verbose=True` it names the SSE type and whether it counted as progress."""
    messages: list[Any] = [
        _init(),
        _stream("content_block_delta"),
        _stream("ping"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=True,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    err = capsys.readouterr().err
    assert "stream event: content_block_delta (progress=True)" in err
    assert "stream event: ping (progress=False)" in err


@pytest.mark.asyncio
async def test_stream_event_stays_silent_when_not_verbose(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#125: the debug trace is gated on the debug level — the default path
    stays flood-free even though a real turn emits thousands of deltas."""
    messages: list[Any] = [_init(), _stream("content_block_delta"), _result()]
    _install_fake_client(monkeypatch, messages)

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    err = capsys.readouterr().err
    assert "stream event:" not in err
    assert "[unhandled]" not in err


@pytest.mark.asyncio
async def test_user_message_branch_rearms_idle_and_logs_debug(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#125: UserMessage (a tool result) gets an explicit branch — it rearms
    the idle deadline via `note_activity` and leaves a debug trace when verbose,
    rather than sitting silently in the ignore set."""
    calls: list[str] = []
    guardrails = SessionGuardrails(_config())
    monkeypatch.setattr(
        guardrails, "note_activity", lambda: calls.append("note_activity")
    )

    messages: list[Any] = [
        _init(),
        UserMessage(content="tool result"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=True,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    err = capsys.readouterr().err
    assert calls == ["note_activity"], "UserMessage must rearm the idle deadline"
    assert "user message (tool result) received" in err
    assert "[unhandled]" not in err


@pytest.mark.asyncio
async def test_long_generation_with_deltas_is_not_killed_by_idle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#125 knob-breaking test, end-to-end through the agent loop: a single
    turn whose generation runs PAST the idle threshold while emitting
    StreamEvent deltas (and no turn boundary) must NOT be killed. This is the
    exact mika#2029 failure: duration ~= turns x idleTimeoutMs."""
    # 40 deltas x 3ms = ~120ms of streaming, 2x the 60ms idle budget, with the
    # only AssistantMessage (turn boundary) up front. Gaps are 20x under budget
    # so a loaded runner cannot trip the watchdog on scheduling jitter alone.
    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (0.0, _assistant([TextBlock(text="starting the long turn")], "msg_1")),
    ]
    for _ in range(40):
        script.append((0.003, _stream("content_block_delta")))
    script.append((0.0, _result()))
    _install_delayed_client(monkeypatch, script)
    guardrails = SessionGuardrails(_idle_config(idle_ms=60))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    out = capsys.readouterr().out
    assert guardrails.aborted is False, "a streaming turn must not trip idle_timeout"
    assert "idle_timeout" not in out
    assert exit_code == 0


@pytest.mark.asyncio
async def test_genuine_silence_still_fires_idle_timeout_through_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#125 anti-vacuity, end-to-end: a session that goes quiet — no deltas,
    no turn boundary — past the idle budget must STILL be terminated as
    `idle_timeout`. Proves the knob-breaking test above is not vacuous."""
    # init, then a long silence before the (never-reached) result.
    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (2.0, _result()),  # would arrive far after the 40ms budget expires
    ]
    _install_delayed_client(monkeypatch, script)
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    import json

    out = capsys.readouterr().out
    payload = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert payload["status"] == "terminated"
    assert payload["subtype"] == "idle_timeout"
    assert exit_code == 1


# ── Rate-limited stall classification end-to-end through run_agent (cpp#119) ──
#
# The guardrail UNIT is covered in tests/test_guardrails.py. These tests close
# the integration seam the founding incident (2026-08-06, session c58c49b0-…)
# actually travelled: a throttle signal arrives ON THE SDK MESSAGE STREAM, the
# session goes silent while the bundled SDK backs off between retries, the idle
# watchdog fires between them, and run_agent emits the terminal ResultJson. The
# 429-on-a-stall must surface as `subtype=rate_limited` + `api_error_status=429`
# — a quota refusal named as throttling, not misreported as `idle_timeout`.


def _rejected_rate_limit_event() -> RateLimitEvent:
    """A CLI RateLimitEvent whose subscription window has been rejected (429).
    Mirrors what the Claude Code CLI puts on the stream when the limit is hit
    and the SDK begins its silent backoff."""
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="rejected",
            resets_at=None,
            rate_limit_type="five_hour",
            utilization=1.0,
            overage_status=None,
            overage_resets_at=None,
            overage_disabled_reason=None,
            raw={},
        ),
        uuid="rle_1",
        session_id="sess_test",
    )


@pytest.mark.asyncio
async def test_rate_limit_event_reclassifies_the_stall_through_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#119 end-to-end: a RateLimitEvent(status="rejected") on the stream,
    followed by silence past the idle budget, terminates the session as
    `rate_limited` with api_error_status=429 — NOT `idle_timeout`. This is the
    founding-incident path (2026-08-06), which the terminal-ResultMessage
    source of cpp#54 never reaches because no ResultMessage ever arrives when
    the guardrail fires between the SDK's silent retries."""
    import json

    # init and the throttle signal arrive immediately; the (never-reached)
    # result would land far after the 60ms budget expires, so the watchdog
    # fires during the simulated backoff while the flag is armed.
    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (0.0, _rejected_rate_limit_event()),
        (2.0, _result()),
    ]
    _install_delayed_client(monkeypatch, script)
    # cpp#133: short ceiling so the now-bounded throttle wait terminates as
    # `rate_limited` before the simulated retry (`_result` at 2.0s) lands,
    # keeping this an end-to-end assertion on the abort path's api_error_status.
    guardrails = SessionGuardrails(_idle_config(idle_ms=60, ceiling_ms=30))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task_rl",
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    out = capsys.readouterr().out
    payload = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert payload["status"] == "terminated"
    # The distinction the ticket is about: rate_limited, not idle_timeout.
    assert payload["subtype"] == "rate_limited", payload
    assert payload["api_error_status"] == 429, payload
    assert payload["task_id"] == "task_rl"
    assert exit_code == 1


@pytest.mark.asyncio
async def test_assistant_rate_limit_error_reclassifies_the_stall_through_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#119 end-to-end, second signal source: an AssistantMessage whose
    turn was refused for throttling (error=="rate_limit") arms the guardrail
    even when no RateLimitEvent preceded it, so a following idle stall is
    classified `rate_limited` with api_error_status=429."""
    import json

    refused = AssistantMessage(
        content=[],
        model="claude-test",
        message_id="msg_rl",
        error="rate_limit",
    )
    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (0.0, refused),
        (2.0, _result()),
    ]
    _install_delayed_client(monkeypatch, script)
    # cpp#133: short ceiling — same reason as the RateLimitEvent test above.
    guardrails = SessionGuardrails(_idle_config(idle_ms=60, ceiling_ms=30))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    out = capsys.readouterr().out
    payload = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert payload["status"] == "terminated"
    assert payload["subtype"] == "rate_limited", payload
    assert payload["api_error_status"] == 429, payload
    assert exit_code == 1


@pytest.mark.asyncio
async def test_throttled_session_survives_backoff_and_completes_through_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#133 end-to-end (the founding-incident win): a throttle signal arrives,
    the session goes silent past the idle budget while the SDK backs off, and the
    retry then succeeds. With the ceiling generous relative to the backoff, the
    watchdog must NOT kill the session at the idle deadline — run_agent completes
    `success`. Before #133 this exact path terminated as a stall and lost the
    in-flight work."""
    import json

    # Throttle at 0.0; the retry's result lands at 0.3s — ~5x the 60ms idle
    # budget, well inside the 10s ceiling. The old behaviour aborted at 60ms.
    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (0.0, _rejected_rate_limit_event()),
        (0.3, _result()),
    ]
    _install_delayed_client(monkeypatch, script)
    guardrails = SessionGuardrails(_idle_config(idle_ms=60, ceiling_ms=10_000))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    out = capsys.readouterr().out
    payload = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert payload["status"] == "success", payload
    assert payload["subtype"] == "success", payload
    assert guardrails.aborted is False
    assert exit_code == 0


@pytest.mark.asyncio
async def test_genuine_idle_stall_carries_no_api_error_status_through_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#119 back-compat guard: with NO rate-limit signal on the stream, a
    silent session still terminates as `idle_timeout` AND its ResultJson omits
    `api_error_status` (exclude_none). Proves the reclassification above is
    scoped to the throttled case and does not weaken the genuine-stall path for
    consumers that only know the three original guardrail values."""
    import json

    script: list[tuple[float, Any]] = [
        (0.0, _init()),
        (2.0, _result()),  # far past the 40ms budget
    ]
    _install_delayed_client(monkeypatch, script)
    guardrails = SessionGuardrails(_idle_config(idle_ms=40))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    out = capsys.readouterr().out
    payload = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert payload["status"] == "terminated"
    assert payload["subtype"] == "idle_timeout", payload
    assert "api_error_status" not in payload, payload
    assert exit_code == 1
