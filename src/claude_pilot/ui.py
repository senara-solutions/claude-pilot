"""Stderr log renderer with ANSI colors. Port of src/ui.ts."""

from __future__ import annotations

from .logger import write_file_log, write_log
from .types import ResolvedGuardrailConfig

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"
ORANGE = "\x1b[38;5;208m"


def _log(msg: str) -> None:
    write_log(msg + "\n")


def log_init(session_id: str, model: str, task_id: str | None = None) -> None:
    task_str = f", task {task_id}" if task_id else ""
    _log(f"{DIM}[init]{RESET} Session {session_id[:8]}, model {model}{task_str}")


def log_reconnect(session_id: str, model: str) -> None:
    _log(f"{DIM}[reconnect]{RESET} Session {session_id[:8]}, model {model}")


def log_tool(tool_name: str, detail: str, decision: str | None = None) -> None:
    if decision:
        color = GREEN if decision == "ALLOW" else RED if decision == "DENY" else YELLOW
        decision_str = f" → {color}{decision}{RESET}"
    else:
        decision_str = ""
    _log(f"{DIM}[tool]{RESET} {BOLD}{tool_name}{RESET}: {detail}{decision_str}")


def log_question(question: str, answer: str | None = None) -> None:
    answer_str = f' → {GREEN}"{answer}"{RESET}' if answer else ""
    _log(f'{MAGENTA}[question]{RESET} "{question}"{answer_str}')


def log_text(text: str) -> None:
    write_log(f"{DIM}{text}{RESET}")


def log_done(turns: int, cost_usd: float | None, duration_ms: int) -> None:
    secs = f"{duration_ms / 1000:.0f}"
    cost_str = f"${cost_usd:.2f}" if cost_usd is not None else "$?"
    _log(f"\n{GREEN}[done]{RESET} Success | {turns} turns | {cost_str} | {secs}s")


def log_error(subtype: str, errors: list[str]) -> None:
    _log(f"\n{RED}[error]{RESET} {subtype}: {', '.join(errors)}")


def log_denied(tool_name: str, detail: str) -> None:
    _log(f"{RED}[denied]{RESET} {tool_name}: {detail}")


def log_retry(reason: str) -> None:
    _log(f"{YELLOW}[retry]{RESET} {reason}")


def log_fallback(reason: str) -> None:
    _log(f"{YELLOW}[fallback]{RESET} {reason} — answering from claude-pilot")


def log_config(cwd: str, config_path: str, found: bool, relay: bool) -> None:
    status = "found" if found else "NOT FOUND"
    relay_str = "enabled" if relay else "disabled"
    _log(f"{DIM}[config]{RESET} cwd={cwd} config={config_path} [{status}] relay={relay_str}")


def log_tool_request(tool_name: str, detail: str) -> None:
    _log(f"{DIM}[tool:request]{RESET} {BOLD}{tool_name}{RESET}: {detail}")


def log_relay_send(tool_name: str) -> None:
    _log(f"{DIM}[relay:send]{RESET} {tool_name} → agent")


def log_relay_recv(tool_name: str, action: str, latency_ms: int) -> None:
    color = GREEN if action == "allow" else RED if action == "deny" else YELLOW
    _log(f"{DIM}[relay:recv]{RESET} {tool_name} ← {color}{action}{RESET} ({latency_ms}ms)")


def log_verbose(msg: str) -> None:
    _log(f"{DIM}[debug] {msg}{RESET}")


def log_escalate(tool_name: str, detail: str) -> None:
    _log(f"{CYAN}[ESCALATE]{RESET} Claude wants to use: {BOLD}{tool_name}{RESET}")
    _log(f"  {detail}")


def log_question_escalate(question: str) -> None:
    _log(f"{CYAN}[QUESTION]{RESET} {question}")


def log_prompt(prompt: str) -> None:
    write_file_log(f"[prompt] {prompt}\n")


def log_guardrail(type_: str, detail: str) -> None:
    _log(f"\n{ORANGE}[guardrail]{RESET} {BOLD}{type_}{RESET}: {detail}")


def log_deny_resume(attempt: int, budget: int, subtype: str) -> None:
    """cpp#151 B2 — a session that ended on `error_during_execution` after a
    NON-TERMINAL refusal is being handed a fresh turn instead of being buried.

    Never silent (plan phase 11): a resume that leaves no line would make the
    turn count, the cost and the duration of a session unexplainable from the
    log alone. `attempt`/`budget` are rendered together so an operator reading
    the tail can see how much of the recovery budget is left.
    """
    _log(
        f"\n{ORANGE}[resume]{RESET} {BOLD}{subtype}{RESET} followed a "
        f"non-terminal policy denial — continuing the session "
        f"(attempt {attempt}/{budget})"
    )


def log_deny_resume_failed(detail: str) -> None:
    """cpp#151 B2 — the resume nudge could not be delivered.

    The session then falls through to the ordinary terminal emit, so the
    failure is visible next to the result line rather than swallowed.
    """
    _log(f"{RED}[resume:failed]{RESET} {detail}")


def log_policy_allow(tool_name: str, detail: str, rule_id: str | None) -> None:
    tag = f" [{rule_id}]" if rule_id else ""
    _log(f"{GREEN}[policy:allow]{RESET} {BOLD}{tool_name}{RESET}: {detail}{tag}")


