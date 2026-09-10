---
title: Idle Watchdog Starvation Fix - Plan
type: fix
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# Idle Watchdog Starvation Fix - Plan

## Goal Capsule

Objective: `senara-solutions/claude-pilot#168` — the internal idle watchdog
(`guardrails.py:_idle_watchdog`) failed to fire on two production stalls
(mika#2246, 2h18 and 58min of total silence, well past the 1800s structural
ceiling). This is the D2 companion of mika#2249 (D1 = the external
mtime-worktree reaper in the mika engine — out of scope here). Three volets:
(1) per-line log timestamps, the diagnostic prerequisite; (2) a live
py-spy/gdb trace of a hung pilot — **not doable from a sandboxed clone, left
for prod**; (3) a bounded anti-starvation fix, investigated and implemented
here.

Means: instrument first (volet 1), then determine — by reading the actual
code, not by assumption — whether the SDK's own message read is genuinely
synchronous-blocking or genuinely async-awaitable, and fix whichever one it
actually is. The investigation (below) found the ticket's own leading
hypothesis (a blocking read inside the bundled CLI transport) does not hold
up against the pinned `claude-agent-sdk==0.2.148` source, and located the
real synchronous blocking call one step further out, in claude-pilot's own
code. The fix targets that.

Authority hierarchy: `senara-solutions/claude-pilot#168` body > this plan >
implementer judgment. The issue names the fix location as a hypothesis
("cause la mieux étayée (non prouvée)") and explicitly asks for the
sync-vs-async determination to be made by reading the code — that
determination, and the judgment calls it produced, are this plan's main
content.

Stop conditions: none that halt. Volet 2 (live process trace) is explicitly
out of reach in a sandboxed clone and is flagged for prod, not faked.

Execution profile: single-repo (`claude-pilot`), four source files
(`logger.py`, `agent.py`, `guardrails.py`, `types.py`) plus three test files
(one new, one extended, one new).

Tail ownership: implementer opens the PR through green CI (gated, no merge);
MPC reviews/merges.

## Investigation: is the blocking call async or sync? (the load-bearing question)

The ticket proposes wrapping `stream.__anext__()` in `_merge_stream`
(`agent.py:939-955` at issue-filing time) in `asyncio.wait_for`, on the
hypothesis that the bundled CLI subprocess transport performs a synchronous
blocking read during a silent 429 backoff. This was checked directly against
the pinned dependency, not assumed:

- `claude_agent_sdk._internal.transport.subprocess_cli` (installed
  `claude-agent-sdk==0.2.148`, matching `pyproject.toml`'s pin exactly) spawns
  the bundled `claude` binary via `anyio.open_process(...)` and reads its
  stdout through `anyio.streams.text.TextReceiveStream`. On the asyncio
  backend (this codebase never touches trio) `anyio`'s process streams are
  non-blocking POSIX pipes wired into the event loop via
  `loop.add_reader`/`asyncio.StreamReader` machinery — genuinely
  async-awaitable, with real checkpoints. `close()`'s own docstring in that
  file explicitly reasons about anyio cancellation semantics and checkpoints,
  which would make no sense if the reads it wraps were synchronous.
- A grep of the whole `claude_agent_sdk` package for blocking primitives
  (`time.sleep`, `subprocess.run`, `.communicate(`, raw socket calls) found
  exactly two `subprocess.run` call sites: `session_resume.py` (macOS
  Keychain credential read, Darwin-only, early-returns elsewhere, and only
  runs once at connect time) and `sessions.py` (`git worktree list`, used for
  session *discovery*, never on the live message-stream path). Neither runs
  during an in-flight turn's silent backoff.
- `_merge_stream` already races the read against the guardrail: `next_msg =
  asyncio.ensure_future(stream.__anext__()); await asyncio.wait({next_msg,
  guardrail_watcher}, return_when=FIRST_COMPLETED)`. `guardrail_watcher`
  awaits `guardrails.wait_aborted()`, which is `asyncio.Event.wait()` —
  and `asyncio.Event.wait()` resolves deterministically and immediately the
  instant `.set()` is called. So IF the SDK read is genuinely
  async-awaitable (confirmed above), this race ALREADY bounds a stuck read
  correctly: once `_idle_watchdog` fires, `guardrail_watcher` resolves,
  `next_msg.cancel()` runs, and `_GUARDRAIL_TRIP` is yielded — no additional
  `asyncio.wait_for` changes that outcome.

**Determination: the SDK's own message read is async-awaitable, not
synchronous-blocking, and the existing `_merge_stream` race already bounds
it correctly.** `tests/test_watchdog_starvation.py::
test_watchdog_fires_on_a_genuinely_stalled_async_stream` pins this: a fake
client whose stream stalls exactly the way a genuinely async read would
stall (an `asyncio.Event` that is never set, not a raise, not a generator
that exhausts) still terminates via `idle_timeout` in ~30ms, with **zero
changes to `_merge_stream`**. That test would already pass on
pre-cpp#168 `main` — it is evidence for a negative (this code path was
never the problem), not a regression pin for a fix made here.

