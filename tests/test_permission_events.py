"""Tests for the cm#99 permission-event emitter.

Two axes:

1. **Correctness** — env-gate, allowlist enforcement (6 fields, no leaks),
   decision normalisation, drop-oldest on overflow, silent fail on transport
   error / 400 / 403 / missing config.
2. **Fail-open guarantee (AC2 KEY)** — the emit call must be non-blocking on
   the classifier's critical path even when the destination is dead. A
   black-hole test times 100 emissions against an unroutable endpoint and
   compares to a disabled run; the delta must be within a small tolerance
   (well under the 500ms total-session budget the brief specifies).
"""

from __future__ import annotations

import json
import time
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claude_pilot import permission_events
from claude_pilot.permission_events import (
    EVENT_QUEUE_MAX,
    PermissionEventEmitter,
    _build_body,
    is_event_log_enabled,
)

# ---------------------------------------------------------------------------
# Env-gate — mirrors is_orchestrator_inbox_enabled shape (see inbox_writer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("2", False),
        ("no", False),
        ("anything-else", False),
        ("1", True),
        (" 1 ", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("\ttrue\n", True),
    ],
)
def test_is_event_log_enabled(raw: str | None, expected: bool) -> None:
    assert is_event_log_enabled(raw) is expected


# ---------------------------------------------------------------------------
# _build_body — EXPLICIT 7-field allowlist; nothing extra can ride
# ---------------------------------------------------------------------------

#: The wire contract, in one place, so the two tests that pin it cannot drift
#: apart. cpp#151 B0 added `terminal` as the seventh field.
WIRE_FIELDS = {
    "tool_name",
    "decision",
    "rule_id",
    "cwd",
    "tool_use_id",
    "agent_id",
    "terminal",
}


def test_build_body_produces_exactly_the_allowlisted_fields() -> None:
    body = _build_body(
        tool_name="Bash",
        decision="allow",
        rule_id="tier1-auto-approve",
        cwd="/tmp",
        tool_use_id="tu_1",
        agent_id="agent_x",
    )
    assert set(body.keys()) == WIRE_FIELDS
    assert body["decision"] == "allow"
    # cpp#151 B0: an ALLOW has no lethality — the field is present on the wire
    # and null, never absent, so cm sees a stable schema across decisions.
    assert body["terminal"] is None


def test_build_body_carries_terminal_for_a_lethal_denial() -> None:
    """cpp#151 B0: the audit wire records WHICH refusals also killed the run.

    Both values are asserted in one test because the whole point of the field
    is the distinction — a `terminal` that is always True (or always False)
    carries exactly as little information as the absent field it replaces."""
    lethal = _build_body(
        tool_name="Bash",
        decision="deny",
        rule_id="bash-rm:destination-veto",
        cwd="/tmp",
        tool_use_id="tu_1",
        agent_id=None,
        terminal=True,
    )
    survivable = _build_body(
        tool_name="Bash",
        decision="deny",
        rule_id="bash-grep",
        cwd="/tmp",
        tool_use_id="tu_2",
        agent_id=None,
        terminal=False,
    )
    assert lethal["terminal"] is True
    assert survivable["terminal"] is False


def test_build_body_carries_none_agent_id() -> None:
    body = _build_body(
        tool_name="Bash",
        decision="deny",
        rule_id="policy-deny",
        cwd="/tmp",
        tool_use_id="tu_1",
        agent_id=None,
    )
    assert body["agent_id"] is None
    # Wire-serialisable: json.dumps must succeed on the payload.
    assert json.loads(json.dumps(body))["agent_id"] is None


# ---------------------------------------------------------------------------
# Emitter — env-gate disables entirely (no thread, no memory beyond deque)
# ---------------------------------------------------------------------------


