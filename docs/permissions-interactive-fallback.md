# Interactive fallback in `permissions.py`

**Status:** documentation of existing behavior (cpp#70). Prerequisite readout for cpp#69 (extending interactive mode). No behavior change; no refactor.

**Scope:** what the interactive fallback in `src/claude_pilot/permissions.py` does today, as of `1cb9f39`.

---

## TL;DR

- The fallback exists (`_interactive_fallback`, `_interactive_permission`, `_interactive_question`, `_ainput` at `permissions.py:1065-1140`) and works — but in the default deployment posture, **it is unreachable**.
- With the default `MIKA_PILOT_POLICY_DISABLED` unset, `policy.py` Tier 2 evaluates every request and always returns a terminal `allow` / `deny` / `deny-with-notify` decision (`policy.py:240-244`, default-deny). Both interactive fallback branches (`permissions.py:897-906` and `permissions.py:980-987`) sit *below* the Tier 2 return path. The relay path itself is only reachable via emergency rollback (`permissions.py:894-895`).
- The fallback is therefore effectively **dead code on the live pilot loop**. It is a legacy path preserved for `--no-relay` invocations and for the emergency-rollback (`MIKA_PILOT_POLICY_DISABLED=1`) posture. cpp#69 planning should treat the branch as legacy machinery to fork or replace, not extend.
- **Zero test coverage.** No test in `tests/` exercises `_interactive_fallback`, `_interactive_permission`, `_interactive_question`, or `_ainput` (grep confirms).

---

## Where it lives

`src/claude_pilot/permissions.py`:

| Symbol | Lines | Role |
|---|---|---|
| `_interactive_fallback` | 1065-1077 | Entry point. TTY-gates then dispatches by tool name. |
| `_interactive_permission` | 1080-1091 | Plain permission prompt (`Allow? (y/n)`). Non-`AskUserQuestion` tools. |
| `_interactive_question` | 1094-1131 | `AskUserQuestion` prompt with numbered options + free-text fallback. |
| `_ainput` | 1134-1140 | Non-blocking stdin readline via `run_in_executor` (bridges sync stdin to asyncio). |

Call sites (both in `create_permission_handler`):

- `permissions.py:897-906` — **"No relay → interactive fallback"** branch. Fires when `not relay or config is None`, i.e. `--no-relay` was passed or no `.claude/claude-pilot.json` was found.
- `permissions.py:976-987` — **relay-exhausted retry** branch. Fires when the relay subprocess raised `TransportError` twice (initial + one retry with error feedback per contract in `permissions.py:945-953`).

---

## Trigger conditions

### What actually triggers it (once policy has been bypassed)

The `_interactive_fallback` function itself is called on exactly two conditions:

1. **Relay disabled** (`permissions.py:898`): `not relay or config is None`. `relay` is set false either explicitly (`--no-relay`, `cli.py:44`) or as a fallback when no config file was found (`cli.py:207-217`).
2. **Relay agent produced a malformed response twice in a row** (`permissions.py:976-980`): the initial `invoke_command` raised `TransportError`, the retry-with-error-feedback (`permissions.py:945-953`) also raised `TransportError`. `TransportError` is raised by `transport.py` on: subprocess spawn failure, non-zero exit, non-JSON stdout, or Pydantic validation failure against `PilotResponse`.

Note both are `TransportError`-driven, not timeout- or "unknown response"-driven — the relay contract does not carry an explicit "I don't know" action (`PilotResponse` is `allow | deny | answer`, `types.py`).

### What actually reaches it (the load-bearing gate)

**The dominant gate is Tier 2 policy evaluation, not the fallback logic itself.**

`create_permission_handler` runs in this order (`permissions.py:748-990`):

1. Per-spawn Bash evaluator, if `MIKA_PERMISSION_POLICY_MODE=per_spawn` (`permissions.py:761-790`) — allow-only shortcut; deny falls through.
2. Tier 1 auto-approve (`permissions.py:793-801`) — returns on hit.
3. Tier 1.5 auto-answer for compact-safe compaction (`permissions.py:804-813`) — returns on hit.
4. **Tier 2 policy** (`permissions.py:822-892`) — enabled by default; disabled by `MIKA_PILOT_POLICY_DISABLED=1` (`permissions.py:732`). Because `policy.evaluate` returns the policy default when no rule matches (`policy.py:240-244`) and that default is `"deny"` (`policy.py:64`), this block **always returns terminally** when enabled.
5. Relay block, and its interactive-fallback tail — only reached when `policy_enabled` is `False`.

The TODO at `permissions.py:894-895` states this outright:

> The relay path is only reachable when `MIKA_PILOT_POLICY_DISABLED=1` (emergency rollback).

So in the live deployment posture, neither the "no relay → interactive" branch nor the "relay-exhausted → interactive" branch runs — the request has already terminated in Tier 2. The only current path to the interactive fallback is:

- `MIKA_PILOT_POLICY_DISABLED=1` **and** (`--no-relay` OR missing config OR relay double-fault).

### Guardrail interaction (stall / idle-timeout)

- The **idle timer** is paused for the entire relay attempt (`permissions.py:924-925` and `permissions.py:988-990`, via `guardrails.pause_idle_timer()` / `resume_idle_timer()`). Because the interactive fallback runs *inside* the `try:` (the second-retry branch, `permissions.py:980`) or *before* the guardrail-pause branch (the "no relay" branch, `permissions.py:899`, which runs before line 924), the interactive prompt's block on stdin **does not count against `--idle-timeout`** in either path.
- The **stall counter** counts consecutive turns with no tool calls (`guardrails.py:229-238`). The interactive fallback resolves a *tool call* (returning `Allow` or `Deny`), so completing it does not increment the stall counter. However, if the operator hangs at the prompt indefinitely, no turn advances — stall detection is turn-scoped, not time-scoped, so it silently sits.
- No dedicated timeout on `_ainput` itself. `sys.stdin.readline` blocks until EOL or EOF.

### Rate-limiting / repeat firing

None. The fallback can fire on every tool call in an interactive session. There is no cool-down, no per-session counter, no back-off.

---

## UI / prompt shape

### Permission fallback (`_interactive_permission`, `permissions.py:1080-1091`)

Rendered to **stderr** (via `log_escalate` in `ui.py:95-97`):

```
[ESCALATE] Claude wants to use: <tool_name>
  <summary>
  Allow? (y/n):
```

- `[ESCALATE]` is cyan (ANSI `\x1b[36m`), `<tool_name>` is bold.
- `<summary>` is `_summarize_input(tool_name, tool_input)` (`permissions.py:1161-1173`) — a truncated, secret-scrubbed rendering (200 chars for Bash `command`, file path for Read/Write/Edit, pattern for Glob/Grep, JSON dump truncated to 150 chars for anything else).
- Input options: **`y` (or any string starting with `y`, case-insensitive) → Allow; anything else → Deny**. No allow-with-modifications, deny-with-reason, or defer-to-relay-with-hint. Denial message is the string literal `"Denied by user"`.

### AskUserQuestion fallback (`_interactive_question`, `permissions.py:1094-1131`)

Rendered to stderr:

```
[QUESTION] <question text>
  1. <option label>
  2. <option label>
  ...

  Your answer:
```

- `[QUESTION]` is cyan (via `log_question_escalate`, `ui.py:100-101`).
- Options list is only shown when `q.get("options")` is a `list` (`permissions.py:1109-1112`); otherwise the question is free-text only.
- Answer parsing: integer input in range `[1, len(options)]` selects the labeled option; anything else (including non-integer, out-of-range, empty) is taken as a **verbatim free-text answer** (`permissions.py:1114-1122`). Free-text mode always accepts (`_interactive_question` never denies — the only deny in this path is the malformed-input guard at `permissions.py:1097-1100`).
- Each question in the array is prompted in sequence; answers are keyed by the question text (`permissions.py:1103-1124`).

### Formatting caveats

- ANSI colors are always emitted to stderr regardless of TTY detection on stderr. Only `sys.stdin.isatty()` is checked (`permissions.py:1069`). A non-TTY stderr receiving ANSI codes is harmless but visible in log captures unless the file sink strips them (which it does — see below).
- No rich formatting: no boxes, no wrapping, no highlighting of dangerous arguments.

---

## Transport channel

- **Prompt output:** `sys.stderr` (via `_ainput`, `permissions.py:1137-1138`, and via the ANSI log helpers).
- **User input:** `sys.stdin`, blocking readline dispatched to the default thread executor (`permissions.py:1139-1140`). This is how the sync stdin read reconciles with the asyncio event loop: `loop.run_in_executor(None, sys.stdin.readline)` moves the blocking call to a worker thread so the loop remains responsive to other tasks.
- **No separate FD, IPC socket, or terminal reservation.** The prompt shares the same stderr the rest of `ui.py` uses for `[tool]`, `[relay:send]`, `[relay:recv]`, `[policy:allow]`, `[policy:deny]`, `[done]`, etc. If claude-pilot is chatty, the prompt line can be interleaved with other log lines — there is no synchronization or muted-mode around the prompt.

### Under `--log-dir` file logging

- `--log-dir` (`cli.py:47-53`, `cli.py:200-203`) enables a per-session file sink at `<log-dir>/<task-id>.log` (or `session.log`).
- The sink duplicates every stderr write into the file with ANSI stripped (`logger.py:39-43`, `logger.py:61-70`). So the interactive prompt (`  Allow? (y/n): `, option lists, `[ESCALATE]` / `[QUESTION]` lines) is captured plain-text in the log — humans still see it on stderr with color.
- `_ainput` writes directly to `sys.stderr` and flushes (`permissions.py:1137-1138`); it does **not** route through `write_log`, so the prompt hits the terminal but **is not written to the file sink**. Only the `log_escalate` / `log_question_escalate` context above the prompt goes to the file. Result: log files show the escalation event but not the exact `Allow? (y/n):` prompt text.
- Stdin is unaffected — file logging captures output only.

### Non-TTY behavior

- `_interactive_fallback` immediately auto-denies when `sys.stdin.isatty()` is false (`permissions.py:1069-1073`), returning `PermissionResultDeny(message="Non-interactive mode: auto-denied", interrupt=False)`.
- This means: cpp invoked via subprocess from a mika-skills handler, or via `mika ask --agent` piping, or under systemd, or in CI, cannot use the interactive fallback — it silently denies. Not configurable; TTY detection is hard-coded to `sys.stdin`.
- `interrupt=False` on the deny means the SDK returns the denial as a tool_result to the LLM (not an interrupt); the pilot continues and adapts. **Post-cpp#128 the Tier 2 policy path shares this default** — it calls `permissions._denial_is_terminal` and reserves `interrupt=True` for a destination veto (worktree containment / control plane) and tier3-dangerous Bash. The old contrast ("interactive is soft, Tier 2 is always hard") no longer holds; the two differ in *who decides*, not in lethality.

---

## Integration with async / subprocess model

- `_ainput` uses `asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)` (`permissions.py:1139-1140`) — the blocking `readline` runs on the default `ThreadPoolExecutor`, so the asyncio event loop is not blocked. Other coroutines (message pump, guardrail watchdogs) continue to run.
- **However**, the SDK's `can_use_tool` callback is awaited synchronously by the SDK's message pump — the tool result is not returned until the fallback resolves. So while other asyncio tasks run, the *Claude session* is blocked at the tool boundary until the operator answers. This is the same shape as a slow relay agent.
- **Recovery path if the operator hangs the prompt:** none. `_ainput` has no timeout wrapper. `sys.stdin.readline` returns on EOL or EOF; a stuck operator holds the session indefinitely. Ctrl-C at the terminal will raise `KeyboardInterrupt` in the executor thread which propagates as `RuntimeError` through the awaited future — the signal handler in `cli.py` traps SIGINT/SIGTERM for graceful shutdown, so the session ends cleanly. There is no "prompt timed out → default to deny" recovery.

---

## Configurability

**None.** No CLI flags gate the interactive fallback. The behavior is derived from three inputs, none of them user-facing:

| Input | Where | Effect |
|---|---|---|
| `--no-relay` / missing config | `cli.py:44`, `cli.py:207-217` | Sets `relay=False`, routing `create_permission_handler`'s "no-relay → interactive" branch (if reached — see policy gate). |
| `MIKA_PILOT_POLICY_DISABLED=1` | `permissions.py:732` | Bypasses Tier 2, exposing the relay + interactive tail. |
| `sys.stdin.isatty()` | `permissions.py:1069` | TTY-gate; false → auto-deny. |

There is no `--no-interactive-fallback`, no `--interactive-fallback-timeout <ms>`, no environment variable to force-enable it in a non-TTY context. cpp#69 would introduce these knobs if wanted.

---

## Test coverage inventory

Grep for `interactive`, `_interactive`, `_ainput`, `isatty`, `fallback` across `tests/`:

```
tests/test_ipython_magics.py:23:from IPython.core.interactiveshell import InteractiveShell   # unrelated
tests/test_tier1.py:1539: """`node` / `python3` alone = interactive REPL, ...""" # unrelated
```

**Zero targeted tests.** No test constructs a permission handler in `relay=False` mode, no test stubs `sys.stdin`, no test exercises `_ainput`, `_interactive_permission`, `_interactive_question`, or the non-TTY auto-deny branch.

### What should be tested (if cpp#69 extends this surface)

Suggested minimum coverage before extending — file as separate follow-up ticket, not in-scope for cpp#70:

1. `_interactive_fallback` non-TTY branch returns `PermissionResultDeny(message="Non-interactive mode: auto-denied", interrupt=False)`.
2. `_interactive_permission` "y" / "Y" / "yes" / "yep" all Allow; "n" / "" / "no" / garbage all Deny.
3. `_interactive_question` numeric-in-range selects the labeled option verbatim.
4. `_interactive_question` numeric-out-of-range falls through to free-text.
5. `_interactive_question` non-numeric input treated as free-text answer.
6. `_interactive_question` malformed input (missing `questions` list) returns the specific deny message and does not raise.
7. `_ainput` returns without blocking the asyncio loop when other tasks are scheduled concurrently.
8. Full-stack: relay double-fault under `MIKA_PILOT_POLICY_DISABLED=1` in a TTY session routes to `_interactive_fallback` and the resulting decision is recorded via `_record_decision` with `rule_id="relay-fallback-interactive"` (`permissions.py:984`).

---

## Bugs surfaced during investigation

**None observed in the strict sense.** The existing behavior matches its stated contract (docstring at `permissions.py:1-7`). The findings below are design residuals worth naming for cpp#69, not defects to fix here:

1. **Fallback is largely dead code today.** The comment at `permissions.py:894-895` is honest ("only reachable when `MIKA_PILOT_POLICY_DISABLED=1`"), but the implication that the fallback surface is legacy is not documented anywhere else. cpp#69 planning should decide whether to fork (b), unify (c), or delete this path as part of a broader redesign.
2. **Interactive prompt not captured in `--log-dir`.** `_ainput` writes directly to `sys.stderr` bypassing `write_log`, so the `Allow? (y/n):` line and option-numbered choices appear on stderr but never make it into the log file. `log_escalate` / `log_question_escalate` above them do. Operators reading log files see the escalation event but not the exact input prompt. Suggest either routing `_ainput`'s prompt through `write_log` (writes both) OR adding a `write_file_log(prompt)` mirror before the executor call. Follow-up: fine to bundle into cpp#69 or file separately as a minor observability fix — no urgency.
3. **No prompt timeout, no operator-away recovery.** A hung `_ainput` holds the SDK session forever (only SIGINT unwedges it). If cpp#69 puts the interactive path in a live loop, it should introduce a prompt timeout with a default-deny recovery, or at minimum a `KeyboardInterrupt`-friendly cancel path.
4. ~~**Interactive denial uses `interrupt=False`** while Tier 2 uses `interrupt=True`.~~ **RESOLVED by cpp#128.** The two are now aligned by default: an ordinary Tier 2 refusal is also `interrupt=False`, and `interrupt=True` is reserved for a destination veto and tier3-dangerous Bash. The "LLM can fabricate around the denial" rationale this residual recorded is the same counter-reason cpp#128 argues is carried by the session guardrails — specifically by `maxTurns=200`, since `stallThreshold` / `emptyResponseThreshold` / `idleTimeoutMs` cannot fire on a busy refusal-retry loop. Nothing left for cpp#69 to flip here.

None of the above are fixed in this ticket per the guardrails (`NO refactor of permissions.py logic`).

---

## cpp#69 prerequisite readout

The existing interactive fallback:

- has a working stdin/stderr transport with async-safe input dispatch,
- offers only binary (`y`/`n`) or numeric-selection answers with no timeouts, no rate-limiting, no logfile capture of the prompt itself, and no configurability,
- is unreachable in the default deployment posture (Tier 2 policy always *decides* first — post-cpp#128 it terminates only on a destination veto or tier3-dangerous Bash, but it still returns before the fallback either way),
- has zero test coverage,
- returns `interrupt=False` on deny — which post-cpp#128 is what Tier 2 does too for an ordinary refusal, so this is no longer a divergence.

**Recommendation for cpp#69:** **(b) fork** — share the transport primitives (`_ainput`, `_summarize_input`, stderr renderers) but build a fresh interactive loop with configurable enable/disable, prompt timeout with default-deny, first-class logfile capture, and consistent `interrupt=` semantics. Option (a) "extend" would inherit the dead-code position and the design residuals above. Option (c) "unify" (delete + replace) is defensible if cpp#69 decides the fallback is fully redundant with the new mode; that call needs a broader look at the emergency-rollback posture (`MIKA_PILOT_POLICY_DISABLED=1`) before committing.

Final choice belongs to cpp#69 architect review — this ticket only surfaces the facts.
