"""Dollar cap reaches the CLI — cpp#197.

`_sdk_guardrail_kwargs` used to stop at `pass` for `maxBudgetUsd`, so the
`error_max_budget_usd` termination subtype had no emitter. These tests drive
the real `run_agent` entrypoint and capture the `ClaudeAgentOptions` it hands
to `ClaudeSDKClient`, then build the CLI command from those options: the proof
is that the cap reaches `--max-budget-usd`, not that a helper returns a dict.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import ResultMessage, SystemMessage

from claude_pilot import agent as agent_module
from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.types import ResolvedGuardrailConfig

from .test_agent import _FakeClient, _noop_permission, _terminal_payload


def _config(max_budget_usd: float) -> ResolvedGuardrailConfig:
    return ResolvedGuardrailConfig(
        maxTurns=200,
        maxBudgetUsd=max_budget_usd,
        stallThreshold=0,
        emptyResponseThreshold=0,
        idleTimeoutMs=0,
        minTurnsBeforeDetection=0,
    )


def _init() -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": "sess_test", "model": "claude-test"})


def _result(subtype: str, cost: float) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=100,
        duration_api_ms=50,
        is_error=subtype != "success",
        num_turns=3,
        session_id="sess_test",
        total_cost_usd=cost,
        errors=None if subtype == "success" else [f"Reached maximum budget (${cost})"],
    )


async def _run_capturing_options(
    monkeypatch: pytest.MonkeyPatch, max_budget_usd: float, result: ResultMessage
) -> tuple[ClaudeAgentOptions, int]:
    captured: list[ClaudeAgentOptions] = []

    def _factory(*_args: Any, **kwargs: Any) -> _FakeClient:
        captured.append(kwargs["options"])
        return _FakeClient([_init(), result])

    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _factory)
    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config(max_budget_usd)),
    )
    assert len(captured) == 1
    return captured[0], exit_code


def _cli_command(options: ClaudeAgentOptions) -> list[str]:
    transport = SubprocessCLITransport(
        prompt="test", options=dataclasses.replace(options, cli_path="/bin/claude")
    )
    return transport._build_command()


@pytest.mark.asyncio
async def test_armed_cap_reaches_the_cli_command(monkeypatch: pytest.MonkeyPatch) -> None:
    options, _ = await _run_capturing_options(monkeypatch, 40.0, _result("success", 1.0))

    assert options.max_budget_usd == 40.0
    cmd = _cli_command(options)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "40.0"


@pytest.mark.asyncio
async def test_disarmed_cap_stays_off_the_cli_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 is the shipped default and means disabled: no flag, no cap."""
    options, _ = await _run_capturing_options(monkeypatch, 0.0, _result("success", 1.0))

    assert options.max_budget_usd is None
    assert "--max-budget-usd" not in _cli_command(options)


@pytest.mark.asyncio
async def test_budget_result_is_a_clean_termination(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the CLI emits on crossing the cap surfaces as `terminated`, with
    the subtype and the cost the engine's `pilot_cost_overrun` reads."""
    await _run_capturing_options(monkeypatch, 40.0, _result("error_max_budget_usd", 40.12))

    payload = _terminal_payload(capsys.readouterr().out)
    assert payload["status"] == "terminated"
    assert payload["subtype"] == "error_max_budget_usd"
    assert payload["cost_usd"] == pytest.approx(40.12)
