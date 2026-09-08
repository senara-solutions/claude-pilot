"""Pilot-transcript JSONL writer tests (claude-pilot#165).

The defect this file detects was SILENT: mika#1705 shipped the producer
(``ANTHROPIC_LOG_FILE`` injected at every dispatch, directory mounted) and
mika#2040/PR#2240 shipped the ingestion, but no line was ever written — and
"no file" is indistinguishable from "no session". The anti-vacuity test below
is that detector: it fails on any build whose SDK loop writes nothing.

Helpers are imported from ``tests.test_agent`` rather than duplicated: that
module already owns the scripted-fake-client shape ``run_agent`` needs, and a
second copy would drift the moment the SDK message set changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_pilot import transcript_writer
from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails
from tests.test_agent import (
    _assistant,
    _config,
    _init,
    _install_fake_client,
    _noop_permission,
    _result,
)


@pytest.fixture(autouse=True)
def _rearm_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer disarms itself for the rest of the session after its first
    write failure (one error line, not one per SDK message). That flag is
    module state, so a failure test would silently mute every test that ran
    after it. Re-arm before each test."""
    monkeypatch.setattr(transcript_writer, "_disarmed", False)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_short_session_writes_at_least_one_valid_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC3 — the anti-vacuity detector. A short pilot session with
    ``ANTHROPIC_LOG_FILE`` set must leave the expected file behind carrying at
    least one line the mika reader would ingest (``schema_version == "v1"``).
    Red on any build with no writer at all."""
    log_file = tmp_path / "pilot-transcripts" / "task-abc.jsonl"
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", str(log_file))

    _install_fake_client(
        monkeypatch,
        [_init(), _assistant([TextBlock(text="hello")], "msg_0"), _result()],
    )

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task-abc",
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    assert exit_code == 0
    assert log_file.exists(), "no transcript file — the writer never fired"
    lines = _read_lines(log_file)
    assert len(lines) >= 1, "transcript file is empty — vacuous capture"
    assert all(line["schema_version"] == "v1" for line in lines)


def test_assistant_line_matches_the_v1_schema() -> None:
    """AC2 — the fields the mika reader (PR#2240 ``pilot_transcript.rs``) maps
    onto ``pilot_transcripts`` columns."""
    message = AssistantMessage(
        content=[TextBlock(text="hi")],
        model="claude-test",
        usage={"input_tokens": 12, "output_tokens": 34},
    )

    line = transcript_writer.transcript_line(message)

    assert line["schema_version"] == "v1"
    assert line["provider"] == "anthropic"
    assert line["model"] == "claude-test"
    assert line["tokens_in"] == 12
    assert line["tokens_out"] == 34
    assert line["latency_ms"] is None
    assert line["timestamp"].endswith("Z")
    assert line["response_body"]["content"][0]["text"] == "hi"


def test_result_line_carries_latency_and_no_model() -> None:
    """KTD4/KTD5 — ``ResultMessage`` has ``duration_api_ms`` (the API latency
    the reader stores as ``latency_ms``) and no ``model`` field at all."""
    line = transcript_writer.transcript_line(_result())

    assert line["schema_version"] == "v1"
    assert line["latency_ms"] == 50
    assert line["model"] is None


def test_no_env_var_is_a_silent_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — outside a mika dispatch the var is absent and the writer must do
    nothing at all, without raising."""
    monkeypatch.delenv("ANTHROPIC_LOG_FILE", raising=False)

    assert transcript_writer.record_sdk_message(_result()) is False
    assert list(tmp_path.iterdir()) == []


def test_blank_env_var_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty var is the same as unset — it names no path."""
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", "   ")

    assert transcript_writer.record_sdk_message(_result()) is False


def test_partial_and_inbound_messages_are_not_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spike §3 — write on the COMPLETE assistant/result messages only. A
    ``StreamEvent`` carries the same text as the ``AssistantMessage`` that
    closes the turn, so recording both would double-count every token."""
    log_file = tmp_path / "t.jsonl"
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", str(log_file))

    assert transcript_writer.record_sdk_message(UserMessage(content="tool result")) is False
    assert not log_file.exists()


def test_write_failure_is_swallowed_and_disarms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KTD2 — the transcript is observability, never the product. An
    unwritable path must not raise into the SDK loop, and must log once rather
    than once per message."""
    unwritable = tmp_path / "wall"
    unwritable.write_text("not a directory")
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", str(unwritable / "nested" / "t.jsonl"))

    assert transcript_writer.record_sdk_message(_result()) is False
    assert transcript_writer.record_sdk_message(_result()) is False

    assert capsys.readouterr().err.count("pilot-transcript") == 1


def test_unserializable_tool_input_degrades_instead_of_losing_the_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KTD3 — ``asdict`` stops at ``Any``. Without ``default=str`` a single
    exotic tool input would raise inside ``json.dumps`` and, per KTD2, lose
    the line silently."""
    log_file = tmp_path / "t.jsonl"
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", str(log_file))

    class _Exotic:
        def __repr__(self) -> str:
            return "<exotic>"

    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Bash", input={"cmd": _Exotic()})],
        model="claude-test",
    )

    assert transcript_writer.record_sdk_message(message) is True
    line = _read_lines(log_file)[0]
    assert "<exotic>" in json.dumps(line["response_body"])


async def test_write_failure_does_not_change_the_session_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KTD2, at the seam that matters — a broken transcript path must leave
    ``run_agent``'s exit code exactly where it was."""
    unwritable = tmp_path / "wall"
    unwritable.write_text("not a directory")
    monkeypatch.setenv("ANTHROPIC_LOG_FILE", str(unwritable / "t.jsonl"))

    _install_fake_client(
        monkeypatch,
        [_init(), _assistant([TextBlock(text="hello")], "msg_0"), _result()],
    )

    exit_code = await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id="task-abc",
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )

    assert exit_code == 0


def test_result_message_is_recorded_even_when_the_branch_breaks_early() -> None:
    """KTD1 — the hook sits at the TOP of the loop body, so a ``ResultMessage``
    the deny-resume path ``break``s on is still captured. Pinned here as the
    contract of ``record_sdk_message``: it accepts the message type on its own,
    with no dependency on how the branch below it exits."""
    assert transcript_writer.transcript_line(_result())["schema_version"] == "v1"
    assert isinstance(_result(), ResultMessage)
