"""Interactive shell tests (cpp#69).

Two layers, mirroring the fake-the-SDK-seam strategy of test_agent.py /
test_ipython_magics.py:

* The REPL loop (``InteractiveShell`` / ``run_shell``) is driven against a fake
  ``ClaudeSDKClient`` and a fake stdin — covering turn streaming, one-session
  continuity, the opening-prompt seed, and the ``:`` meta-commands.
* The permission relay *in an interactive context* is exercised against the
  real ``create_permission_handler(interactive=True)`` with a stubbed TTY stdin:
  a policy DEFAULT deny is surfaced to the operator and their y/n flows back,
  while explicit rule denies and the non-TTY posture stay hard halts. The
  permission chain internals themselves remain owned by test_permissions.py /
  test_tier1.py — this only pins the interactive wiring cpp#69 adds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from claude_pilot import shell as shell_module
from claude_pilot.permissions import create_permission_handler
from claude_pilot.shell import InteractiveShell, run_shell


class _FakeSDKClient:
    """Stand-in for ClaudeSDKClient as an async context manager: records
    lifecycle calls, echoes the last prompt back as one assistant turn."""

    instances: ClassVar[list[_FakeSDKClient]] = []

    def __init__(self, options: Any = None) -> None:
        self.options = options
        self.prompts: list[str] = []
        self.enter_calls = 0
        self.exit_calls = 0
        self.interrupt_calls = 0
        type(self).instances.append(self)

    async def __aenter__(self) -> _FakeSDKClient:
        self.enter_calls += 1
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.exit_calls += 1
        return False

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    def receive_response(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield AssistantMessage(
                content=[TextBlock(text=f"echo:{self.prompts[-1]}")],
                model="claude-test",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id="sess_test",
                total_cost_usd=0.0012,
            )

        return gen()


class _FakeStdin:
    """Minimal stdin: pops queued lines, returns '' (EOF) when drained."""

    def __init__(self, lines: list[str], *, tty: bool = True) -> None:
        self._lines = list(lines)
        self._tty = tty
        self.readline_calls = 0

    def readline(self) -> str:
        self.readline_calls += 1
        return self._lines.pop(0) if self._lines else ""

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture()
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSDKClient]:
    monkeypatch.setattr(shell_module, "ClaudeSDKClient", _FakeSDKClient)
    _FakeSDKClient.instances = []
    return _FakeSDKClient


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
    *,
    opening_prompt: str | None = None,
) -> int:
    """Run the REPL to completion with ``lines`` fed on stdin."""
    monkeypatch.setattr("sys.stdin", _FakeStdin(lines))
    return asyncio.run(
        run_shell(
            cwd="/tmp",
            verbose=False,
            config=None,
            relay=False,
            opening_prompt=opening_prompt,
        )
    )


# ── REPL loop ─────────────────────────────────────────────────────────────


def test_turn_streams_assistant_text_and_footer(
    fake_client: type[_FakeSDKClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _drive(monkeypatch, ["what is 2+2?\n"])
    assert code == 0
    (client,) = fake_client.instances
    assert client.prompts == ["what is 2+2?"]
    captured = capsys.readouterr()
    assert "echo:what is 2+2?" in captured.out  # model text on stdout
    assert "success" in captured.err  # footer on stderr, stdout stays clean


def test_single_persistent_session_across_turns(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive(monkeypatch, ["first\n", "second\n"])
    # ONE client for the whole REPL — the multi-turn continuity contract.
    (client,) = fake_client.instances
    assert client.prompts == ["first", "second"]
    assert client.enter_calls == 1
    assert client.exit_calls == 1


def test_opening_prompt_seeds_first_turn(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive(monkeypatch, [], opening_prompt="kickoff prompt")
    (client,) = fake_client.instances
    assert client.prompts == ["kickoff prompt"]


def test_eof_exits_cleanly_without_a_turn(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _drive(monkeypatch, [])  # immediate EOF
    assert code == 0
    (client,) = fake_client.instances
    assert client.prompts == []


def test_exit_command_stops_the_repl(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive(monkeypatch, [":exit\n", "never reached\n"])
    (client,) = fake_client.instances
    assert client.prompts == []  # nothing sent to Claude


def test_help_command_does_not_query(
    fake_client: type[_FakeSDKClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drive(monkeypatch, [":help\n"])
    (client,) = fake_client.instances
    assert client.prompts == []
    assert "commands" in capsys.readouterr().err


def test_session_command_reports_id_after_a_turn(
    fake_client: type[_FakeSDKClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drive(monkeypatch, ["hello\n", ":session\n"])
    err = capsys.readouterr().err
    assert "sess_test" in err


def test_unknown_meta_command_is_reported_not_sent(
    fake_client: type[_FakeSDKClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drive(monkeypatch, [":bogus\n"])
    (client,) = fake_client.instances
    assert client.prompts == []
    assert "unknown command" in capsys.readouterr().err


def test_slash_prefixed_input_is_sent_to_claude(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A leading '/' is a Claude slash-command, NOT a REPL meta-command.
    _drive(monkeypatch, ["/mika do the thing\n"])
    (client,) = fake_client.instances
    assert client.prompts == ["/mika do the thing"]


def test_blank_lines_are_skipped(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _drive(monkeypatch, ["\n", "   \n", "real\n"])
    (client,) = fake_client.instances
    assert client.prompts == ["real"]


# ── permission relay is WIRED, not re-implemented ─────────────────────────


def test_shell_options_carry_permission_handler(
    fake_client: type[_FakeSDKClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK client is constructed with a can_use_tool built by
    create_permission_handler — same chain as the headless pilot."""
    _drive(monkeypatch, ["hi\n"])
    (client,) = fake_client.instances
    assert client.options is not None
    assert client.options.can_use_tool is not None
    assert client.options.permission_mode == "default"
    assert "ScheduleWakeup" in client.options.disallowed_tools


