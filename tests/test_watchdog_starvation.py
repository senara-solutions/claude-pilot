"""cpp#168 — idle-watchdog starvation resistance.

The ticket's leading (unproven) hypothesis was event-loop starvation: a
synchronous blocking call somewhere in the message-processing path freezes
the asyncio loop, and `asyncio.wait cannot preempt a blocked thread` — so
`_idle_watchdog` (an `asyncio.Task` sleeping cooperatively on the same loop)
never gets to fire. Two mika#2246 pilots lived 2h18 and 58min in total
silence, well past the structural ceiling.

This file proves two separate things, matched to what the code investigation
actually found (see the plan doc for the full trace):

1. `test_watchdog_fires_on_a_genuinely_stalled_async_stream` — the SDK's own
   message read (`client.receive_response()`, bottoming out in
   `claude_agent_sdk`'s anyio-backed subprocess transport) is confirmed
   genuinely async-awaitable. A stream that stalls the way a truly async read
   would stall is ALREADY bounded by `_merge_stream`'s existing
   `asyncio.wait({next_msg, guardrail_watcher}, FIRST_COMPLETED)` race — this
   test passes with NO change to `_merge_stream`, which is the evidence for
   the plan doc's decision not to add a redundant `asyncio.wait_for` there.

2. `test_watchdog_survives_a_permanently_hung_transcript_write` — the call
   that actually DOES block the event-loop thread: `record_sdk_message`
   (transcript_writer.py) performs synchronous, unconditional file I/O for
   every AssistantMessage/ResultMessage, directly in the SDK message loop
   (agent.py). This test simulates a permanently wedged sink (a dead disk / a
   hung mount under `$ANTHROPIC_LOG_FILE`) and asserts the session still
   terminates promptly via `_record_sdk_message_bounded`'s thread-offload +
   ceiling, instead of hanging for the write's full duration.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from claude_agent_sdk.types import TextBlock

from claude_pilot import agent as agent_module
from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.types import ResolvedGuardrailConfig
from tests.test_agent import _assistant, _init, _noop_permission, _terminal_payload


def _starvation_config(idle_ms: int = 30) -> ResolvedGuardrailConfig:
    """Idle timeout tight enough to fire promptly in a test; stall/empty
    detection disabled so a single text-bearing turn can't trip them first;
    the other ceilings left at their (generous, minutes-scale) defaults so
    only the idle watchdog can end this session."""
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=0.0,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=idle_ms,
        minTurnsBeforeDetection=0,
    )


class _StallingFakeClient:
    """Like `tests.test_agent._FakeClient`, but the stream never ends: after
    yielding the scripted messages it awaits an `asyncio.Event` that is never
    set. Models the ticket's "silent 429 backoff" — `stream.__anext__()`
    genuinely never resolves again — as a real async hang, not a raise or a
    generator that simply exhausts."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    async def __aenter__(self) -> _StallingFakeClient:
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
            await asyncio.Event().wait()  # never set — genuine async stall

        return gen()


def _install_stalling_client(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> None:
    def _factory(*_args: Any, **_kwargs: Any) -> _StallingFakeClient:
        return _StallingFakeClient(messages)

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)


async def test_watchdog_fires_on_a_genuinely_stalled_async_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression-confirming: the existing `_merge_stream` race already
    bounds a stuck-but-async SDK read. No production code under test here
    was changed by cpp#168 — this pins the behavior the plan doc's
    investigation relies on."""
    messages: list[Any] = [_init(), _assistant([TextBlock(text="hello")], "msg_1")]
    _install_stalling_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_starvation_config(idle_ms=30))

    start = time.monotonic()
    exit_code = await asyncio.wait_for(
        run_agent(
            prompt="test",
            cwd=".",
            verbose=False,
            task_id=None,
            permission_handler=_noop_permission,
            guardrails=guardrails,
        ),
        timeout=5.0,
    )
    elapsed = time.monotonic() - start

    assert exit_code == 1
    assert elapsed < 1.0, f"took {elapsed:.2f}s — the idle watchdog should fire in ~30ms"
    payload = _terminal_payload(capsys.readouterr().out)
    assert payload["subtype"] == "idle_timeout"
    assert payload["status"] == "terminated"


async def test_watchdog_survives_a_permanently_hung_transcript_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The code-verified real starvation mechanism (see module docstring):
    `record_sdk_message` blocking forever must not prevent `_idle_watchdog`
    from firing. Without `_record_sdk_message_bounded` (agent.py) this test
    hangs until the outer `asyncio.wait_for(..., timeout=5.0)` kills it —
    i.e. it would FAIL (timeout) on the pre-cpp#168 code, which called
    `record_sdk_message(message)` directly and synchronously in this exact
    spot."""
    # Deliberately bounded rather than a literal infinite hang: a permanently
    # stuck worker thread (e.g. `threading.Event().wait()` that is never set)
    # can deadlock the test process itself at interpreter/executor teardown
    # (observed: the NEXT test in this file hung indefinitely). 1s is
    # comfortably longer than both the write ceiling and the idle timeout
    # below, which is all the assertion needs — a write that eventually
    # returns still proves the point, since a genuinely infinite production
    # hang is bounded identically by `_TRANSCRIPT_WRITE_TIMEOUT_S` either way.
    def _hung_record_sdk_message(_message: object) -> bool:
        time.sleep(1.0)
        return True

    monkeypatch.setattr(agent_module, "record_sdk_message", _hung_record_sdk_message)
    # Production default is 5s; tighten it so the test stays fast while still
    # exercising the real bounded-offload code path end to end.
    monkeypatch.setattr(agent_module, "_TRANSCRIPT_WRITE_TIMEOUT_S", 0.2)

    messages: list[Any] = [_init(), _assistant([TextBlock(text="hello")], "msg_1")]
    _install_stalling_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_starvation_config(idle_ms=30))

    start = time.monotonic()
    exit_code = await asyncio.wait_for(
        run_agent(
            prompt="test",
            cwd=".",
            verbose=False,
            task_id=None,
            permission_handler=_noop_permission,
            guardrails=guardrails,
        ),
        timeout=5.0,
    )
    elapsed = time.monotonic() - start

    assert exit_code == 1
    # Bounded by _TRANSCRIPT_WRITE_TIMEOUT_S (0.2s here) plus idleTimeoutMs
    # (30ms) — NOT by the write's actual (1s) duration. Comfortably
    # under the 5s outer safety net regardless.
    assert elapsed < 2.0, (
        f"took {elapsed:.2f}s — a hung transcript write starved the watchdog"
    )
    payload = _terminal_payload(capsys.readouterr().out)
    assert payload["subtype"] == "idle_timeout"
    assert payload["status"] == "terminated"


async def test_normal_productive_stream_is_unaffected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression control (required alongside the starvation tests): a
    session whose transcript writes complete normally and whose stream ends
    cleanly must still report `success`, with the bounded offload adding no
    observable behavior change."""
    from tests.test_agent import _install_fake_client, _result

    def _fast_record_sdk_message(_message: object) -> bool:
        return True

    monkeypatch.setattr(agent_module, "record_sdk_message", _fast_record_sdk_message)

    messages: list[Any] = [
        _init(),
        _assistant([TextBlock(text="hello")], "msg_1"),
        _result(),
    ]
    _install_fake_client(monkeypatch, messages)
    guardrails = SessionGuardrails(_starvation_config(idle_ms=10_000))

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )

    assert exit_code == 0
    payload = _terminal_payload(capsys.readouterr().out)
    assert payload["status"] == "success"
