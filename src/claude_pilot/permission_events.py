"""Permission-decision audit-event emitter (cm#99).

Every permission-classifier decision in :mod:`claude_pilot.permissions` — allow
or deny, from tier1 / tier1.5 / tier2 / relay / interactive / per-spawn — fires
one HTTP event to control-monitor's ``POST /api/v1/permission-events`` endpoint
so cm can track and analyse gating patterns across dispatches.

**Design pillars (Forme b, Prime doctrine).** cm MUST NEVER be in the critical
path of a permission decision. The classifier is fail-CLOSED (deny on error);
this emitter is fail-OPEN — a dead / slow / misconfigured cm can never slow,
block, or influence a decision. Concretely:

1. The producer (:func:`emit`) only touches in-process memory: it puts one
   dict onto a bounded :class:`collections.deque` and signals a condition
   variable, then returns. No network syscall, no filesystem I/O, no logging
   at INFO level. Enqueue is nanoseconds; sub-ms even for hundreds of
   consecutive events. AC2 timing test in ``tests/test_permission_events.py``
   pins this against a black-hole destination.
2. A daemon background worker thread drains the queue and issues the HTTP
   POST with a short per-request timeout (:data:`EVENT_POST_TIMEOUT_SECS`).
   HTTP failures never propagate — the caller's decision has already been
   returned to the SDK.
3. The queue is bounded (:data:`EVENT_QUEUE_MAX`) with drop-OLDEST semantics
   (``deque(maxlen=…)``). Overflow (e.g. cm down for an hour) drops the
   oldest tail, never blocks the producer, never grows unbounded. AC3.

**Contract (FIXED — must match cm ingestion at commit 27ee2f8).** Six fields,
exactly — ``tool_name``, ``decision``, ``rule_id``, ``cwd``, ``tool_use_id``,
``agent_id`` — assembled via an EXPLICIT allowlist in :func:`_build_body` so
no adjacent decision-context field can ever be spread onto the wire (defense
in depth for the corpus-side reconstruction cm already does — the cm side
tolerates unknowns; the wire side never sends them). ``decision`` is
normalised to lowercase ``"allow"`` or ``"deny"`` — cm rejects anything else
with 400.

Env-var gating mirrors :mod:`inbox_writer`:

- ``MIKA_CM_EVENT_LOG_ENABLED`` — ``1`` / ``true`` (case-insensitive) enables;
  anything else disables (module is a total no-op, no thread started).
- ``MIKA_GATEWAY_URL`` — gateway base URL.
- ``MIKA_INTERNAL_TOKEN`` — bearer token, sent as ``X-Internal-Token`` header.

All three must be resolvable for a POST to fire; a missing gateway URL or
token is logged once per-process and further emissions become silent no-ops
so we do not spam stderr in a misconfigured deployment.
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import Any, Final

# ── Tunables (edit-time constants) ───────────────────────────────────────────

#: Bounded queue depth. Chosen to comfortably hold a long-running session's
#: permission stream during a brief cm outage (a busy dispatch fires on the
#: order of hundreds of decisions per session; 1024 leaves generous headroom
#: without a memory-footprint concern). Drop-oldest on overflow — see AC3.
EVENT_QUEUE_MAX: Final[int] = 1024

#: Per-request HTTP timeout on the background worker's POST. Short — the
#: emitter is best-effort and cm's SLO is single-digit ms on the happy path;
#: a slow cm should not backlog the worker with long-hung sockets.
EVENT_POST_TIMEOUT_SECS: Final[float] = 2.0

#: Path suffix for the cm ingestion endpoint. Combined with ``MIKA_GATEWAY_URL``
#: (base) to form the POST URL. Kept as a module constant so the wire contract
#: is discoverable next to the emitter's other invariants.
EVENT_POST_PATH: Final[str] = "/api/v1/permission-events"

#: Cap the graceful atexit drain so process shutdown is never blocked by a
#: dead / slow cm. If the worker cannot drain within this budget we drop the
#: rest — same fail-open discipline as steady-state. Kept small so scripted
#: deploys / CI runs are not slowed by a misconfigured collector.
_ATEXIT_DRAIN_BUDGET_SECS: Final[float] = 0.5

#: Load-bearing allowlist of body fields — cm's ingestion tolerates unknowns
#: but the wire side must never send them (task brief: "EXPLICIT allowlist of
#: the 6 fields when building the JSON body. NEVER spread/serialize the whole
#: decision object"). Tuple to make accidental mutation obvious.
_ALLOWED_BODY_FIELDS: Final[tuple[str, ...]] = (
    "tool_name",
    "decision",
    "rule_id",
    "cwd",
    "tool_use_id",
    "agent_id",
)

#: Accepted decision values on the wire. cm rejects any other value with 400
#: (AC1). We normalise callers' inputs to lowercase here; anything outside this
#: set drops the event on the floor (fail-open — we NEVER raise into the
#: classifier).
_ALLOWED_DECISIONS: Final[frozenset[str]] = frozenset({"allow", "deny"})


# ── Env-gate helpers ─────────────────────────────────────────────────────────


def is_event_log_enabled(raw: str | None) -> bool:
    """Mirror :func:`inbox_writer.is_orchestrator_inbox_enabled` semantics.

    ``1`` and ``true`` (case-insensitive, whitespace-stripped) are on. Anything
    else — ``0``, ``false``, empty, unset — is off.
    """
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true")


# ── Emitter ──────────────────────────────────────────────────────────────────


class PermissionEventEmitter:
    """Bounded-queue, background-thread, fail-open HTTP emitter.

    One instance is created at module load (:data:`_emitter`); callers use the
    module-level :func:`emit` shim. The worker thread is lazy-started on the
    first accepted event so a disabled / never-invoked emitter carries zero
    thread cost.
    """

    def __init__(self, *, queue_max: int = EVENT_QUEUE_MAX) -> None:
        # deque(maxlen=…) gives drop-OLDEST-on-overflow for free: an append on a
        # full deque silently discards the head. AC3.
        self._queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=queue_max)
        # Condition wraps queue mutation and the worker's wait, so an append
        # that races with a wait is impossible to lose (Condition.wait releases
        # the lock, worker acquires on notify). Simpler and racier than
        # Event.set/clear — see docstring in module for the race analysis.
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # Emitted-then-observed diagnostic counters. Kept as ints so the
        # producer path is a single ``+=`` (no allocation). Not exposed on the
        # wire; useful for tests and for a future ``/status`` surface.
        self._enqueued = 0
        self._dropped_overflow = 0
        self._posted = 0
        self._post_errors = 0
        # One-shot flag so we log missing config at most once per process to
        # avoid spamming stderr in a misconfigured deployment.
        self._config_warning_emitted = False

    # -- Public API -----------------------------------------------------------

    def emit(
        self,
        *,
        tool_name: str,
        decision: str,
        rule_id: str,
        cwd: str,
        tool_use_id: str,
        agent_id: str | None,
    ) -> None:
        """Enqueue one permission-decision event. Non-blocking, fail-open.

        Silent no-op when ``MIKA_CM_EVENT_LOG_ENABLED`` is not truthy — no
        thread started, no memory used beyond the deque itself.
        """
        # Env-gate is read at emit-time so a mid-session flip takes effect.
        # This matches inbox_writer's pattern and lets an operator disable the
        # side-channel without restarting the pilot.
        if not is_event_log_enabled(os.environ.get("MIKA_CM_EVENT_LOG_ENABLED")):
            return

        # Normalise + validate decision at the wire boundary. cm rejects any
        # other value with 400, so dropping here saves a round-trip and a
        # log-and-drop cycle on the worker thread. We fail SILENT (drop the
        # event) rather than raise — the classifier has already returned its
        # result; there is nothing to recover.
        wire_decision = decision.strip().lower() if isinstance(decision, str) else ""
        if wire_decision not in _ALLOWED_DECISIONS:
            return

        # Coerce None agent_id to explicit None on the wire. Everything else is
        # a string field so we cast defensively — a Pydantic-typed input path
        # feeds these, but a mid-refactor call site with a stray int must not
        # crash the classifier.
        payload = _build_body(
            tool_name=str(tool_name),
            decision=wire_decision,
            rule_id=str(rule_id),
            cwd=str(cwd),
            tool_use_id=str(tool_use_id) if tool_use_id is not None else "",
            agent_id=str(agent_id) if agent_id is not None else None,
        )

        with self._cond:
            # Track overflow: deque(maxlen=…) silently drops on append when
            # full, so we compare len before/after. Sub-microsecond, no
            # allocation.
            was_full = len(self._queue) == self._queue.maxlen
            self._queue.append(payload)
            self._enqueued += 1
            if was_full:
                self._dropped_overflow += 1
            self._cond.notify()

        # Lazy-start the worker on first enqueue; guarded by _thread being None
        # so a hot loop pays only one None-check per event after the first.
        if self._thread is None:
            self._ensure_worker_started()

    # -- Worker lifecycle -----------------------------------------------------

    def _ensure_worker_started(self) -> None:
        """Idempotent, thread-safe worker start."""
        # Coarse-grained lock via the Condition — start-thread races are rare
        # (only on the very first emit), so the extra ``with self._cond:`` cost
        # doesn't matter in practice.
        with self._cond:
            if self._thread is not None:
                return
            t = threading.Thread(
                target=self._run,
                name="cpp-permission-event-emitter",
                daemon=True,
            )
            self._thread = t
            t.start()
            # Register a best-effort drain on process exit so happy-path
            # events land instead of being silently discarded. Bounded by
            # _ATEXIT_DRAIN_BUDGET_SECS so a dead cm can't block shutdown.
            atexit.register(self._drain_at_exit)

    def _run(self) -> None:
        """Worker loop — drain the queue and POST each event.

        Batches whatever is available on each wakeup to amortise the wait
        cost. A slow POST does NOT hold the queue lock: we snapshot the batch
        under the lock, then release before issuing HTTP.
        """
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping and not self._queue:
                    return
                batch = list(self._queue)
                self._queue.clear()

            for event in batch:
                self._post(event)

    def _post(self, event: dict[str, Any]) -> None:
        """Issue one POST. Never raises — all exceptions are logged-and-dropped.

        Reads env at post-time (not emit-time) so a mid-session config change
        picks up the new gateway URL / token without restart. The env-gate
        (:func:`is_event_log_enabled`) is checked at emit-time only; if the
        operator disables the flag between enqueue and post, the already-queued
        events still ship (they represent decisions that already happened).
        """
        gateway_url = os.environ.get("MIKA_GATEWAY_URL")
        internal_token = os.environ.get("MIKA_INTERNAL_TOKEN")
        if not gateway_url or not internal_token:
            self._warn_config_missing_once()
            return

        url = f"{gateway_url.rstrip('/')}{EVENT_POST_PATH}"
        try:
            data = json.dumps(event).encode("utf-8")
        except (TypeError, ValueError):
            # If json.dumps trips on a non-serializable value we can't recover
            # — drop the event silently. _build_body only accepts str/None so
            # this branch is defense-in-depth.
            self._post_errors += 1
            return

        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-internal-token": internal_token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=EVENT_POST_TIMEOUT_SECS) as resp:
                # Write-and-forget: do NOT read the body. cm's contract is
                # 202 on accept, 400 on malformed, 403 on bad token; none of
                # those require action from us — the classifier decision has
                # already been returned to the SDK.
                status = getattr(resp, "status", None)
                if status == 202:
                    self._posted += 1
                else:
                    # 400 / 403 / other — best-effort log, drop.
                    self._post_errors += 1
                    _stderr_write(
                        f"[claude-pilot cm-emit] unexpected status {status} "
                        f"from {url} (event dropped)\n"
                    )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ):
            # NEVER propagate — cm being down / unreachable / slow must not
            # affect the pilot session in any way. Bump the diagnostic
            # counter and move on.
            self._post_errors += 1

    def _warn_config_missing_once(self) -> None:
        """Once-per-process stderr note when gateway URL / token are missing."""
        if self._config_warning_emitted:
            return
        self._config_warning_emitted = True
        _stderr_write(
            "[claude-pilot cm-emit] MIKA_GATEWAY_URL or MIKA_INTERNAL_TOKEN "
            "unset; permission-event emission disabled.\n"
        )

    # -- Shutdown -------------------------------------------------------------

    def _drain_at_exit(self) -> None:
        """Give the worker a bounded window to flush queued events on exit."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        t = self._thread
        if t is None or not t.is_alive():
            return
        t.join(timeout=_ATEXIT_DRAIN_BUDGET_SECS)
        # If we timed out the daemon thread will be killed with the interpreter.
        # Silent by design — remaining events are lost, which is the fail-open
        # posture.

    # -- Test helpers ---------------------------------------------------------

    def _stats(self) -> dict[str, int]:
        """Snapshot of the counters, for tests. Not part of the public API."""
        return {
            "enqueued": self._enqueued,
            "dropped_overflow": self._dropped_overflow,
            "posted": self._posted,
            "post_errors": self._post_errors,
            "queue_depth": len(self._queue),
        }

    def _wait_until_drained(self, timeout: float) -> bool:
        """Block until the queue is empty or ``timeout`` elapses. Test-only.

        The classifier code path never calls this — it exists so tests can
        deterministically assert on ``_stats`` without a busy-loop.
        """
        deadline = _monotonic() + timeout
        while _monotonic() < deadline:
            with self._cond:
                if not self._queue:
                    # Give the worker a moment to finish the in-flight POST.
                    pass
                else:
                    self._cond.wait(timeout=0.01)
                    continue
            # Small settle so an in-flight _post finishes bumping counters.
            _sleep(0.005)
            with self._cond:
                if not self._queue:
                    return True
        return False


