---
title: Transport Buffer-Overflow Clean Halt - Plan
type: fix
date: 2026-09-15
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# Transport Buffer-Overflow Clean Halt - Plan

## Goal Capsule

Objective: `senara-solutions/claude-pilot#187` — a session-ending
`CLIJSONDecodeError` crashes claude-pilot outright whenever the bundled SDK's
line-framer buffer guard trips on a single incoming SDK message larger than
`max_buffer_size` (default 1MB). Reproduced twice on 2026-09-15: mika#2295
(dev-pilot on mika#2310's predecessor, turn 82, ~$4.50 spent, work done,
awaiting sub-agent review results, exit 1, zero verdict, draft-PR salvage
only via the mika#1282 dirty-worktree recovery) and mika#2310 (same fatal,
turn 20, stochastic — not tied to a specific turn). Both times the trigger
was a completed `Agent` tool sub-agent whose result came back as one SDK
message over 1MB (a review payload with a diff/plan embedded).

**Scope, per samidarko's dispatch comment on the issue**: ONLY problem #1
(the transport crash). Problem #2 (a non-terminal `policy:deny` misattributed
as the cause of a later crash) is `cpp#184` — untouched here.

Means: read `subprocess_cli.py` in the pinned `claude-agent-sdk==0.2.152` to
find the exact locus and exception shape, verify whether the SDK exposes a
configurable buffer ceiling, then add a targeted catch at the one call site
in `agent.py` where the SDK message stream is consumed, converting the crash
into a named, clean halt — plus raise the SDK's own buffer cap so most large
reviews never need the fallback at all.

Authority hierarchy: `senara-solutions/claude-pilot#187` body + samidarko's
dispatch comment > this plan > implementer judgment.

Stop conditions: none that halt — the SDK genuinely exposes the buffer-size
knob (verified below), so both parts of the dispatch's two-pronged fix apply;
no case where fix #2 had to be skipped for lack of an SDK API.

Execution profile: single-repo (`claude-pilot`), two source files
(`agent.py`, `types.py`), one test file (`tests/test_agent.py`, extended).

Tail ownership: implementer opens the PR through green CI (gated, no merge);
MPC reviews/merges.

## Locus, verified against the pinned SDK (`claude-agent-sdk==0.2.152`)

Confirmed by reading
`.venv/lib/python3.14/site-packages/claude_agent_sdk/_internal/transport/subprocess_cli.py`,
not assumed from the ticket:

- `_DEFAULT_MAX_BUFFER_SIZE = 1024 * 1024` (`:35`) — the 1MB the crash names.
- `SubprocessCLITransport.__init__` (`:240-244`): `self._max_buffer_size =
  options.max_buffer_size if options.max_buffer_size is not None else
  _DEFAULT_MAX_BUFFER_SIZE`. **`ClaudeAgentOptions.max_buffer_size: int |
  None = None`** is a real, documented field (`types.py:2098`,
  `"Maximum bytes to buffer when reading the CLI subprocess stdout."`) — the
  SDK DOES expose the knob.
- `_read_messages_impl`'s inner `guard(length)` (`:1090-1098`): called on
  every framed line and on the still-buffering tail (`:1103`, `:1107`). If
  `length > self._max_buffer_size` it raises:
  ```python
  raise SDKJSONDecodeError(
      f"JSON message exceeded maximum buffer size of {self._max_buffer_size} bytes",
      ValueError(f"Buffer size {length} exceeds limit {self._max_buffer_size}"),
  )
  ```
  — i.e. `CLIJSONDecodeError` (aliased `SDKJSONDecodeError`), whose
  `original_error` is a bare `ValueError`.
- `_parse_stdout_line` (`:191-213`) raises the SAME exception TYPE for
  genuinely corrupt JSON (`json.loads` failure on a complete, correctly
  framed line) — `raise SDKJSONDecodeError(line, e) from e` where `e` is a
  `json.JSONDecodeError`. Two different failure classes, one exception type,
  distinguished only by `original_error`'s concrete type and message.
- This all happens inside `_read_messages_impl`, the generator behind
  `client.receive_response()`. Upstream of it there is no seam: the reader
  raises before yielding the oversized message to anyone, so **truncating or
  splitting the sub-agent's payload from claude-pilot's side is not an
  option** — the crash happens before claude-pilot ever sees a byte of that
  message.
