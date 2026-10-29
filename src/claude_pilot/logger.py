"""File + stderr logging with ANSI stripping for the file sink.

Port of src/logger.ts. Single module-level file handle; first write failure
reports one warning and disables further file writes (best-effort sink, never
crashes the session).

cpp#168: every line is prefixed with a UTC timestamp. Before this, a pilot log
carried no timing information at all -- two production stalls (mika#2246,
2h18 and 58min of total silence) left logs that "stopped dead" with no way to
measure the interval between the last line and the process's actual death, or
to correlate lines against `idleTimeoutMs`/`toolWaitCeilingMs`/
`modelWaitCeilingMs`. This is the diagnostic prerequisite the rest of cpp#168
depends on. Stamping is per LINE, not per `write_log`/`write_file_log` call:
`log_text` streams a turn's text in fragments with no trailing newline
(cpp#123 partial-message streaming), so a naive per-call stamp would either
mid-line-stamp a fragment or miss the line boundaries entirely. `_LineStamper`
tracks "am I at the start of a line" ACROSS calls and stamps only there.

cpp#171: the writes leave the event-loop thread. A dispatched pilot runs with
`--verbose`, and 96% of its lines are `[debug] stream event` traces, up to
~5,600 lines in one second; each line was a write + flush on stderr (itself a
file, redirected by dispatch-lib) and another on the file sink, synchronously
on the loop thread that also runs `_idle_watchdog`. A slow sink could starve
the watchdog by the same mechanism as cpp#168. `start_background_writer()`
moves both sinks to one writer thread fed by a bounded queue: stamping stays
synchronous (pure computation, order fixed at the call), I/O does not. Off by
default, so a caller that never starts it (tests, library use) keeps the
synchronous behaviour byte for byte.
"""

from __future__ import annotations

import atexit
import os
import queue
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_file: IO[str] | None = None
_error_reported = False

# cpp#171: background writer state. An item is (stderr_text, file_text), either
# may be None; a threading.Event is a flush marker the writer sets once every
# item queued before it has been written.
_QUEUE_MAX_ITEMS = 100_000
_BATCH_MAX_ITEMS = 1_000
_FLUSH_TIMEOUT_S = 2.0
_queue: queue.Queue[tuple[str | None, str | None] | threading.Event] | None = None
_dropped = 0
_dropped_lock = threading.Lock()


