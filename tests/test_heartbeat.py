"""Tests for the cm heartbeat writer (cpp#111, D8 subsystem 2 client-side)."""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claude_pilot import heartbeat as heartbeat_module
from claude_pilot.heartbeat import (
    DEFAULT_CM_API_URL,
    DEFAULT_ENTITY,
    HEARTBEAT_TIMEOUT_SECS,
    _build_url,
    emit_heartbeat,
    emit_heartbeat_throttled,
    is_heartbeat_disabled,
    reset_throttle_state,
)

# ── Pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("anything-else", False),
        ("1", True),
        (" 1 ", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("\ttrue\n", True),
    ],
)
def test_is_heartbeat_disabled(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    if raw is None:
        monkeypatch.delenv("CM_HEARTBEAT_DISABLED", raising=False)
    else:
        monkeypatch.setenv("CM_HEARTBEAT_DISABLED", raw)
    assert is_heartbeat_disabled() is expected


def test_build_url_normalises_trailing_slash() -> None:
    assert (
        _build_url("http://127.0.0.1:8090/", "pilot")
        == "http://127.0.0.1:8090/api/v1/agents/pilot/heartbeat"
    )
    assert (
        _build_url("http://127.0.0.1:8090", "pilot")
        == "http://127.0.0.1:8090/api/v1/agents/pilot/heartbeat"
    )
    assert (
        _build_url("http://127.0.0.1:8090//", "cm-pilot")
        == "http://127.0.0.1:8090/api/v1/agents/cm-pilot/heartbeat"
    )


# ── emit_heartbeat env-gating ───────────────────────────────────────────────


def _configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up a fully-configured heartbeat env for tests that need the POST
    to actually attempt a fire."""
    monkeypatch.delenv("CM_HEARTBEAT_DISABLED", raising=False)
    monkeypatch.setenv("CM_TOKEN", "cm-tok-xyz")
    monkeypatch.setenv("CM_API_URL", "http://cm.example:8090")
    monkeypatch.delenv("CM_HEARTBEAT_ENTITY", raising=False)


def test_emit_heartbeat_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CM_HEARTBEAT_DISABLED", "1")
    monkeypatch.setenv("CM_TOKEN", "tok")
    with patch("claude_pilot.heartbeat.urllib.request.urlopen") as urlopen_mock:
        assert emit_heartbeat("session:t-1") is False
        urlopen_mock.assert_not_called()


def test_emit_heartbeat_skips_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CM_HEARTBEAT_DISABLED", raising=False)
    monkeypatch.delenv("CM_TOKEN", raising=False)
    monkeypatch.setenv("CM_API_URL", "http://cm.example:8090")
    with patch("claude_pilot.heartbeat.urllib.request.urlopen") as urlopen_mock:
        assert emit_heartbeat("session:t-1") is False
        urlopen_mock.assert_not_called()


# ── emit_heartbeat success path ─────────────────────────────────────────────


def _mock_201_response() -> MagicMock:
    response = MagicMock()
    response.status = 201
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, exc_type, exc_val, exc_tb: None
    return response


def test_emit_heartbeat_posts_to_cm_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        assert emit_heartbeat("session:mika#1878") is True

    assert captured["url"] == "http://cm.example:8090/api/v1/agents/pilot/heartbeat"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer cm-tok-xyz"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["timeout"] == HEARTBEAT_TIMEOUT_SECS
    payload = json.loads(captured["body"])
    assert payload["reason"] == "session:mika#1878"
    assert "meta" not in payload  # omitted when not passed


def test_emit_heartbeat_includes_meta_when_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["body"] = req.data
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        assert emit_heartbeat("recovery:tool", meta={"tool": "Bash", "action": "allow"}) is True

    payload = json.loads(captured["body"])
    assert payload["reason"] == "recovery:tool"
    assert payload["meta"] == {"tool": "Bash", "action": "allow"}


def test_emit_heartbeat_uses_env_entity_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setenv("CM_HEARTBEAT_ENTITY", "cm-pilot")

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["url"] = req.full_url
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        emit_heartbeat("session:t-1")

    assert captured["url"] == "http://cm.example:8090/api/v1/agents/cm-pilot/heartbeat"


def test_emit_heartbeat_explicit_entity_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setenv("CM_HEARTBEAT_ENTITY", "cm-pilot")

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["url"] = req.full_url
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        emit_heartbeat("session:t-1", entity="explicit-entity")

    assert captured["url"] == "http://cm.example:8090/api/v1/agents/explicit-entity/heartbeat"


def test_emit_heartbeat_falls_back_to_default_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CM_HEARTBEAT_DISABLED", raising=False)
    monkeypatch.setenv("CM_TOKEN", "tok")
    monkeypatch.delenv("CM_API_URL", raising=False)
    monkeypatch.delenv("CM_HEARTBEAT_ENTITY", raising=False)

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> Any:
        captured["url"] = req.full_url
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        emit_heartbeat("session:t-1")

    assert captured["url"].startswith(DEFAULT_CM_API_URL)
    assert f"/agents/{DEFAULT_ENTITY}/heartbeat" in captured["url"]


# ── Error paths — must return False, never raise ────────────────────────────


def test_emit_heartbeat_returns_false_on_non_201_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    response = MagicMock()
    response.status = 500
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, exc_type, exc_val, exc_tb: None

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        return_value=response,
    ):
        assert emit_heartbeat("session:t-1") is False


def test_emit_heartbeat_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "http://cm.example:8090",
            500,
            "boom",
            {},
            None,  # type: ignore[arg-type]
        ),
    ):
        assert emit_heartbeat("session:t-1") is False


def test_emit_heartbeat_returns_false_on_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cm unreachable — the DoS-of-observability class must not crash pilot."""
    _configured_env(monkeypatch)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        assert emit_heartbeat("session:t-1") is False


def test_emit_heartbeat_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cm slow — the 500ms timeout kicks in, POST is dropped, pilot never
    stalls (design constraint from cpp#111)."""
    _configured_env(monkeypatch)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=TimeoutError("slow cm"),
    ):
        assert emit_heartbeat("session:t-1") is False


def test_emit_heartbeat_passes_500ms_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the exact 500ms cap is passed to urlopen — the design
    constraint's teeth."""
    _configured_env(monkeypatch)

    seen_timeouts: list[float] = []

    def fake_urlopen(req: Any, timeout: float) -> Any:
        seen_timeouts.append(timeout)
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        emit_heartbeat("session:t-1")

    assert seen_timeouts == [0.5]


# ── Throttled emit ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    """Ensure per-key rate-limit state doesn't leak between tests."""
    reset_throttle_state()


def test_emit_heartbeat_throttled_fires_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        return_value=_mock_201_response(),
    ) as urlopen_mock:
        result = emit_heartbeat_throttled(
            "turn:1",
            throttle_key="pilot:turn",
            min_interval_secs=60.0,
        )

    assert result is True
    urlopen_mock.assert_called_once()