**So where is the real synchronous blocking call?** `agent.py`'s SDK message
loop, three lines below `_merge_stream`'s yield, unconditionally for every
`AssistantMessage`/`ResultMessage`:

```python
record_sdk_message(message)   # transcript_writer.py
```

`record_sdk_message` opens (`Path.open("a", ...)`), writes, and flushes
`$ANTHROPIC_LOG_FILE` **synchronously**, directly on the event-loop thread,
with no bound, on the single highest-frequency unconditional call in the
whole loop (CLAUDE.md's own words: "a single call at the top of
`agent.py`'s `_merge_stream` loop, before every type branch"). `logger.py`'s
`write_log`/`write_file_log` (`sys.stderr.write`+`flush()`, file
`write()`+`flush()`, called from `log_text` for every AssistantMessage
content block, `log_init`, `log_guardrail`, etc.) are the identical shape —
synchronous, unbounded, called from inside the same loop — and share the
same risk. Either one, if the underlying sink stalls (a full pipe buffer
because a parent process — e.g. `dispatch-lib.sh` — is not draining stdout/
stderr, or a hung network mount under `$ANTHROPIC_LOG_FILE` / the file log
path), freezes the interpreter thread the event loop runs on. `asyncio.wait`
— the idle watchdog included — cannot preempt a blocked thread: this is
where that sentence in the issue body is actually true, just not where the
issue's own hypothesis placed it. It also matches the symptom precisely:
"logs stop dead, no terminal marker" is exactly what a frozen writer would
produce, since the very thing that would announce the death (the log write
itself) is what is stuck.

`tests/test_watchdog_starvation.py::
test_watchdog_survives_a_permanently_hung_transcript_write` reproduces this:
`record_sdk_message` monkeypatched to block (bounded to 1s in the test, to
avoid deadlocking the test process's thread-pool teardown — see the test's
own comment; production reality is that this can be unboundedly long) while
the stream itself also stalls. Confirmed to FAIL against the pre-fix
call site (the constant `_TRANSCRIPT_WRITE_TIMEOUT_S` the test monkeypatches
does not exist before this change — reverting `agent.py` alone and re-running
reproduces that).

## Decision: why NOT wrap `stream.__anext__()`, and what to do instead

Given the determination above, three choices were on the table:

1. Implement the ticket's literal line anyway (`asyncio.wait_for` around
   `stream.__anext__()` in `_merge_stream`). **Rejected.** It would be inert
   window-dressing for the confirmed-async read (the existing race already
   bounds it), and a naive implementation adds a new `asyncio.sleep` task on
   the highest-volume code path in the process — a turn emits thousands of
   `StreamEvent`s under `include_partial_messages=True` — which directly
   conflicts with this codebase's own established no-task-churn discipline
   for exactly this kind of per-event overhead (cpp#123's comments on
   `_bump_idle_deadline` are explicit about avoiding task creation "across
   the thousands of deltas a turn emits"). Implementing it would not have
   prevented the mika#2246 stalls, because the failure is not there.
2. Move `record_sdk_message`'s (and `logger.py`'s) I/O onto a persistent
   background thread transparently, so every existing synchronous caller
   keeps working unchanged. **Rejected for `transcript_writer.py`
   specifically**, on inspection of `tests/test_transcript_writer.py`: ~10
   tests (KTD1-5, AC1-3) call `record_sdk_message` directly and assert the
   write already happened by the time the call returns (e.g.
   `test_unserializable_tool_input_degrades_instead_of_losing_the_line`
   reads the file immediately after the call). Making the write
   asynchronous would make those assertions racy/flaky, breaking a
   carefully-built, ticket-annotated existing test contract to fix a
   different ticket — the wrong trade.
