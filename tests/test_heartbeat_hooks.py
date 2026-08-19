"""Integration tests for the 4 heartbeat hook sites (cpp#111 D8-2 client).

Verifies that ``run_agent`` (Transitions 1, 2, 4) and
``create_permission_handler`` (Transition 3) fire ``emit_heartbeat`` /
``emit_heartbeat_throttled`` at the intended lifecycle boundaries.

Uses the same fake-client pattern as ``test_agent.py`` — mock
``ClaudeSDKClient`` at the agent module and drive a scripted message stream.
Heartbeats are captured by monkeypatching the two emit symbols exported by
``agent`` and ``permissions`` (rebound at import time).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from claude_pilot import agent as agent_module
from claude_pilot import permissions as permissions_module
from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.heartbeat import reset_throttle_state
from claude_pilot.permissions import create_permission_handler
from claude_pilot.types import (
    PilotConfig,
    PilotEvent,
    PilotResponseAllow,
    ResolvedGuardrailConfig,
    TransportError,
)

# ── Fake SDK client + message helpers (mirrors test_agent.py) ───────────────


def _config() -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
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
    def _factory(*_args: Any, **_kwargs: Any) -> _FakeClient:
        return _FakeClient(messages)

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)


async def _noop_permission(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
    raise AssertionError("permission handler must not be invoked in these tests")


class _HeartbeatRecorder:
    """Collects (reason, entity, meta) tuples for each emit call.

    Used to substitute both ``emit_heartbeat`` and ``emit_heartbeat_throttled``
    so tests can assert exact call-shape without hitting the network.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[str, Any] | None]] = []
        self.throttled_calls: list[tuple[str, str, float]] = []

    def emit(
        self,
        reason: str,
        *,
        entity: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        self.calls.append((reason, entity, meta))
        return True

    def emit_throttled(
        self,
        reason: str,
        *,
        throttle_key: str,
        min_interval_secs: float,
        entity: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        # Throttled calls are tracked separately so tests can distinguish
        # per-turn from unthrottled transitions. Every call routes through
        # the plain-emit recorder too so the aggregate order is preserved.
        self.throttled_calls.append((reason, throttle_key, min_interval_secs))
        self.calls.append((reason, entity, meta))
        return True


@pytest.fixture(autouse=True)
def _clean_throttle_state() -> None:
    """Prevent per-key rate-limit state from leaking between tests."""
    reset_throttle_state()


# ── Transitions 1, 2, 4: agent lifecycle ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_fires_session_and_complete_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minimal session (init + one text turn + result) fires:
    Transition 1 — ``session:<task_id>``
    Transition 4 — ``complete:<task_id>``
    """
    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="hello world enough content")], "msg_1"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(agent_module, "emit_heartbeat", recorder.emit)
    monkeypatch.setattr(agent_module, "emit_heartbeat_throttled", recorder.emit_throttled)
    guardrails = SessionGuardrails(_config())

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="mika#1878",
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert exit_code == 0
    reasons = [reason for reason, _entity, _meta in recorder.calls]
    assert "session:mika#1878" in reasons, f"missing session-start; got {reasons}"
    assert "complete:mika#1878" in reasons, f"missing session-end; got {reasons}"
    # session must precede complete
    assert reasons.index("session:mika#1878") < reasons.index("complete:mika#1878")
    # complete carries the exit_code in meta
    complete_call = next(
        (call for call in recorder.calls if call[0] == "complete:mika#1878"),
        None,
    )
    assert complete_call is not None
    _, _, meta = complete_call
    assert meta is not None and meta.get("exit_code") == 0


@pytest.mark.asyncio
async def test_run_agent_uses_unknown_task_id_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="hello")], "msg_1"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(agent_module, "emit_heartbeat", recorder.emit)
    monkeypatch.setattr(agent_module, "emit_heartbeat_throttled", recorder.emit_throttled)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    reasons = [reason for reason, _entity, _meta in recorder.calls]
    assert "session:unknown" in reasons
    assert "complete:unknown" in reasons


@pytest.mark.asyncio
async def test_run_agent_fires_per_turn_heartbeat_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transition 2: each turn boundary invokes emit_heartbeat_throttled with
    the shared throttle key so a tool-heavy stream can't flood cm-api.

    Assertion is on the CALL — the throttle itself is unit-tested in
    test_heartbeat.py. Here we verify the wire: right throttle_key, right
    reason format, all turn boundaries route through the throttled path.
    """
    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="first turn text content")], "msg_1"),
        _assistant([TextBlock(text="second turn text content")], "msg_2"),
        _assistant([TextBlock(text="third turn text content")], "msg_3"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(agent_module, "emit_heartbeat", recorder.emit)
    monkeypatch.setattr(agent_module, "emit_heartbeat_throttled", recorder.emit_throttled)
    guardrails = SessionGuardrails(_config())

    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="t-42",
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    # Turn boundaries fire between messages (2 boundaries for 3 msgs), so
    # expect at least 2 throttled per-turn emits. Reason format is
    # `turn:<n>` where n is the just-closed turn number.
    turn_reasons = [reason for reason, *_ in recorder.throttled_calls]
    assert len(turn_reasons) >= 2, f"expected multiple per-turn throttled emits, got {turn_reasons}"
    for reason in turn_reasons:
        assert reason.startswith("turn:"), f"bad reason shape: {reason}"

    # All per-turn emits share the same throttle_key
    keys = {throttle_key for _, throttle_key, _ in recorder.throttled_calls}
    assert keys == {"pilot:turn"}, f"multiple keys leaked: {keys}"

    # All per-turn emits use the same 60s interval
    intervals = {interval for _, _, interval in recorder.throttled_calls}
    assert intervals == {60.0}, f"unexpected intervals: {intervals}"


@pytest.mark.asyncio
async def test_run_agent_complete_heartbeat_fires_on_guardrail_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transition 4 must fire on the early-return path where a guardrail
    aborts the session, not just on natural completion."""
    messages: list[Any] = [_init()]  # no result — the guardrail trip is the terminal event
    _install_fake_client(monkeypatch, messages)
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(agent_module, "emit_heartbeat", recorder.emit)
    monkeypatch.setattr(agent_module, "emit_heartbeat_throttled", recorder.emit_throttled)

    # Force a guardrail trip by pre-setting the abort event.
    guardrails = SessionGuardrails(_config())
    from claude_pilot.types import GuardrailAbortReason

    guardrails._abort_reason = GuardrailAbortReason(  # type: ignore[assignment]
        guardrail="idle_timeout",
        turns=0,
        detail="forced abort for test",
    )
    guardrails._abort_event.set()

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="t-guardrail",
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert exit_code == 1
    reasons = [reason for reason, *_ in recorder.calls]
    assert "session:t-guardrail" in reasons
    assert "complete:t-guardrail" in reasons


@pytest.mark.asyncio
async def test_run_agent_complete_heartbeat_fires_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashing SDK still fires the ``complete`` heartbeat via finally so
    cm-side never sees a silent-nocturne pilot on an unhandled exception."""

    def _crashing_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("SDK client construction blew up")

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _crashing_factory)
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(agent_module, "emit_heartbeat", recorder.emit)
    monkeypatch.setattr(agent_module, "emit_heartbeat_throttled", recorder.emit_throttled)
    guardrails = SessionGuardrails(_config())

    with pytest.raises(RuntimeError):
        await run_agent(
            prompt="test",
            cwd=".",
            verbose=False,
            task_id="t-crash",
            permission_handler=_noop_permission,
            guardrails=guardrails,
        )

    reasons = [reason for reason, *_ in recorder.calls]
    assert "session:t-crash" in reasons
    assert "complete:t-crash" in reasons