def test_shell_module_has_no_permission_logic() -> None:
    """Guard against re-implementation drift: the shell must import its
    permission handling from claude_pilot.permissions."""
    import inspect

    source = inspect.getsource(shell_module)
    assert "create_permission_handler" in source
    assert not hasattr(shell_module, "is_tier1_auto_approve")


# ── permission relay IN an interactive context (cpp#69 core) ──────────────


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None, suggestions=[], tool_use_id="tool_test", agent_id=None
    )


def _interactive_handler(policy_path: Path) -> Any:
    return create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        policy_path=policy_path,
        interactive=True,
    )


def test_default_deny_surfaces_to_operator_who_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy DEFAULT deny (no rule matched) in interactive+TTY posture is
    surfaced to the operator; their 'y' flows back as Allow."""
    stdin = _FakeStdin(["y\n"], tty=True)
    monkeypatch.setattr("sys.stdin", stdin)
    handler = _interactive_handler(Path("/nonexistent/policy.yaml"))
    result = asyncio.run(handler("Bash", {"command": "curl https://example.com"}, _ctx()))
    assert isinstance(result, PermissionResultAllow)
    assert stdin.readline_calls == 1  # the operator WAS asked


def test_default_deny_surfaces_to_operator_who_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = _FakeStdin(["n\n"], tty=True)
    monkeypatch.setattr("sys.stdin", stdin)
    handler = _interactive_handler(Path("/nonexistent/policy.yaml"))
    result = asyncio.run(handler("Bash", {"command": "curl https://example.com"}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    # Operator deny does not hard-abort the session (interrupt=False) — the
    # REPL keeps driving. Contrast the explicit rule-deny below.
    assert result.interrupt is False
    assert result.message == "Denied by user"


def test_explicit_rule_deny_stays_hard_even_in_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EXPLICIT rule-based deny is never handed to the operator — it halts
    the session (interrupt=True) exactly as headlessly. The operator drives
    only the unknown space, never overrides an explicit 'no'."""
    policy_file = tmp_path / "rule_deny.yaml"
    policy_file.write_text(
        "rules:\n"
        "  - id: deny-curl\n"
        "    tool: Bash\n"
        "    pattern: '^curl\\s'\n"
        "    decision: deny\n"
        "    reason: rule-based test deny\n"
        "default:\n"
        "  decision: allow\n"
        "  reason: default allow (test fixture)\n"
    )
    stdin = _FakeStdin(["y\n"], tty=True)  # operator would say yes...
    monkeypatch.setattr("sys.stdin", stdin)
    handler = _interactive_handler(policy_file)
    result = asyncio.run(handler("Bash", {"command": "curl https://evil.test"}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True  # ...but the explicit deny still halts
    assert result.message == "rule-based test deny"
    assert stdin.readline_calls == 0  # operator was NEVER consulted


def test_non_tty_interactive_keeps_fail_closed_default_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interactive=True but a non-TTY stdin (piped / CI) keeps the headless
    fail-closed default-deny with interrupt=True — the shell never silently
    widens privilege off a TTY."""
    stdin = _FakeStdin([], tty=False)
    monkeypatch.setattr("sys.stdin", stdin)
    handler = _interactive_handler(Path("/nonexistent/policy.yaml"))
    result = asyncio.run(handler("Bash", {"command": "curl https://example.com"}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    assert stdin.readline_calls == 0


def test_interrupt_during_turn_returns_to_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A KeyboardInterrupt mid-turn interrupts just that turn (best-effort
    client.interrupt()) and the REPL keeps going — it does not crash."""

    class _InterruptingClient(_FakeSDKClient):
        def receive_response(self) -> AsyncIterator[Any]:
            async def gen() -> AsyncIterator[Any]:
                raise KeyboardInterrupt
                yield  # pragma: no cover — unreachable, makes this an async gen

            return gen()

    client = _InterruptingClient()
    shell = InteractiveShell(client)
    asyncio.run(shell._turn("go"))
    assert client.interrupt_calls == 1
    assert "interrupted" in capsys.readouterr().err