3. **Chosen.** Leave `record_sdk_message` itself completely unchanged (still
   fully synchronous; its existing test suite is untouched and still passes
   unmodified). Bound and offload it ONLY at its one call site inside the
   SDK message loop (`agent.py`), via
   `asyncio.wait_for(asyncio.to_thread(record_sdk_message, message),
   timeout=_TRANSCRIPT_WRITE_TIMEOUT_S)`. This is the smallest change that
   actually removes the starvation risk from the event loop: the write now
   runs on a worker thread, so `await`ing it yields control back to the loop
   and `_idle_watchdog`'s own `asyncio.sleep` keeps ticking regardless of how
   long the write takes; the `wait_for` ceiling (5s default) additionally
   bounds how long the loop can be stuck awaiting ONE slow write before
   moving on, converting "the loop is frozen for the write's entire duration
   (unboundedly, up to the full stall)" into "the loop resumes normal,
   correctly-guarded operation after at most `_TRANSCRIPT_WRITE_TIMEOUT_S`."
   A timeout is treated exactly like any other transcript-write failure:
   best-effort, never fatal — consistent with `record_sdk_message`'s own
   documented "fails open" contract. The abandoned worker thread keeps
   running to completion (or forever) in the background; that is a leaked
   thread, not a frozen event loop, and is the trade this fix deliberately
   makes.