def test_emit_no_op_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIKA_CM_EVENT_LOG_ENABLED", raising=False)
    e = PermissionEventEmitter()
    for _ in range(50):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1",
            cwd="/tmp",
            tool_use_id="tu",
            agent_id=None,
        )
    stats = e._stats()
    assert stats["enqueued"] == 0
    assert stats["queue_depth"] == 0
    # No worker thread started when disabled.
    assert e._thread is None


def test_emit_no_op_when_flag_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "0")
    e = PermissionEventEmitter()
    e.emit(
        tool_name="Bash",
        decision="allow",
        rule_id="tier1",
        cwd="/tmp",
        tool_use_id="tu",
        agent_id=None,
    )
    assert e._stats()["enqueued"] == 0


# ---------------------------------------------------------------------------
# Decision normalisation — only "allow"/"deny" reach the wire (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["allow", "ALLOW", " Allow ", "deny", "DENY"])
def test_emit_accepts_case_insensitive_allow_deny(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    e = PermissionEventEmitter()
    e.emit(
        tool_name="Bash",
        decision=decision,
        rule_id="tier1",
        cwd="/tmp",
        tool_use_id="tu",
        agent_id=None,
    )
    assert e._stats()["enqueued"] == 1


@pytest.mark.parametrize("decision", ["escalate", "answer", "", "true", None])
def test_emit_drops_unknown_decision(
    monkeypatch: pytest.MonkeyPatch, decision: Any
) -> None:
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    e = PermissionEventEmitter()
    e.emit(
        tool_name="Bash",
        decision=decision,
        rule_id="tier1",
        cwd="/tmp",
        tool_use_id="tu",
        agent_id=None,
    )
    assert e._stats()["enqueued"] == 0


# ---------------------------------------------------------------------------
# Bounded queue, drop-OLDEST on overflow (AC3)
# ---------------------------------------------------------------------------


def test_queue_drops_oldest_on_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fill the queue past its cap with a stalled worker; count overflow drops."""
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    small_cap = 8
    e = PermissionEventEmitter(queue_max=small_cap)
    # Prevent the worker from draining the queue while we test the ring buffer.
    # We install a monkeypatched _post that never runs (worker never fires
    # because it also would need the env-driven URL). Instead we simply do NOT
    # let the worker start: patch _ensure_worker_started to a no-op.
    e._ensure_worker_started = lambda: None  # type: ignore[method-assign]

    for i in range(small_cap * 3):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id=f"rule-{i}",
            cwd="/tmp",
            tool_use_id=f"tu-{i}",
            agent_id=None,
        )
    stats = e._stats()
    assert stats["enqueued"] == small_cap * 3
    # Queue holds at most the cap; overflow count = enqueued - cap.
    assert stats["queue_depth"] == small_cap
    assert stats["dropped_overflow"] == small_cap * 2
    # Drop-OLDEST verification: the queue's head should be the (small_cap*2)-th
    # event, not the 0th. Inspect the ring directly (test-only).
    with e._cond:
        head = e._queue[0]
    assert head["rule_id"] == f"rule-{small_cap * 2}"


# ---------------------------------------------------------------------------
# End-to-end POST — happy path shape (202), body contents, headers
# ---------------------------------------------------------------------------


def _make_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, exc_type, exc_val, exc_tb: None
    return resp


def test_worker_posts_event_with_wire_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.setenv("MIKA_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok-xyz")

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _make_response(202)

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1-auto-approve",
            cwd="/tmp/work",
            tool_use_id="tu_42",
            agent_id="agent_x",
        )
        # Wait for the background worker to drain.
        assert e._wait_until_drained(timeout=2.0), "worker never drained the queue"

    assert captured["url"] == "https://gw.example/api/v1/permission-events"
    assert captured["method"] == "POST"
    # urllib header casing normalises to Header-Case on Request.headers dict.
    assert captured["headers"]["X-internal-token"] == "tok-xyz"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["timeout"] > 0

    body = json.loads(captured["body"])
    assert set(body.keys()) == WIRE_FIELDS
    assert body["decision"] == "allow"
    assert body["rule_id"] == "tier1-auto-approve"
    assert body["cwd"] == "/tmp/work"
    assert body["tool_use_id"] == "tu_42"
    assert body["agent_id"] == "agent_x"

    stats = e._stats()
    assert stats["posted"] == 1
    assert stats["post_errors"] == 0


def test_worker_survives_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong token / 403 must NOT propagate, NOT retry, NOT influence any
    caller. The event is dropped; the emitter bumps the post_errors counter."""
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.setenv("MIKA_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "bad-tok")

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen",
        return_value=_make_response(403),
    ):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1",
            cwd="/tmp",
            tool_use_id="tu",
            agent_id=None,
        )
        assert e._wait_until_drained(timeout=2.0)

    stats = e._stats()
    assert stats["posted"] == 0
    assert stats["post_errors"] == 1