def test_emit_heartbeat_throttled_suppresses_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_env(monkeypatch)

    call_count = [0]

    def fake_urlopen(req: Any, timeout: float) -> Any:
        call_count[0] += 1
        return _mock_201_response()

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        first = emit_heartbeat_throttled(
            "turn:1", throttle_key="pilot:turn", min_interval_secs=60.0
        )
        second = emit_heartbeat_throttled(
            "turn:2", throttle_key="pilot:turn", min_interval_secs=60.0
        )
        third = emit_heartbeat_throttled(
            "turn:3", throttle_key="pilot:turn", min_interval_secs=60.0
        )

    assert first is True
    assert second is False
    assert third is False
    assert call_count[0] == 1


def test_emit_heartbeat_throttled_fires_again_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate time advance by monkeypatching time.monotonic on the module."""
    _configured_env(monkeypatch)

    fake_clock = [1000.0]

    def fake_monotonic() -> float:
        return fake_clock[0]

    monkeypatch.setattr(heartbeat_module.time, "monotonic", fake_monotonic)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        return_value=_mock_201_response(),
    ) as urlopen_mock:
        assert emit_heartbeat_throttled("turn:1", throttle_key="k", min_interval_secs=60.0) is True

        # 30s later — still within interval, suppressed
        fake_clock[0] = 1030.0
        assert emit_heartbeat_throttled("turn:2", throttle_key="k", min_interval_secs=60.0) is False

        # 61s from first — interval elapsed, fires again
        fake_clock[0] = 1061.0
        assert emit_heartbeat_throttled("turn:3", throttle_key="k", min_interval_secs=60.0) is True

    assert urlopen_mock.call_count == 2


def test_emit_heartbeat_throttled_keys_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different throttle_keys share no state — a burst under key A does not
    block a first-time emit under key B."""
    _configured_env(monkeypatch)

    with patch(
        "claude_pilot.heartbeat.urllib.request.urlopen",
        return_value=_mock_201_response(),
    ) as urlopen_mock:
        assert emit_heartbeat_throttled("turn:1", throttle_key="A", min_interval_secs=60.0) is True
        assert emit_heartbeat_throttled("turn:2", throttle_key="A", min_interval_secs=60.0) is False
        assert (
            emit_heartbeat_throttled("recovery:1", throttle_key="B", min_interval_secs=60.0) is True
        )

    assert urlopen_mock.call_count == 2


def test_emit_heartbeat_throttled_respects_disabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CM_HEARTBEAT_DISABLED=1 must suppress the throttled path too."""
    monkeypatch.setenv("CM_HEARTBEAT_DISABLED", "1")
    monkeypatch.setenv("CM_TOKEN", "tok")

    with patch("claude_pilot.heartbeat.urllib.request.urlopen") as urlopen_mock:
        result = emit_heartbeat_throttled(
            "turn:1", throttle_key="pilot:turn", min_interval_secs=60.0
        )

    assert result is False
    urlopen_mock.assert_not_called()
