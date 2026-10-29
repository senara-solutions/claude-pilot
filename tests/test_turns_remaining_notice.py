"""Turns-remaining notice — cpp#289.

The pilot injects, once, a short notice when the main loop reaches
`maxTurns - TURNS_REMAINING_NOTICE`. The hook is taken from the
`ClaudeAgentOptions` that `run_agent` hands to `ClaudeSDKClient`, so these
tests exercise the hook the session actually registers, fed by a real
`SessionGuardrails` turn counter.
"""

from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ResultMessage, SystemMessage

from claude_pilot import agent as agent_module
from claude_pilot.agent import (
    TURNS_REMAINING_NOTICE,
    run_agent,
    turns_remaining_notice_text,
)
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.types import ResolvedGuardrailConfig

from .test_agent import _FakeClient, _noop_permission


def _config(max_turns: int) -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=max_turns,
        maxBudgetUsd=0.0,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=0,
        minTurnsBeforeDetection=0,
    )


async def _session(
    monkeypatch: pytest.MonkeyPatch, max_turns: int
) -> tuple[SessionGuardrails, ClaudeAgentOptions]:
    captured: list[ClaudeAgentOptions] = []

    def _factory(*_args: Any, **kwargs: Any) -> _FakeClient:
        captured.append(kwargs["options"])
        return _FakeClient(
            [
                SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.0,
                ),
            ]
        )

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)
    guardrails = SessionGuardrails(_config(max_turns))
    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=guardrails,
    )
    return guardrails, captured[0]


def _hook(options: ClaudeAgentOptions, event: str) -> Any:
    assert options.hooks is not None
    (matcher,) = options.hooks[event]  # type: ignore[index]
    (hook,) = matcher.hooks
    return hook


def _advance_to(guardrails: SessionGuardrails, turn: int) -> None:
    """Close main-loop turns until `guardrails.turns == turn`."""
    while guardrails.turns < turn:
        n = guardrails.turns
        guardrails.on_assistant_message([{"type": "tool_use"}], message_id=f"msg_{n}")
    assert guardrails.turns == turn


def _input(event: str = "PostToolUse", **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "tool_name": "Bash", "session_id": "s", **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("max_turns", [150, 200])
async def test_notice_fires_exactly_once_at_the_threshold_and_never_below(
    monkeypatch: pytest.MonkeyPatch, max_turns: int
) -> None:
    guardrails, options = await _session(monkeypatch, max_turns)
    hook = _hook(options, "PostToolUse")
    threshold = max_turns - TURNS_REMAINING_NOTICE

    # A flight that stays below the threshold never gets it.
    _advance_to(guardrails, threshold - 1)
    for _ in range(3):
        assert await hook(_input(), "t", {}) == {}

    # Crossing it: exactly one injection, carrying the turns left.
    _advance_to(guardrails, threshold)
    first = await hook(_input(), "t", {})
    out = first["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert out["additionalContext"] == turns_remaining_notice_text(TURNS_REMAINING_NOTICE)

    _advance_to(guardrails, max_turns - 1)
    assert await hook(_input(), "t", {}) == {}


@pytest.mark.asyncio
async def test_a_failed_tool_turn_still_crosses_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardrails, options = await _session(monkeypatch, 200)
    _advance_to(guardrails, 160)

    out = (await _hook(options, "PostToolUseFailure")(_input("PostToolUseFailure"), "t", {}))[
        "hookSpecificOutput"
    ]
    assert out["hookEventName"] == "PostToolUseFailure"
    assert "40 turns left" in out["additionalContext"]


@pytest.mark.asyncio
async def test_both_events_share_one_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    guardrails, options = await _session(monkeypatch, 200)
    _advance_to(guardrails, 150)

    assert await _hook(options, "PostToolUseFailure")(_input("PostToolUseFailure"), "t", {})
    assert await _hook(options, "PostToolUse")(_input(), "t", {}) == {}


@pytest.mark.asyncio
async def test_a_subagent_tool_call_neither_receives_nor_spends_the_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardrails, options = await _session(monkeypatch, 200)
    hook = _hook(options, "PostToolUse")
    _advance_to(guardrails, 150)

    assert await hook(_input(agent_id="sub_1", agent_type="reviewer"), "t", {}) == {}
    assert await hook(_input(), "t", {}) != {}


@pytest.mark.asyncio
async def test_a_cap_within_the_notice_distance_is_disarmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardrails, options = await _session(monkeypatch, TURNS_REMAINING_NOTICE)
    hook = _hook(options, "PostToolUse")
    for turn in (1, TURNS_REMAINING_NOTICE - 1):
        _advance_to(guardrails, turn)
        assert await hook(_input(), "t", {}) == {}


@pytest.mark.asyncio
async def test_the_injection_is_logged_as_a_guardrail_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    guardrails, options = await _session(monkeypatch, 150)
    _advance_to(guardrails, 100)
    capsys.readouterr()

    await _hook(options, "PostToolUse")(_input(), "t", {})

    err = capsys.readouterr().err
    assert "[guardrail]" in err
    assert "turns_remaining_notice" in err
    assert "turn 100/150" in err


def test_the_notice_text_is_thin() -> None:
    """Every word is re-read by the cache on each later turn."""
    text = turns_remaining_notice_text(TURNS_REMAINING_NOTICE)
    assert "50 turns left" in text
    assert "Commit, push" in text
    assert "PR" in text
    assert len(text) <= 160