# ── Transition 3: tool-call recovery in permission handler ──────────────────


def test_permission_handler_fires_recovery_heartbeat_when_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transition 3: the first relay invocation raises TransportError, the
    bounded retry succeeds — ``emit_heartbeat("recovery:tool", ...)`` fires
    exactly once. Fabricates the retry sequence by stubbing invoke_command."""
    monkeypatch.setenv("MIKA_PILOT_POLICY_DISABLED", "1")
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(permissions_module, "emit_heartbeat", recorder.emit)

    call_count = [0]

    async def _fake_invoke(
        _config: PilotConfig, _event: PilotEvent, *_a: object
    ) -> PilotResponseAllow:
        call_count[0] += 1
        if call_count[0] == 1:
            raise TransportError("first attempt blew up")
        return PilotResponseAllow(action="allow")

    monkeypatch.setattr(permissions_module, "invoke_command", _fake_invoke)

    handler = create_permission_handler(
        config=PilotConfig(command="true"),
        relay=True,
        verbose=False,
        cwd="/tmp",
    )
    ctx = ToolPermissionContext(signal=None, suggestions=[], tool_use_id="tool_x", agent_id=None)

    # rm -rf / misses Tier 1 / Tier 1.5; policy disabled → reaches relay.
    asyncio.run(handler("Bash", {"command": "rm -rf /"}, ctx))

    assert call_count[0] == 2, "test setup: retry should have fired once"
    recovery_calls = [call for call in recorder.calls if call[0] == "recovery:tool"]
    assert len(recovery_calls) == 1, (
        f"expected exactly one recovery emit; recorder saw {recorder.calls}"
    )
    _, _, meta = recovery_calls[0]
    assert meta is not None
    assert meta.get("tool") == "Bash"
    assert meta.get("action") == "allow"


def test_permission_handler_no_recovery_emit_on_first_try_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: a happy-path first-try allow does NOT fire the recovery
    heartbeat. Otherwise every allow would be tagged as a recovery and cm
    couldn't distinguish real recoveries from routine traffic."""
    monkeypatch.setenv("MIKA_PILOT_POLICY_DISABLED", "1")
    recorder = _HeartbeatRecorder()
    monkeypatch.setattr(permissions_module, "emit_heartbeat", recorder.emit)

    async def _fake_invoke(
        _config: PilotConfig, _event: PilotEvent, *_a: object
    ) -> PilotResponseAllow:
        return PilotResponseAllow(action="allow")

    monkeypatch.setattr(permissions_module, "invoke_command", _fake_invoke)

    handler = create_permission_handler(
        config=PilotConfig(command="true"),
        relay=True,
        verbose=False,
        cwd="/tmp",
    )
    ctx = ToolPermissionContext(signal=None, suggestions=[], tool_use_id="tool_x", agent_id=None)
    asyncio.run(handler("Bash", {"command": "rm -rf /"}, ctx))

    assert not any(call[0] == "recovery:tool" for call in recorder.calls), (
        f"first-try success must not emit recovery; recorder saw {recorder.calls}"
    )


# Suppress "unused import" lint for symbols referenced only in test bodies.
_ = ToolUseBlock