def _timestamp() -> str:
    """Millisecond-precision UTC timestamp for a log line (cpp#168)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _LineStamper:
    """Prefixes each LINE of a stream of `write_log`/`write_file_log` calls
    with a timestamp, tracking line-start state across calls (cpp#168).

    `str.splitlines(keepends=True)` splits a chunk into segments that each
    carry their own trailing newline (or none, for a still-open final
    fragment) — exactly the granularity needed to stamp only true line
    starts and to carry "was the previous call mid-line" state forward.
    """

    __slots__ = ("_at_line_start",)

    def __init__(self) -> None:
        self._at_line_start = True

    def stamp(self, msg: str) -> str:
        if not msg:
            return msg
        out: list[str] = []
        for segment in msg.splitlines(keepends=True):
            if self._at_line_start:
                out.append(f"[{_timestamp()}] ")
            out.append(segment)
            self._at_line_start = segment.endswith("\n")
        return "".join(out)


# Two independent line-streams: `write_log` fans one message out to BOTH
# stderr and the file sink (same content, so one stamp is reused for both);
# `write_file_log` (e.g. `log_prompt`) writes file-only lines that are a
# logically separate sequence and must not perturb `write_log`'s line-start
# tracking, or vice versa.
_combined_stamper = _LineStamper()
_file_only_stamper = _LineStamper()


def init_file_log(file_path: str) -> None:
    global _file, _error_reported
    _error_reported = False
    path = Path(file_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _file = path.open("a", encoding="utf-8")
        # Best-effort chmod; ignore if filesystem doesn't support it.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as err:
        _report_error(err)
        _file = None


def write_log(msg: str) -> None:
    """Write to stderr (with color) and to the file sink (without color).

    cpp#168: stamped once, and the SAME stamped text reaches both sinks — they
    describe the same event at the same instant, so they must carry the same
    timestamp rather than being stamped independently a few instructions
    apart.
    """
    stamped = _combined_stamper.stamp(msg)
    if _queue is not None:
        _enqueue((stamped, stamped))
        return
    sys.stderr.write(stamped)
    sys.stderr.flush()
    _write_file(stamped)


def write_file_log(msg: str) -> None:
    """Write only to the file sink (skip stderr).

    cpp#168: stamped with its OWN line-start tracker — this is a logically
    separate line sequence from `write_log`'s (e.g. `log_prompt`), and sharing
    a tracker would let one stream's mid-line state corrupt the other's.
    """
    stamped = _file_only_stamper.stamp(msg)
    if _queue is not None:
        _enqueue((None, stamped))
        return
    _write_file(stamped)


def start_background_writer() -> None:
    """cpp#171: route both sinks through one writer thread. Idempotent."""
    global _queue
    if _queue is not None:
        return
    _queue = queue.Queue(maxsize=_QUEUE_MAX_ITEMS)
    threading.Thread(target=_writer_loop, args=(_queue,), name="pilot-log", daemon=True).start()
    atexit.register(flush_log)


def flush_log(timeout: float = _FLUSH_TIMEOUT_S) -> bool:
    """Wait until every line queued so far is written. True if it was, within
    `timeout`; False if the sink is too slow (the lines stay queued). A no-op
    returning True when the background writer is off."""
    q = _queue
    if q is None:
        return True
    marker = threading.Event()
    try:
        q.put(marker, timeout=timeout)
    except queue.Full:
        return False
    return marker.wait(timeout)


def _enqueue(item: tuple[str | None, str | None]) -> None:
    """Never blocks the caller: a full queue (sink stalled for ~100k lines)
    drops the line and counts it; the writer reports the count once it can."""
    global _dropped
    assert _queue is not None
    try:
        _queue.put_nowait(item)
    except queue.Full:
        with _dropped_lock:
            _dropped += 1


def _take_dropped() -> int:
    global _dropped
    with _dropped_lock:
        n, _dropped = _dropped, 0
    return n


def _writer_loop(
    q: queue.Queue[tuple[str | None, str | None] | threading.Event],
) -> None:
    """Drain in batches: one flush per batch rather than per line."""
    while True:
        batch = [q.get()]
        while len(batch) < _BATCH_MAX_ITEMS:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        err_parts: list[str] = []
        file_parts: list[str] = []
        markers: list[threading.Event] = []
        dropped = _take_dropped()
        if dropped:
            note = f"[{_timestamp()}] [logger] dropped {dropped} lines: sink too slow\n"
            err_parts.append(note)
            file_parts.append(note)
        for item in batch:
            if isinstance(item, threading.Event):
                markers.append(item)
                continue
            err_text, file_text = item
            if err_text is not None:
                err_parts.append(err_text)
            if file_text is not None:
                file_parts.append(file_text)
        if err_parts:
            try:
                sys.stderr.write("".join(err_parts))
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
        if file_parts:
            _write_file("".join(file_parts))
        for marker in markers:
            marker.set()


def close_file_log() -> None:
    global _file
    flush_log()
    if _file is not None:
        try:
            _file.close()
        except OSError:
            pass
        _file = None


def _write_file(msg: str) -> None:
    global _file
    if _file is None:
        return
    sink = _file
    try:
        sink.write(_ANSI_RE.sub("", msg))
        sink.flush()
    except OSError as err:
        _report_error(err)
        _file = None
    except ValueError:
        # cpp#171: closed under the writer by a `close_file_log` whose flush
        # timed out on a stalled sink. Nothing left to write to.
        _file = None


def _report_error(err: OSError) -> None:
    global _error_reported
    if not _error_reported:
        _error_reported = True
        sys.stderr.write(f"Warning: log file write error: {err}\n")