**Residual scope decision, stated plainly:** `logger.py`'s per-line
stderr/file writes (`log_text`, `log_init`, `log_guardrail`, ...) share the
identical synchronous-I/O-in-the-hot-loop shape and are NOT bounded by this
PR. They are called from roughly a dozen sites throughout `agent.py`
(vs. `record_sdk_message`'s one unconditional site), and bounding all of them
would be a substantially larger, higher-blast-radius change than this
ticket's own scope suggests it wants. `record_sdk_message` was chosen because
it is (a) unconditional — fires on every AssistantMessage/ResultMessage
regardless of content, unlike `log_text` which only fires on text-bearing
blocks — and (b) explicitly named in CLAUDE.md as sitting "at the TOP of the
loop body." This is a judgment call, flagged here rather than left implicit:
`logger.py`'s writes are a same-shape, mechanical follow-up, not addressed in
this PR.

## Watchdog self-hardening (the alternative cause, cheap to close)

The ticket names a second candidate cause: the fire-and-forget
`_idle_watchdog` task (`guardrails.py:792`, `loop.create_task(...)`, never
awaited by anything) dying on a swallowed exception. Nothing retrieves that
task's result, so an unhandled exception there would surface, at best, as
asyncio's own "Task exception was never retrieved" warning — never as a
terminated session. `_idle_watchdog`'s `try/except asyncio.CancelledError`
now gains a sibling `except Exception` clause: any other exception aborts the
session with a NEW, distinct guardrail reason, `watchdog_error` (additive to
`GuardrailAbortReason.guardrail`'s `Literal`, same pattern as `rate_limited`/
`awaiting_tool`/`awaiting_model` before it), naming the exception type and
message in the abort detail. This is deliberately a DIFFERENT reason from
`idle_timeout`, not a reuse of it: the two failure modes call for different
operator response (a genuinely silent model vs. a bug in the watchdog
itself), and collapsing them would hide exactly the population this
hardening exists to make visible. `asyncio.CancelledError` is unaffected —
pinned by
`test_idle_watchdog_cancellation_is_still_a_clean_noop` — so ordinary
`dispose()` teardown is not misreported as a crash.

## Implementation units

1. **`logger.py` (volet 1)** — `_LineStamper`, a small stateful class
   tracking "am I at the start of a line" across calls (needed because
   `log_text` streams a turn's text in fragments with no trailing newline —
   a naive per-call stamp would either mid-line-stamp a fragment or miss line
   boundaries). Two independent instances: one for `write_log`'s
   stderr+file-fanout stream, one for `write_file_log`'s separate file-only
   stream (e.g. `log_prompt`). Format: `[YYYY-MM-DDTHH:MM:SS.mmmZ] `,
   millisecond-precision UTC.
2. **`agent.py`** — `_TRANSCRIPT_WRITE_TIMEOUT_S = 5.0` and
   `_record_sdk_message_bounded(message)`, offloading `record_sdk_message`
   via `asyncio.to_thread` + `asyncio.wait_for`; the one call site in the SDK
   message loop now `await`s it. `_merge_stream`'s docstring gains the
   investigation summary and the explicit "why not wrap `stream.__anext__()`
   here" reasoning, so the next reader does not have to redo this
   investigation from scratch.
3. **`guardrails.py`** — `_idle_watchdog` gains the `except Exception`
   hardening described above; `_abort`'s `guardrail` parameter `Literal`
   gains `"watchdog_error"`.
4. **`types.py`** — `GuardrailAbortReason.guardrail` `Literal` gains
   `"watchdog_error"`, additive per the existing pattern; `ResultJson`'s
   subtype-enumeration docstring updated to list it.
5. **Tests** — `tests/test_logger.py` (new): per-line timestamp format,
   multi-fragment streaming stamps once, `write_log`/`write_file_log` stamp
   independently. `tests/test_guardrails.py` (extended): watchdog-crash
   hardening + the cancellation-is-still-clean negative control.
   `tests/test_watchdog_starvation.py` (new): the three tests described
   under Investigation/Decision above, plus a productive-stream regression
   control.

## Acceptance Criteria

- AC1 (volet 1): pilot logs are timestamped per line. Met —
  `tests/test_logger.py`.
- AC2 (volet 3): a silent pilot (429/transport silence) is killed by the
  internal watchdog at ≤ `idleTimeout`, a callback IS emitted, never 2h. Met
  for the code-verified real starvation mechanism —
  `test_watchdog_survives_a_permanently_hung_transcript_write`. The SDK-read
  hypothesis is independently pinned as already-safe —
  `test_watchdog_fires_on_a_genuinely_stalled_async_stream`.
- AC3: the alternative cause (watchdog task dies on a swallowed exception) is
  closed cheaply — `test_idle_watchdog_crash_terminates_instead_of_dying_silently`.
- AC4 (ticket's own AC): "cause du non-fire tranchée par trace processus...
  et documentée" — **not met by this PR, and cannot be**, per volet 2 below.
  What IS delivered is a code-audit-based determination (this document's
  Investigation section) that narrows the cause from "somewhere in the SDK
  or claude-pilot" to a specific, named, fixed call site — a live trace is
  still the only way to CONFIRM this was the actual mechanism in the two
  mika#2246 incidents specifically, as opposed to the closely related
  `logger.py` shape (flagged, not fixed, above) or something not yet
  considered.

## Out of scope: volet 2 (live process trace)

A py-spy/gdb dump of a hung pilot requires a LIVE production stall; it is
structurally impossible to produce from a sandboxed clone with no running
production workload, and was not attempted or faked. This remains for an
operator/MPC to capture the next time a pilot stalls in production — it is
the only thing that can definitively settle "blocked asyncio loop" vs
"watchdog task died on a swallowed exception" vs any mechanism not
considered here, as opposed to the code-audit-based determination this plan
makes. If a stall recurs post-merge, `py-spy dump --pid <pid>` (or `gdb -p
<pid>` with the Python gdb extensions) on the live process, cross-referenced
against the now-timestamped log's last line, is the concrete next step.

## Verification

```
uv run pytest tests/test_logger.py tests/test_guardrails.py \
  tests/test_watchdog_starvation.py tests/test_agent.py \
  tests/test_transcript_writer.py -q
```
→ 138 passed (see PR body for verbatim output of this and the full suite).

```
uv run pytest -q                 # full suite
uv run ruff check .
uv run mypy src
./scripts/verify-pipeline.sh
```

## Frères

- `senara-solutions/mika-platform#2249` (D1, the external mtime-worktree
  reaper in the mika engine — not touched here).
- `senara-solutions/mika-platform#1901` (SDK-stall / 429 Anthropic lineage).
- `senara-solutions/claude-pilot#145` (the `_WaitState` machinery this fix's
  investigation reads closely — `AWAITING_TOOL`/`AWAITING_MODEL` share the
  same `_idle_watchdog` loop hardened here).
- `senara-solutions/claude-pilot#165` (the pilot-transcript writer this fix
  bounds without modifying — `transcript_writer.py`, `record_sdk_message`).
