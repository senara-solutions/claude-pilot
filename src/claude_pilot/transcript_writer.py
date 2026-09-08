"""Pilot-transcript JSONL writer (claude-pilot#165, the writer half of mika#2040).

One JSONL line per complete SDK message, appended to ``$ANTHROPIC_LOG_FILE``.
mika owns the other two thirds of this contract: ``skills/executor.rs`` injects
the env var and mounts the directory (mika#1705), and
``crates/mika-agent/src/task_engine/pilot_transcript.rs`` reads the lines back
(mika#2040 / PR#2240). That reader is the **sole source of truth for the line
format**; this module mirrors it and must be updated with it.

Why it exists: mika#1705 shipped the producer and the ingestion and never
shipped the writer. For 25+ days the directory stayed empty and nothing said
so, because "no file" is indistinguishable from "no session".

Posture is `inbox_writer`'s (mika#1189): a side channel that **fails open**. A
transcript is observability, not the product — an unwritable path must never
cost a dispatch. Every failure is swallowed, logged once, and disarms the
writer for the rest of the session.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import AssistantMessage, ResultMessage

from .ui import log_error

# The one schema version mika ingests. Bumping it is a CROSS-REPO change: a
# mika that only accepts "v2" refuses every line a "v1" claude-pilot writes,
# and the refusal is loud but total. Ship the reader accepting both versions
# for one release before changing this constant.
PILOT_TRANSCRIPT_SCHEMA_VERSION = "v1"

# Only these two carry model output. `StreamEvent` is the partial-message
# stream (`include_partial_messages=True` in agent.py) — recording it as well
# would write the same tokens twice.
_RECORDED_MESSAGES = (AssistantMessage, ResultMessage)

# Set after the first I/O failure. A missing mount would otherwise emit one
# error line per SDK message and drown the session's stderr.
_disarmed = False

# Same throttle for the non-fatal skips, which do NOT disarm the writer.
_warned_serialization = False


def _warn_once(flag: str, detail: str) -> None:
    """Log ``detail`` the first time ``flag`` fires, then stay quiet.

    A silent skip is the exact sin this ticket exists to fix, so the first one
    is always reported; the rest are suppressed because a session can carry
    thousands of messages.
    """
    if globals()[flag]:
        return
    globals()[flag] = True
    log_error("pilot-transcript", [detail])


def transcript_line(message: AssistantMessage | ResultMessage) -> dict[str, Any]:
    """Map one SDK message onto the v1 line the mika reader ingests.

    Pure — no I/O, no env, no failure mode. Every field but ``schema_version``
    is optional on the reader's side and maps to a nullable column.

    ``request_body`` stays absent by design: the outbound HTTP body is not
    exposed at the SDK consumption point, and the env var's name
    (``ANTHROPIC_LOG_FILE``, which evokes a raw HTTP log) is the only thing
    that suggests otherwise. Response, usage, model and latency are reliable.
    """
    usage = getattr(message, "usage", None) or {}
    return {
        "schema_version": PILOT_TRANSCRIPT_SCHEMA_VERSION,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provider": "anthropic",
        # `ResultMessage` has no `model` field at all; `AssistantMessage` does.
        "model": getattr(message, "model", None),
        "response_body": dataclasses.asdict(message),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
        # The session's API latency, present on the result message only.
        "latency_ms": getattr(message, "duration_api_ms", None),
    }


def record_sdk_message(message: object) -> bool:
    """Append one transcript line for ``message``. Returns whether it wrote.

    Never raises. A ``False`` return means "nothing to record here" (wrong
    message type, no ``$ANTHROPIC_LOG_FILE``) or "the write failed and the
    writer is now disarmed" — the caller is an SDK message loop and has no
    useful way to tell those apart, nor any business reacting to either.
    """
    global _disarmed

    if _disarmed or not isinstance(message, _RECORDED_MESSAGES):
        return False

    path = (os.environ.get("ANTHROPIC_LOG_FILE") or "").strip()
    if not path:
        return False

    # Serialised BEFORE the file is touched, and its failure is handled
    # SEPARATELY: an exotic value `asdict`/`json.dumps` cannot render is a
    # property of ONE message, while an unwritable path is a property of the
    # whole session. Sharing one handler would let a single odd tool input
    # disarm the writer for every message that followed it.
    try:
        # `default=str` is load-bearing: `asdict` recurses through the
        # dataclasses but stops at `Any` (`ToolUseBlock.input`,
        # `ResultMessage.structured_output`).
        line = json.dumps(transcript_line(message), default=str)
    except Exception as exc:
        _warn_once("_warned_serialization", f"skipped one message: {exc}")
        return False

    try:
        # The producer mounts the directory, but creating it here removes the
        # cheapest whole-session failure class for one syscall.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Opened per line on purpose: no long-lived handle to survive a crash,
        # and a file the producer creates late is picked up on the next write.
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    # Fail open: the path is unusable for the rest of the session, so stop
    # trying. A transcript is observability, never the product.
    except OSError as exc:
        _disarmed = True
        log_error("pilot-transcript", [f"disabled after write failure on {path}: {exc}"])
        return False

    return True
