"""Schema round-trip + defaults tests for types.py."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from claude_pilot.types import (
    GUARDRAIL_DEFAULTS,
    GuardrailAbortReason,
    GuardrailConfig,
    PilotConfig,
    PilotEvent,
    PilotResponseAllow,
    PilotResponseAnswer,
    PilotResponseDeny,
    ResultJson,
)


def test_guardrail_defaults() -> None:
    assert GUARDRAIL_DEFAULTS.maxTurns == 200
    assert GUARDRAIL_DEFAULTS.maxBudgetUsd == 0.0
    assert GUARDRAIL_DEFAULTS.stallThreshold == 5
    assert GUARDRAIL_DEFAULTS.emptyResponseThreshold == 5
    assert GUARDRAIL_DEFAULTS.idleTimeoutMs == 300_000
    assert GUARDRAIL_DEFAULTS.minTurnsBeforeDetection == 10
    # cpp#133: throttled-backoff ceiling (30 min).
    assert GUARDRAIL_DEFAULTS.rateLimitCeilingMs == 1_800_000
    # cpp#145: the two wait ceilings. Pinned because they are the ONLY thing
    # standing between a waiting session and immortality — a silent default
    # change would remove the bound without any test noticing.
    assert GUARDRAIL_DEFAULTS.toolWaitCeilingMs == 1_800_000
    assert GUARDRAIL_DEFAULTS.modelWaitCeilingMs == 900_000


def test_pilot_config_minimal() -> None:
    cfg = PilotConfig.model_validate({"command": "mika"})
    assert cfg.command == "mika"
    assert cfg.args is None
    assert cfg.timeout is None
    assert cfg.guardrails is None


def test_pilot_config_full() -> None:
    cfg = PilotConfig.model_validate({
        "command": "mika",
        "args": ["--agent", "mika-dev", "ask"],
        "timeout": 120000,
        "guardrails": {"maxTurns": 50, "stallThreshold": 3},
    })
    assert cfg.args == ["--agent", "mika-dev", "ask"]
    assert cfg.timeout == 120000
    assert cfg.guardrails is not None
    assert cfg.guardrails.maxTurns == 50


def test_pilot_config_rejects_timeout_out_of_range() -> None:
    with pytest.raises(ValidationError):
        PilotConfig.model_validate({"command": "mika", "timeout": 999})  # below min
    with pytest.raises(ValidationError):
        PilotConfig.model_validate({"command": "mika", "timeout": 700_000})  # above max


def test_pilot_config_rejects_empty_command() -> None:
    with pytest.raises(ValidationError):
        PilotConfig.model_validate({"command": ""})


def test_guardrail_config_idle_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        GuardrailConfig.model_validate({"idleTimeoutMs": 4_000_000})  # above 1h cap


def test_guardrail_config_rate_limit_ceiling_bounds() -> None:
    # cpp#133: accepts a valid ceiling and rejects one above the 6h zombie cap.
    cfg = GuardrailConfig.model_validate({"rateLimitCeilingMs": 900_000})
    assert cfg.rateLimitCeilingMs == 900_000
    with pytest.raises(ValidationError):
        GuardrailConfig.model_validate({"rateLimitCeilingMs": 21_600_001})


def test_guardrail_config_wait_ceiling_bounds() -> None:
    # cpp#145: same shape and same 6h zombie cap as the cpp#133 ceiling above.
    cfg = GuardrailConfig.model_validate(
        {"toolWaitCeilingMs": 900_000, "modelWaitCeilingMs": 600_000}
    )
    assert cfg.toolWaitCeilingMs == 900_000
    assert cfg.modelWaitCeilingMs == 600_000
    with pytest.raises(ValidationError):
        GuardrailConfig.model_validate({"toolWaitCeilingMs": 21_600_001})
    with pytest.raises(ValidationError):
        GuardrailConfig.model_validate({"modelWaitCeilingMs": 21_600_001})


def test_guardrail_abort_reason_accepts_the_wait_vocabulary() -> None:
    """cpp#145: `awaiting_tool` / `awaiting_model` are part of the wire
    vocabulary that reaches `ResultJson.subtype`. Pinned so the two Literal
    unions (here and in `SessionGuardrails._abort`) cannot drift apart."""
    for reason in ("awaiting_tool", "awaiting_model"):
        parsed = GuardrailAbortReason.model_validate(
            {"guardrail": reason, "turns": 3, "detail": "d"}
        )
        assert parsed.guardrail == reason
        assert parsed.api_error_status is None
    with pytest.raises(ValidationError):
        GuardrailAbortReason.model_validate(
            {"guardrail": "awaiting_something_else", "turns": 3, "detail": "d"}
        )


def test_pilot_event_round_trip() -> None:
    evt = PilotEvent(
        type="permission",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tool_123",
    )
    raw = evt.model_dump_json(exclude_none=True)
    parsed = json.loads(raw)
    assert parsed == {
        "type": "permission",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_use_id": "tool_123",
    }


def test_pilot_response_discriminated_union() -> None:
    from pydantic import TypeAdapter

    from claude_pilot.types import PilotResponse

    adapter: TypeAdapter[PilotResponse] = TypeAdapter(PilotResponse)

    assert isinstance(adapter.validate_python({"action": "allow"}), PilotResponseAllow)
    assert isinstance(
        adapter.validate_python({"action": "deny", "message": "no"}),
        PilotResponseDeny,
    )
    assert isinstance(
        adapter.validate_python({"action": "answer", "answers": {"q": "a"}}),
        PilotResponseAnswer,
    )

    with pytest.raises(ValidationError):
        adapter.validate_python({"action": "unknown"})


def test_result_json_single_line_no_none_fields() -> None:
    result = ResultJson(
        status="success",
        subtype="success",
        session_id="abc123",
        turns=5,
        cost_usd=0.42,
        duration_ms=12345,
    )
    line = result.to_line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["status"] == "success"
    assert "task_id" not in parsed  # excluded when None
    assert "errors" not in parsed
    assert parsed["session_id"] == "abc123"
    assert parsed["turns"] == 5
    assert parsed["cost_usd"] == 0.42
    assert parsed["duration_ms"] == 12345


def test_result_json_terminated_with_reason() -> None:
    result = ResultJson(
        status="terminated",
        subtype="stall_detected",
        turns=7,
        cost_usd=0.0,
        duration_ms=60_000,
        termination_reason="7 consecutive turns with no tool calls",
    )
    parsed = json.loads(result.to_line())
    assert parsed["status"] == "terminated"
    assert parsed["termination_reason"].startswith("7 consecutive")