#: cpp#151 B0 — the lethality suffix rendered on every ``[policy:deny]`` line.
#: These two literals are the load-bearing artifact of B0: they are what makes
#: the AC5 population ("sessions that took a NON-TERMINAL refusal") readable
#: from `/var/log/claude-pilot/*.stderr` after the fact, instead of being a
#: retrospective judgement about which of two superposed classes a dead session
#: belonged to. Kept as module constants so the measurement command and the
#: renderer cannot drift apart.
TERMINAL_SUFFIX = " (terminal)"
NON_TERMINAL_SUFFIX = " (non-terminal)"


def _lethality_suffix(terminal: bool) -> str:
    return TERMINAL_SUFFIX if terminal else NON_TERMINAL_SUFFIX


def log_policy_deny(
    tool_name: str, detail: str, rule_id: str | None, *, terminal: bool
) -> None:
    """Render a policy refusal, naming whether it also ends the run (cpp#151).

    ``terminal`` is REQUIRED and keyword-only on purpose. cpp#128 split the
    decision (refuse) from the lethality (``interrupt=True``) but left the
    second half unlogged, so the eight dead sessions in the cpp#151 body could
    not be separated into "claude-pilot asked for this kill" (destination veto,
    tier3-dangerous Bash — correct behaviour) and "died despite
    ``interrupt=False``" (the actual residue). A default value here would let a
    future call site silently rejoin the two populations.
    """
    tag = f" [{rule_id}]" if rule_id else ""
    _log(
        f"{RED}[policy:deny]{RESET} {BOLD}{tool_name}{RESET}: "
        f"{detail}{tag}{_lethality_suffix(terminal)}"
    )


def log_policy_deny_with_notify(tool_name: str, detail: str, rule_id: str | None) -> None:
    """Log a policy decision of ``escalate`` (wire-format) = deny-with-notify.

    Renamed from ``log_policy_escalate`` in cpp#20/#21: the runtime semantics
    post-joint-2 are "halt the pilot loop + best-effort operator notify" --
    not a relay-to-operator-for-decision escalation. The wire-format keyword
    stays ``escalate`` for back-compat; the source symbol reads under the
    correct name.
    """
    tag = f" [{rule_id}]" if rule_id else ""
    # cpp#151 B0: deny-with-notify is unconditionally terminal (cpp#128 left it
    # so deliberately — an escalate exists to put a human in the loop). The
    # suffix is rendered anyway so every refusal line in the log states its
    # lethality, and so the AC5 grep for `(non-terminal)` cannot match here.
    _log(
        f"{YELLOW}[policy:deny_with_notify]{RESET} {BOLD}{tool_name}{RESET}: "
        f"{detail}{tag}{TERMINAL_SUFFIX}"
    )


def log_turn_summary(turn: int, summary: str) -> None:
    """Per-turn marker for diagnostically silent turns (cpp#10).

    Emitted when a logical turn produced no text and no tool calls — so the
    operator can still see that the turn happened. Open-string `summary` lets
    callers describe the silence ("thinking-only, no actions" /
    "no observable output").
    """
    _log(f"{DIM}[turn {turn}]{RESET} {summary}")


def log_env(env_path: str, loaded: bool, count: int) -> None:
    if loaded:
        _log(f"{DIM}[env]{RESET} path={env_path} [LOADED] vars={count}")
    else:
        _log(f"{DIM}[env]{RESET} path={env_path} [NOT FOUND]")


def _ceiling_label(ms: int) -> str:
    """cpp#145: render a wait ceiling, naming the unbounded case out loud.

    `0` is a documented mode inherited from `rateLimitCeilingMs` — defer
    indefinitely. It is also the only setting under which a waiting session
    can never be terminated, so it is spelled out rather than rendered as a
    plausible-looking `0.0s`.
    """
    return "off(unbounded)" if ms <= 0 else f"{ms / 1000}s"


def log_guardrail_config(config: ResolvedGuardrailConfig) -> None:
    parts: list[str] = [f"maxTurns={config.maxTurns}"]
    if config.stallThreshold > 0:
        parts.append(f"stallThreshold={config.stallThreshold}")
    if config.emptyResponseThreshold > 0:
        parts.append(f"emptyResponseThreshold={config.emptyResponseThreshold}")
    if config.idleTimeoutMs > 0:
        parts.append(f"idleTimeout={config.idleTimeoutMs / 1000}s")
        # cpp#133: the ceiling only matters while idle detection is armed.
        if config.rateLimitCeilingMs > 0:
            parts.append(f"rateLimitCeiling={config.rateLimitCeilingMs / 1000}s")
        # cpp#145: the two wait ceilings only bound states the idle watchdog
        # reaches, hence the same enclosing condition. But unlike the line
        # above they are ALWAYS printed, including at 0 — because 0 means
        # "wait indefinitely", and that is the one configuration in which the
        # watchdog cannot terminate a waiting session at all. A guardrail that
        # cannot kill must not be the quietest line in the header; omitting it
        # would hide exactly the state an operator most needs to see.
        parts.append(f"toolWaitCeiling={_ceiling_label(config.toolWaitCeilingMs)}")
        parts.append(f"modelWaitCeiling={_ceiling_label(config.modelWaitCeilingMs)}")
    if config.maxBudgetUsd > 0:
        parts.append(f"maxBudget=${config.maxBudgetUsd}")
    _log(f"{DIM}[guardrails]{RESET} {' '.join(parts)}")


def log_unhandled_message(type_name: str) -> None:
    """cpp#123: name an SDK message type the agent loop does not handle.

    Emitted once per type per session. The bug this closes existed because
    `StreamEvent` fell off the end of the message loop without a trace, and a
    failure path that logs nothing is indistinguishable from a path that was
    never taken.
    """
    _log(f"{DIM}[unhandled]{RESET} SDK message type not handled: {type_name}")
