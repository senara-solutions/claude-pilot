"""Interactive REPL for live-driving a Claude session (cpp#69).

``claude-pilot --interactive`` (alias ``-i``) opens an operator-facing shell
that live-drives ONE persistent ``ClaudeSDKClient``: the operator types a
prompt, the assistant's streamed text renders to the terminal, and cpp's
permission machinery runs mid-conversation exactly as it does headlessly.

Design — *reuse, do not rebuild*:

* The ``can_use_tool`` callback is the same object the headless pilot uses,
  built by :func:`claude_pilot.permissions.create_permission_handler`. The
  shell passes ``interactive=True`` so a policy **default** deny (an unknown,
  no-rule-matched request) surfaces to the live operator through the existing
  interactive fallback — a ``y/n`` (or numbered-option) prompt whose answer
  flows straight back to the SDK as the tool decision. Every explicit safety
  decision (Tier 1 auto-approve, rule-based deny, deny-with-notify, the
  chain-danger / destination vetoes) is untouched: the operator drives the
  *unknown* space, never overrides an explicit "no". Tool requests and
  decisions render to stderr through the reused handler's own logger — that IS
  the shell's tool-call annotation stream, not a second one.
* One client spans the whole REPL, so the SDK session id persists across turns
  and each prompt continues the same conversation (multi-turn continuity).

The model's text goes to **stdout**; every frame the shell/handler adds
(banner, prompt, tool annotations, result footer, notices) goes to **stderr**,
keeping stdout a clean transcript of the assistant. Unlike headless mode the
shell writes no ``ResultJson`` line — that machine contract is headless-only.

Guardrails (stall / empty / idle) are deliberately absent: in a REPL the
operator IS the loop, so there is no autonomous drift to police. Meta-commands
use a ``:`` prefix (``:help``, ``:exit``, ``:session``) so a leading ``/`` stays
free to pass through to Claude as a slash-command (``/mika`` …).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, SystemMessage

from .agent import (
    _content_blocks,
    _extract_model,
    _extract_session_id,
    _system_prompt_with_hint,
    _text_of,
)
from .permissions import create_permission_handler
from .types import PilotConfig
from .ui import BOLD, CYAN, DIM, GREEN, RESET, YELLOW

_EXIT_COMMANDS = frozenset({":exit", ":quit", ":q"})
_HELP_COMMANDS = frozenset({":help", ":h", ":?"})
_SESSION_COMMANDS = frozenset({":session", ":info"})

_PROMPT = f"{BOLD}{CYAN}claude>{RESET} "


def build_shell_options(
    *,
    cwd: str,
    config: PilotConfig | None,
    relay: bool,
    verbose: bool,
    task_id: str | None,
) -> ClaudeAgentOptions:
    """Assemble the SDK options for the interactive session.

    Mirrors :func:`claude_pilot.agent._run_agent_inner`'s options — same
    ``permission_mode``, the preset+append system prompt (a plain string would
    REPLACE the Claude Code preset, mika#1409), and the ``ScheduleWakeup``
    exclusion (cpp#59) — but binds an ``interactive=True`` permission handler
    and omits the headless-only guardrail kwargs.
    """
    handler = create_permission_handler(
        config=config,
        relay=relay,
        verbose=verbose,
        cwd=cwd,
        guardrails=None,
        task_id=task_id,
        interactive=True,
    )
    return ClaudeAgentOptions(
        permission_mode="default",
        cwd=cwd,
        setting_sources=["user", "project", "local"],
        can_use_tool=handler,
        system_prompt=_system_prompt_with_hint(),
        disallowed_tools=["ScheduleWakeup"],
    )


class InteractiveShell:
    """The REPL loop bound to one persistent SDK client.

    Constructed with a live, connected ``ClaudeSDKClient``; owns neither its
    connect nor its disconnect (see :func:`run_shell`). Tracks the session id
    and turn count for the ``:session`` command and the per-turn footer.
    """

    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client
        self._session_id: str | None = None
        self._model: str | None = None
        self._turns = 0

    async def run(self, opening_prompt: str | None = None) -> int:
        """Drive the REPL until the operator exits. Returns a process exit code.

        ``opening_prompt`` (from a prompt supplied on the command line) is sent
        as the first turn before the shell begins reading operator input.
        """
        _emit(f"{DIM}claude-pilot interactive shell — :help for commands, "
              f":exit to quit{RESET}")
        pending: str | None = opening_prompt.strip() if opening_prompt else None
        while True:
            raw: str | None
            if pending is not None:
                raw = pending
                pending = None
                _emit(f"{_PROMPT}{raw}")  # echo the seeded turn
            else:
                raw = await self._read_line()
                if raw is None:  # EOF (Ctrl-D)
                    _emit("")
                    break
            line = raw.strip()
            if not line:
                continue
            if line in _EXIT_COMMANDS:
                break
            if line in _HELP_COMMANDS:
                _print_help()
                continue
            if line in _SESSION_COMMANDS:
                self._print_session()
                continue
            if line.startswith(":"):
                _emit(f"{YELLOW}unknown command {line!r} — :help for the list{RESET}")
                continue
            await self._turn(line)
        _emit(f"{DIM}session ended{RESET}")
        return 0

    async def _turn(self, prompt: str) -> None:
        """Run one prompt→response exchange, streaming the assistant to stdout.

        A ``Ctrl-C`` mid-turn interrupts just this turn (best-effort
        ``client.interrupt()``) and returns to the prompt; it never exits the
        shell. Any other SDK error is reported and the loop continues so a
        single bad turn does not kill the session.
        """
        try:
            await self._client.query(prompt)
            async for message in self._client.receive_response():
                self._render(message)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _emit(f"\n{YELLOW}⏹ interrupted — returning to prompt{RESET}")
            try:
                await self._client.interrupt()
            except Exception:
                pass
        except Exception as err:  # surface, don't crash the REPL
            _emit(f"{YELLOW}[error] {type(err).__name__}: {err}{RESET}")

    def _render(self, message: Any) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            self._session_id = _extract_session_id(message) or self._session_id
            self._model = _extract_model(message) or self._model
            return
        if isinstance(message, AssistantMessage):
            self._session_id = getattr(message, "session_id", None) or self._session_id
            for block in _content_blocks(message):
                text = _text_of(block)
                if text:
                    sys.stdout.write(text + "\n")
                    sys.stdout.flush()
            return
        if isinstance(message, ResultMessage):
            self._turns = message.num_turns
            self._session_id = self._session_id or getattr(message, "session_id", None)
            _write_footer(message)

    async def _read_line(self) -> str | None:
        """Prompt on stderr and read one line from stdin off the event loop.

        Returns the raw line (without resolving the trailing newline), or
        ``None`` at EOF. The blocking ``readline`` is dispatched to the default
        thread executor so the loop stays responsive, matching
        ``permissions._ainput``'s bridge.
        """
        sys.stderr.write(_PROMPT)
        sys.stderr.flush()
        loop = asyncio.get_running_loop()
        line: str = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":  # EOF — readline returns "" (a bare newline is "\n")
            return None
        return line

    def _print_session(self) -> None:
        sid = self._session_id[:12] if self._session_id else "(not started)"
        model = self._model or "(unknown)"
        _emit(
            f"{DIM}session {sid} · model {model} · {self._turns} turn(s) "
            f"this exchange{RESET}"
        )


async def run_shell(
    *,
    cwd: str,
    verbose: bool,
    config: PilotConfig | None,
    relay: bool,
    task_id: str | None = None,
    opening_prompt: str | None = None,
) -> int:
    """Open a connected SDK client and drive the interactive REPL over it.

    Owns the client lifecycle (``async with`` connect/disconnect); the
    :class:`InteractiveShell` only borrows it. Returns the intended process
    exit code (``0`` on a clean exit).
    """
    options = build_shell_options(
        cwd=cwd,
        config=config,
        relay=relay,
        verbose=verbose,
        task_id=task_id,
    )
    async with ClaudeSDKClient(options=options) as client:
        shell = InteractiveShell(client)
        exit_code = await shell.run(opening_prompt=opening_prompt)
    return exit_code


def _emit(msg: str) -> None:
    """Write a shell frame to stderr (keeps stdout a pure assistant transcript)."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _write_footer(result: ResultMessage) -> None:
    """Compact end-of-turn footer on stderr."""
    color = GREEN if result.subtype == "success" else YELLOW
    cost = f" · ${result.total_cost_usd:.4f}" if result.total_cost_usd else ""
    _emit(
        f"{DIM}[{color}{result.subtype}{RESET}{DIM}] "
        f"{result.num_turns} turn(s){cost}{RESET}"
    )


def _print_help() -> None:
    _emit(
        f"{BOLD}commands{RESET}\n"
        f"  {CYAN}:help{RESET}  :h  :?     show this help\n"
        f"  {CYAN}:session{RESET}  :info   show session id, model, turn count\n"
        f"  {CYAN}:exit{RESET}  :quit  :q   leave the shell (or Ctrl-D)\n"
        f"{DIM}anything else is sent to Claude as a prompt; a leading '/' is a "
        f"Claude slash-command (e.g. /mika). Ctrl-C interrupts the current "
        f"turn.{RESET}"
    )