- claude-pilot's own consumption point is `agent.py`'s `_merge_stream`
  (`:1009 pre-fix` / renumbered post-fix), which does
  `next_msg = asyncio.ensure_future(stream.__anext__())` then
  `msg = next_msg.result()` — an unguarded `.result()` re-raises whatever the
  reader raised. That propagates out of `_merge_stream`'s `async for` loop in
  `_run_agent_inner`, straight into the existing
  ```python
  except Exception as exc:
      if resume_from is None:
          raise
      ...
  ```
  block. On a FIRST session (`resume_from is None` — true for most of a
  run's life; a resume only exists after cpp#151's deny-resume machinery has
  already fired once) this **re-raises**, escaping `_run_agent_inner`,
  `run_agent`, and out of the process — the bare exit-1 crash the ticket
  reports. Even on a resumed session, the existing generic handler would
  swallow it as a generic resume-failure, losing the specific "the SDK
  reader hit its buffer cap" classification.

## Fix #1 — guaranteed clean halt (always implemented)

Added a dedicated `except CLIJSONDecodeError as exc:` clause **before** the
existing generic `except Exception as exc:` in `_run_agent_inner`'s session
loop (`src/claude_pilot/agent.py`):

- A new helper, `_is_transport_buffer_overflow(exc) -> bool`, distinguishes
  the two `CLIJSONDecodeError` shapes:
  ```python
  err = exc.original_error
  if isinstance(err, json.JSONDecodeError):
      return False
  return isinstance(err, ValueError) and "exceeds limit" in str(err)
  ```
  `json.JSONDecodeError` subclasses `ValueError`, so the check is written the
  narrow way round on purpose: buffer-overflow is "a `ValueError` that is
  NOT a `JSONDecodeError`" AND carries the guard's own wording
  (`"exceeds limit"`, from the `ValueError` message the guard constructs).
  Requiring both means a hypothetical future SDK release that raises some
  *other* bare `ValueError` from a genuinely new failure mode falls through
  to the generic handler rather than being silently mislabeled
  `transport_message_too_large` — the conservative direction to err in.
- If `_is_transport_buffer_overflow` is `False` (genuine malformed JSON):
  falls through to EXACTLY the pre-fix behaviour — re-raise on the first
  session, swallow-and-report-deferred on a resumed one. Real decode
  corruption is a different failure class and must not be laundered into a
  clean "too large" halt.
- If `True`: emits a `ResultJson` with `status="terminated"`,
  `subtype="transport_message_too_large"`, `termination_reason` carrying the
  raw SDK error text, `turns=guardrails.turns`, `cost_usd=None` (no terminal
  `ResultMessage` ever arrived), then `log_guardrail(...)` and `return 1`.
  No `client.interrupt()` call: unlike the existing guardrail-trip site
  (which catches from *inside* the `async with ClaudeSDKClient(...)` block
  while `client` is still open), this catch is OUTSIDE that block — Python
  has already run `__aexit__` (client cleanup) by the time control reaches
  it, so there is no live client left to interrupt.