# ── Body builder — EXPLICIT allowlist (defense-in-depth per cm#99 brief) ─────


def _build_body(
    *,
    tool_name: str,
    decision: str,
    rule_id: str,
    cwd: str,
    tool_use_id: str,
    agent_id: str | None,
) -> dict[str, Any]:
    """Assemble the wire body from named args ONLY.

    Explicit-keyword construction is load-bearing: the task brief calls out
    "NEVER spread/serialize the whole decision object". Every field is named
    and enumerated here; adding a new field is an explicit code edit that
    matches cm's schema, not a silent leak from an upstream refactor.
    """
    body = {
        "tool_name": tool_name,
        "decision": decision,
        "rule_id": rule_id,
        "cwd": cwd,
        "tool_use_id": tool_use_id,
        "agent_id": agent_id,
    }
    # Structural assertion: the constructed body's key set MUST equal the
    # allowlist. This is a code-review invariant on top of the explicit
    # construction above; if a future edit adds an unnamed key it trips here
    # rather than leaking to cm. Never raises to the caller — we're inside the
    # emitter, which is fail-open; on a schema mismatch we drop the event.
    if set(body.keys()) != set(_ALLOWED_BODY_FIELDS):
        return {}  # empty body → json.dumps still works; cm will 400 → dropped
    return body


# ── Module-level shim ────────────────────────────────────────────────────────

_emitter = PermissionEventEmitter()


def emit(
    *,
    tool_name: str,
    decision: str,
    rule_id: str,
    cwd: str,
    tool_use_id: str,
    agent_id: str | None,
) -> None:
    """Enqueue a permission-decision event. Non-blocking, fail-open.

    See :class:`PermissionEventEmitter` for the full contract. Silent no-op
    when the env gate is off. Never raises.
    """
    try:
        _emitter.emit(
            tool_name=tool_name,
            decision=decision,
            rule_id=rule_id,
            cwd=cwd,
            tool_use_id=tool_use_id,
            agent_id=agent_id,
        )
    except Exception:
        # Absolute fail-open backstop — the classifier callback must never
        # see an exception from the emitter. Any programming error in the
        # emitter (a Condition-lock deadlock, a Deque impl bug, …) is
        # swallowed here.
        pass


# ── Small helpers isolated for test monkeypatching ───────────────────────────


def _stderr_write(msg: str) -> None:
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except OSError:
        pass


def _monotonic() -> float:
    """Indirection for tests that need to freeze time in the wait helper."""
    import time

    return time.monotonic()


def _sleep(secs: float) -> None:
    import time

    time.sleep(secs)
