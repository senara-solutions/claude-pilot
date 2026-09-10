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
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_file: IO[str] | None = None
_error_reported = False


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
    sys.stderr.write(stamped)
    sys.stderr.flush()
    _write_file(stamped)


def write_file_log(msg: str) -> None:
    """Write only to the file sink (skip stderr).

    cpp#168: stamped with its OWN line-start tracker — this is a logically
    separate line sequence from `write_log`'s (e.g. `log_prompt`), and sharing
    a tracker would let one stream's mid-line state corrupt the other's.
    """
    _write_file(_file_only_stamper.stamp(msg))


def close_file_log() -> None:
    global _file
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
    try:
        _file.write(_ANSI_RE.sub("", msg))
        _file.flush()
    except OSError as err:
        _report_error(err)
        _file = None


def _report_error(err: OSError) -> None:
    global _error_reported
    if not _error_reported:
        _error_reported = True
        sys.stderr.write(f"Warning: log file write error: {err}\n")
