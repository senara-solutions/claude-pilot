"""cm heartbeat writer (cpp#111, D8 subsystem 2 client-side).

Posts a lightweight "I'm alive" event to the control-monitor heartbeat
endpoint at natural lifecycle transitions in claude-pilot. Feeds cm's agent
freshness monitor (cm#111) so the nudge scanner can alert Sami when a pilot
goes red.

The writer is event-driven — never a background timer. Callers invoke it at
observed state changes (session start, per-turn completion, tool-recovery,
session end). It is fire-and-forget by design: durability at the substrate
layer is cm's job (per ADR D8 subsystem 3 outbox), not the client's.

Gated by env vars, all read at call time (a mid-session flip takes effect on
the next event):

- ``CM_HEARTBEAT_DISABLED`` — case-insensitive ``1`` / ``true`` / ``yes``
  disables all heartbeat calls entirely (for debug / isolated runs). Anything
  else — including ``0``, ``false``, empty, unset — leaves the writer active.
- ``CM_HEARTBEAT_ENTITY`` — entity id in the URL path. Defaults to ``pilot``.
- ``CM_API_URL`` — base URL of the control-monitor api. Defaults to
  ``http://127.0.0.1:8090``. Trailing slashes are normalised away.
- ``CM_TOKEN`` — bearer token shared with cm-api. Absent token means the
  substrate is not fully configured on this host; skip silently rather than
  fire an unauthenticated POST that cm will 401 anyway.

Failures are logged at debug/warn on stderr and NEVER change the caller's
control flow. The pilot MUST run unaffected whether cm is up, slow, or gone.

Bounded latency: :data:`HEARTBEAT_TIMEOUT_SECS` caps every POST at 500ms so
a slow cm-api never stalls a natural transition in the pilot loop. Uses
``urllib.request`` from stdlib — no new HTTP client dependency.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Design constraint from cpp#111: max 500ms timeout on POST, drop on timeout
# with warn log. Tight cap keeps the event-driven POST cheap enough that a
# synchronous call from an async loop doesn't stall a natural transition.
HEARTBEAT_TIMEOUT_SECS: float = 0.5

DEFAULT_ENTITY: str = "pilot"
DEFAULT_CM_API_URL: str = "http://127.0.0.1:8090"

# Module-level throttle-key -> last-emit monotonic timestamp. Used by
# :func:`emit_heartbeat_throttled` to cap per-turn heartbeat frequency
# (design constraint: 1/min for turn completions).
_last_emit: dict[str, float] = {}


def is_heartbeat_disabled() -> bool:
    """Return True when ``CM_HEARTBEAT_DISABLED`` is a truthy string.

    Recognises ``1``, ``true``, ``yes`` (case-insensitive, whitespace
    trimmed). Anything else — empty, unset, ``0``, ``false`` — leaves the
    writer active.
    """
    raw = os.environ.get("CM_HEARTBEAT_DISABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


def emit_heartbeat(
    reason: str,
    *,
    entity: str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Fire-and-forget POST to the cm heartbeat endpoint.

    Returns ``True`` on a 201 from the server, ``False`` on any skip / failure.
    Never raises.

    ``reason`` is a short structured tag describing the transition (e.g.
    ``session:mika#1878``, ``turn:complete``, ``recovery:tool``,
    ``complete:mika#1878``). Passed straight through as the request body's
    ``reason`` field.

    ``entity`` overrides ``CM_HEARTBEAT_ENTITY``; when both are absent the
    writer falls back to :data:`DEFAULT_ENTITY` (``pilot``). The value is
    URL-path-inserted, so callers should keep it to the same URL-safe shape
    the endpoint expects.

    ``meta`` is an optional dict merged into the request body's ``meta``
    field. Kept small — the server is under no obligation to persist it and
    payload bloat only hurts the timeout budget.
    """
    if is_heartbeat_disabled():
        return False

    entity_value = entity or os.environ.get("CM_HEARTBEAT_ENTITY", "").strip() or DEFAULT_ENTITY
    cm_api_url = os.environ.get("CM_API_URL", "").strip() or DEFAULT_CM_API_URL
    token = os.environ.get("CM_TOKEN", "").strip()

    if not token:
        # Substrate not fully configured on this host. Silent skip — an
        # unauthenticated POST would only earn a 401. No warn to avoid
        # noise on dev machines where cm is intentionally not running.
        return False

    payload: dict[str, Any] = {"reason": reason}
    if meta:
        payload["meta"] = meta
    data = json.dumps(payload).encode("utf-8")

    url = _build_url(cm_api_url, entity_value)
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT_SECS) as resp:
            return bool(resp.status == 201)
    except urllib.error.HTTPError as e:
        _warn(f"cm heartbeat POST to {url} got HTTP {e.code}: {e.reason}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _warn(f"cm heartbeat POST to {url} failed: {e}")
        return False


def emit_heartbeat_throttled(
    reason: str,
    *,
    throttle_key: str,
    min_interval_secs: float,
    entity: str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Rate-limited wrapper around :func:`emit_heartbeat`.

    Skips the POST when the last emit under the same ``throttle_key`` fired
    less than ``min_interval_secs`` ago. Used for per-turn heartbeats — the
    stream can produce many turns per second under a tool-heavy pipeline, and
    every one of them firing a POST would flood cm-api pointlessly. Cap at
    1/min per the ticket's design note.

    Returns ``True`` when the underlying POST fired AND returned 201.
    Returns ``False`` on either the rate-limit skip or a downstream failure.
    Uses ``time.monotonic()`` — safe across wall-clock jumps.
    """
    now = time.monotonic()
    last = _last_emit.get(throttle_key)
    if last is not None and (now - last) < min_interval_secs:
        return False
    # Record the timestamp BEFORE the POST so a slow cm-api can't allow a
    # concurrent second call through the rate-limit gate.
    _last_emit[throttle_key] = now
    return emit_heartbeat(reason, entity=entity, meta=meta)


def reset_throttle_state() -> None:
    """Clear the per-key rate-limit state.

    Test hook — callers wire this into fixtures that share module state
    across cases. Production code has no reason to call it.
    """
    _last_emit.clear()


def _build_url(cm_api_url: str, entity: str) -> str:
    """Join the cm-api base URL with the heartbeat path, stripping trailing /."""
    base = cm_api_url.rstrip("/")
    return f"{base}/api/v1/agents/{entity}/heartbeat"


def _warn(msg: str) -> None:
    """Best-effort stderr write; never raises."""
    try:
        sys.stderr.write(f"[claude-pilot heartbeat] {msg}\n")
        sys.stderr.flush()
    except OSError:
        pass