- **Judgment call — not routed through `SessionGuardrails`/
  `GuardrailAbortReason`.** The dispatch comment says "additive exactly like
  `watchdog_error`/`rate_limited`/`prompt_cache_dead`" as a *design*
  precedent (name a distinct reason, document it as additive, never collapse
  into a generic crash) — not necessarily "must literally live in
  `guardrails.py`". Those three all originate INSIDE `SessionGuardrails`'
  own idle-watchdog task and are surfaced via the `_GUARDRAIL_TRIP` sentinel
  merged into the stream by `_merge_stream`. `transport_message_too_large`
  originates in the SDK's transport reader — a different layer entirely,
  reached only via a raised exception, never via the guardrail watchdog. Two
  reasonable options existed: (a) catch it at the transport call site and
  translate it into a guardrail-style abort by hand-triggering
  `SessionGuardrails._abort(...)` with a widened `Literal`, or (b) handle it
  directly at the catch site with its own `ResultJson`, matching the
  `stream_ended_without_result` / `EDE_AFTER_DENY_SUBTYPE` precedent already
  in the same function for exactly this kind of "session loop caught
  something and needs to emit its own terminal line" case. Chose (b): it
  keeps `SessionGuardrails`' contract (it aborts only for conditions ITS OWN
  watchdog observes) intact, avoids widening a `Literal` for a reason the
  guardrail object itself never produces, and mirrors an existing pattern in
  the same file rather than inventing a new one. `ResultJson.subtype` is a
  free `str` (not a `Literal`), so this needed no schema change beyond a
  docstring bullet (`types.py`) naming the new value — additive in the exact
  sense `ResultJson`'s own docstring already establishes for guardrail
  subtypes ("dispatch-lib takes it OPAQUELY... adding a subtype is safely
  additive").

## Fix #2 — raise `max_buffer_size` to a named cap (implemented — SDK exposes it)

`ClaudeAgentOptions.max_buffer_size` is real (see Locus above), so
`_run_agent_inner`'s `ClaudeAgentOptions(...)` construction now passes
`max_buffer_size=_SDK_MAX_BUFFER_SIZE_BYTES`, a new module-level constant:

```python
_SDK_MAX_BUFFER_SIZE_BYTES = 10 * 1024 * 1024  # 10MB
```

**Judgment call — 10MB, not measured.** The ticket does not supply a
distribution of observed sub-agent result sizes, only that the two known
crashes exceeded 1MB. 10MB is a round order-of-magnitude headroom: large
enough that an ordinary large review (diff + plan text) clears it
comfortably, small enough to still bound a single message rather than
removing the guard entirely (an unbounded buffer turns one oversized
message into an unbounded memory allocation, which is a real DoS-shaped
failure mode of its own). This is flagged for MPC/operator revision once
real payload-size telemetry exists — the fix #1 fallback stays load-bearing
regardless of where the cap is set, so raising or lowering
`_SDK_MAX_BUFFER_SIZE_BYTES` later is a one-line, test-covered change
(`test_187_max_buffer_size_is_raised_to_a_named_ceiling`).

Because fix #1 is unconditional, a message that still exceeds the raised
10MB cap is not a silently-moved cliff: it degrades to the same clean
`transport_message_too_large` halt fix #1 provides, not to a crash.

## The mandatory negative test

`tests/test_agent.py`, new section "cpp#187: transport buffer-overflow —
`CLIJSONDecodeError` must not crash the session":

- `_buffer_overflow_error()` / `_malformed_json_error()`: build the two
  `CLIJSONDecodeError` shapes exactly as the pinned SDK constructs them (the
  second via an actual `json.loads` failure, not a hand-rolled
  `JSONDecodeError`, so the discriminator is tested against the real
  exception the stdlib produces).
- `_RaisingClient` / `_install_raising_client`: a `receive_response()` fake
  that yields a scripted message prefix then raises — the seam that
  `_merge_stream`'s `next_msg.result()` actually surfaces the SDK's
  exception through, simulating "the reader dies mid-stream" without needing
  a real >1MB payload.
- `test_187_is_transport_buffer_overflow_distinguishes_the_two_shapes` —
  unit-level: the discriminator returns `True` only for the buffer-overflow
  shape, `False` for genuine JSON corruption AND for an unrelated bare
  `ValueError` (conservative-default check).
- **`test_187_buffer_overflow_ends_session_cleanly_not_a_crash`** — the
  mandated negative test. RED on pre-fix code (`CLIJSONDecodeError`
  propagates out of `run_agent`, uncaught — verified by temporarily
  reverting `agent.py`'s new except-clause and re-running: 3 of the 5 new
  tests fail, this one with the raw `CLIJSONDecodeError` traceback). GREEN
  after: `status="terminated"`, `subtype="transport_message_too_large"`,
  `exit_code == 1`, exactly one terminal JSON line.
- `test_187_genuine_malformed_json_still_raises` — malformed-JSON regression
  guard: `pytest.raises(CLIJSONDecodeError)` around `run_agent`, unchanged
  from pre-fix behaviour on a first session.
- `test_187_normal_small_message_session_unaffected` — happy-path regression
  guard: an ordinary session with no error still reports `status="success"`,
  `exit_code == 0`.
- `test_187_max_buffer_size_is_raised_to_a_named_ceiling` — asserts
  `ClaudeAgentOptions(...)` is constructed with
  `max_buffer_size=_SDK_MAX_BUFFER_SIZE_BYTES` and that the value exceeds the
  SDK's 1MB default.

Anti-vacuity: reverting only the new `except CLIJSONDecodeError` clause (via
`git stash` on `agent.py` alone, tests untouched) reproduces the RED state —
confirmed directly, not inferred (see full report for verbatim output).

## Scope note

`cpp#184` (the ticket's problem #2 — a non-terminal `policy:deny` at
04:56:26Z misattributed as the cause of the 05:06:49Z crash, when the
session actually continued producing tool:request events for ~10 more
minutes after the deny) is **out of scope** for this fix and untouched by
it. This plan and its source change cover ONLY the transport buffer-overflow
crash (problem #1).

## Verification

`uv sync --extra dev` (NOT `--dev` — this repo's dev deps are behind the
`dev` extra), then `uv run pytest`, `uv run ruff check .`, `uv run mypy src`,
`./scripts/verify-pipeline.sh`. All green — verbatim output in the PR/report.
