"""Every tool call is logged — cpp#184.

Tools admitted upstream of `can_use_tool` (settings allow rules, Read/Glob/Grep)
left no line in the pilot log: pilot 67156183 made 15 calls and its log showed
one. These tests drive `run_agent` with a scripted stream that carries no
permission callback at all, which is exactly the shape of an upstream-admitted
call, and read the log.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_pilot.agent import run_agent
from claude_pilot.guardrails import SessionGuardrails

from .test_agent import _config, _init, _install_fake_client, _noop_permission, _result

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _tool_use(
    block_id: str, name: str, inp: dict[str, Any], message_id: str, parent: str | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=block_id, name=name, input=inp)],
        model="claude-test",
        message_id=message_id,
        parent_tool_use_id=parent,
    )


async def _log_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], messages: list[Any]
) -> list[str]:
    _install_fake_client(monkeypatch, [_init(), *messages, _result()])
    await run_agent(
        prompt="test",
        cwd=".",
        verbose=False,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )
    err = _ANSI.sub("", capsys.readouterr().err)
    return [line for line in err.splitlines() if "[tool:use]" in line]


@pytest.mark.asyncio
async def test_a_call_admitted_upstream_of_the_callback_is_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _log_of(
        monkeypatch,
        capsys,
        [
            _tool_use("t1", "Read", {"file_path": "/wt/README.md"}, "m1"),
            _tool_use("t2", "Grep", {"pattern": "maxTurns"}, "m2"),
            _tool_use("t3", "Bash", {"command": "du -sm target"}, "m3"),
        ],
    )

    assert len(lines) == 3
    assert lines[0].endswith("[tool:use] Read: /wt/README.md")
    assert lines[1].endswith("[tool:use] Grep: maxTurns")
    assert lines[2].endswith("[tool:use] Bash: du -sm target")


@pytest.mark.asyncio
async def test_a_multi_line_command_stays_on_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _log_of(
        monkeypatch,
        capsys,
        [_tool_use("t1", "Bash", {"command": "cd wt\ncargo test\n  make"}, "m1")],
    )

    assert len(lines) == 1
    assert lines[0].endswith("[tool:use] Bash: cd wt cargo test make")


@pytest.mark.asyncio
async def test_a_block_seen_twice_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    block = _tool_use("t1", "Read", {"file_path": "a"}, "m1")

    lines = await _log_of(monkeypatch, capsys, [block, block])

    assert len(lines) == 1


@pytest.mark.asyncio
async def test_a_subagent_call_is_logged_and_marked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _log_of(
        monkeypatch,
        capsys,
        [_tool_use("t9", "Read", {"file_path": "b"}, "s1", parent="toolu_agent")],
    )

    assert len(lines) == 1
    assert lines[0].endswith("[tool:use] (subagent) Read: b")


@pytest.mark.asyncio
async def test_a_secret_in_a_command_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _log_of(
        monkeypatch,
        capsys,
        [
            _tool_use(
                "t1",
                "Bash",
                {"command": "GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 gh pr list"},
                "m1",
            )
        ],
    )

    assert len(lines) == 1
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in lines[0]


# cpp#184, residue of defect 1: every `UserMessage` was logged as a tool result.
# Over the first 22 pilots after cpp#311, 40 such lines had no tool waiting:
# they arrived ~2 min after the previous result and the log could not say what
# they were. A message without a tool result now has its own line.

_HISTORIC = "[debug] user message (tool result) received"


async def _full_log_of(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], messages: list[Any]
) -> list[str]:
    _install_fake_client(monkeypatch, [_init(), *messages, _result()])
    await run_agent(
        prompt="test",
        cwd=".",
        verbose=True,
        task_id=None,
        permission_handler=_noop_permission,
        guardrails=SessionGuardrails(_config()),
    )
    return _ANSI.sub("", capsys.readouterr().err).splitlines()


@pytest.mark.asyncio
async def test_a_tool_result_keeps_the_historic_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = await _full_log_of(
        monkeypatch,
        capsys,
        [
            _tool_use("t1", "Read", {"file_path": "a"}, "m1"),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        ],
    )

    assert sum(line.endswith(_HISTORIC) for line in log) == 1
    assert not any("[user:message]" in line for line in log)


@pytest.mark.asyncio
async def test_a_message_without_tool_result_is_named_not_counted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = await _full_log_of(
        monkeypatch,
        capsys,
        [
            UserMessage(
                content=[TextBlock(text="continue"), TextBlock(text="now")],
                origin={"kind": "auto-continuation"},
            )
        ],
    )

    assert not any(line.endswith(_HISTORIC) for line in log)
    named = [line for line in log if "[user:message]" in line]
    assert len(named) == 1
    assert named[0].endswith(
        "[user:message] no tool result: origin=auto-continuation, content=2 text (11 chars)"
    )


@pytest.mark.asyncio
async def test_a_string_message_without_origin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = await _full_log_of(monkeypatch, capsys, [UserMessage(content="hello")])

    named = [line for line in log if "[user:message]" in line]
    assert len(named) == 1
    assert named[0].endswith("no tool result: origin=none, content=str (5 chars)")


@pytest.mark.asyncio
async def test_the_message_text_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    log = await _full_log_of(
        monkeypatch,
        capsys,
        [
            UserMessage(content=f"token {secret}", origin={"kind": "peer", "body": secret}),
            UserMessage(content=[TextBlock(text=secret)]),
        ],
    )

    assert sum("[user:message]" in line for line in log) == 2
    assert not any(secret in line for line in log)


@pytest.mark.asyncio
async def test_a_hostile_origin_kind_is_reduced_to_safe_characters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = await _full_log_of(
        monkeypatch,
        capsys,
        [UserMessage(content="x", origin={"kind": "peer\n\x1b[31m[done] Success"})],
    )

    named = [line for line in log if "[user:message]" in line]
    assert len(named) == 1
    assert "origin=peer31mdoneSuccess," in named[0]
    assert not any(line.startswith("[done] Success") for line in log)