def test_worker_survives_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed body / 400 is log-and-drop, not a retry loop."""
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.setenv("MIKA_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok")

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen",
        return_value=_make_response(400),
    ):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1",
            cwd="/tmp",
            tool_use_id="tu",
            agent_id=None,
        )
        assert e._wait_until_drained(timeout=2.0)

    assert e._stats()["post_errors"] == 1


def test_worker_survives_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport error / connection refused must be swallowed."""
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.setenv("MIKA_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok")

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1",
            cwd="/tmp",
            tool_use_id="tu",
            agent_id=None,
        )
        assert e._wait_until_drained(timeout=2.0)

    assert e._stats()["post_errors"] == 1


def test_worker_survives_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.setenv("MIKA_GATEWAY_URL", "https://gw.example")
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok")

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen",
        side_effect=TimeoutError("slow"),
    ):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1",
            cwd="/tmp",
            tool_use_id="tu",
            agent_id=None,
        )
        assert e._wait_until_drained(timeout=2.0)

    assert e._stats()["post_errors"] == 1


def test_worker_silent_on_missing_gateway_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing MIKA_GATEWAY_URL → no POST is attempted, warning at most once."""
    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
    monkeypatch.delenv("MIKA_GATEWAY_URL", raising=False)
    monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok")

    e = PermissionEventEmitter()
    with patch(
        "claude_pilot.permission_events.urllib.request.urlopen"
    ) as urlopen_mock:
        for _ in range(5):
            e.emit(
                tool_name="Bash",
                decision="allow",
                rule_id="tier1",
                cwd="/tmp",
                tool_use_id="tu",
                agent_id=None,
            )
        assert e._wait_until_drained(timeout=2.0)
        urlopen_mock.assert_not_called()

    # Warning should be emitted at most once per process (5 events, 1 warning).
    err = capsys.readouterr().err
    assert err.count("cm-emit") <= 1


# ---------------------------------------------------------------------------
# AC2 (KEY) — fail-open, non-blocking black-hole timing test
# ---------------------------------------------------------------------------
#
# The task brief specifies:
#   "point emitter at 127.0.0.2:1 (RFC5737-like unreachable), fire 100
#    permission decisions, compare wall-clock to a run with
#    MIKA_CM_EVENT_LOG_ENABLED=0. Both runs must be within noise floor of each
#    other (i.e., emitter adds ≤noise ms even when destination is dead). If
#    black-hole run is meaningfully slower → AC2 fails. Wire this timing test
#    into the test suite so pipeline catches regressions."
#
# The producer path is deque.append + Condition.notify — no network call, no
# blocking primitive. So even with a black-hole destination the enqueue path
# stays sub-microsecond. The threshold below is intentionally generous (100ms
# for 100 events = 1ms/event upper bound) to survive noisy CI while still
# catching the AC2 regression class (accidentally awaiting the HTTP call on
# the producer thread would take ~2s per event x 100 = 200s in the black-hole
# case, blowing this threshold by four orders of magnitude).


BLACKHOLE_URL = "http://127.0.0.2:1"
N_EVENTS = 100
# Per-event upper bound in the black-hole case relative to the disabled case.
# Enqueue is nanoseconds in isolation; we allow up to 5ms total delta over 100
# events to absorb GIL noise / GC pauses on a busy runner without letting a
# regression to sync-post slip through (that class blows the budget by 1000x).
MAX_TOTAL_DELTA_SECS = 0.100


def _time_emissions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> float:
    """Time N_EVENTS emissions with the emitter en/disabled. Returns wall-clock
    in seconds.

    A fresh :class:`PermissionEventEmitter` is used per call so state from a
    prior run (thread, queue depth) does not contaminate the measurement.
    """
    if enabled:
        monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")
        monkeypatch.setenv("MIKA_GATEWAY_URL", BLACKHOLE_URL)
        monkeypatch.setenv("MIKA_INTERNAL_TOKEN", "tok")
    else:
        monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "0")

    e = PermissionEventEmitter()
    # Ensure the emit path is what we time — not first-touch imports / thread
    # startup — by firing one warm-up event before the timed loop.
    e.emit(
        tool_name="Bash",
        decision="allow",
        rule_id="warmup",
        cwd="/tmp",
        tool_use_id="tu_warm",
        agent_id=None,
    )

    start = time.perf_counter()
    for i in range(N_EVENTS):
        e.emit(
            tool_name="Bash",
            decision="allow",
            rule_id="tier1-auto-approve",
            cwd="/tmp",
            tool_use_id=f"tu_{i}",
            agent_id=None,
        )
    return time.perf_counter() - start


def test_ac2_blackhole_producer_never_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC2 KEY test — the emitter must be non-blocking even at a dead endpoint.

    If a regression turns the producer path back into synchronous HTTP (e.g.
    someone removes the queue and calls urlopen inline), this test explodes:
    100 attempts to reach 127.0.0.2:1 with a 2s socket-timeout would take on
    the order of 200s, far outside the noise floor.
    """
    disabled_secs = _time_emissions(monkeypatch, enabled=False)
    enabled_secs = _time_emissions(monkeypatch, enabled=True)

    delta = enabled_secs - disabled_secs
    # Emit a human-readable diagnostic so the summary can quote the numbers
    # even when the test passes (see task brief deliverable #2).
    with capsys.disabled():
        print(
            f"\n[AC2 timing] N={N_EVENTS} events  "
            f"disabled={disabled_secs * 1000:.3f}ms  "
            f"blackhole-enabled={enabled_secs * 1000:.3f}ms  "
            f"delta={delta * 1000:.3f}ms  "
            f"budget<={MAX_TOTAL_DELTA_SECS * 1000:.0f}ms"
        )

    assert delta < MAX_TOTAL_DELTA_SECS, (
        f"AC2 fail-open regression: emitter added {delta * 1000:.3f}ms over "
        f"{N_EVENTS} events at a black-hole destination; budget is "
        f"{MAX_TOTAL_DELTA_SECS * 1000:.0f}ms. Producer path may be blocking "
        f"on the network instead of enqueueing."
    )


# ---------------------------------------------------------------------------
# Module-level emit shim — swallows exceptions absolutely (backstop)
# ---------------------------------------------------------------------------


def test_module_emit_swallows_emitter_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the inner emitter raises for any reason, the module-level shim
    must never let the exception escape into the classifier callback."""

    monkeypatch.setenv("MIKA_CM_EVENT_LOG_ENABLED", "1")

    def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(permission_events._emitter, "emit", _boom)
    # Must not raise.
    permission_events.emit(
        tool_name="Bash",
        decision="allow",
        rule_id="tier1",
        cwd="/tmp",
        tool_use_id="tu",
        agent_id=None,
    )


# ---------------------------------------------------------------------------
# Regression: default queue cap matches EVENT_QUEUE_MAX
# ---------------------------------------------------------------------------


def test_default_queue_cap_matches_constant() -> None:
    e = PermissionEventEmitter()
    assert e._queue.maxlen == EVENT_QUEUE_MAX
