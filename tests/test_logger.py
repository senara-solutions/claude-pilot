"""Per-line log timestamp tests (cpp#168 volet 1).

Before this, a pilot log carried no timing information at all: two production
stalls (mika#2246, 2h18 and 58min of total silence) left logs that "stopped
dead" with no way to measure the interval between the last line and the
process's actual death. `write_log`/`write_file_log` now stamp every LINE
(not every call — `log_text` streams a turn's text in fragments with no
trailing newline) with a millisecond-precision UTC timestamp.
"""

from __future__ import annotations

import re

import pytest

from claude_pilot import logger

_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] ")


@pytest.fixture(autouse=True)
def _fresh_stampers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each stamper carries line-start state across calls — reset it per test
    so tests don't leak "mid-line" state into one another."""
    monkeypatch.setattr(logger, "_combined_stamper", logger._LineStamper())
    monkeypatch.setattr(logger, "_file_only_stamper", logger._LineStamper())


def test_single_line_write_log_is_stamped(capsys: pytest.CaptureFixture[str]) -> None:
    logger.write_log("[init] Session abc123, model claude-test\n")

    err = capsys.readouterr().err
    assert _TS_RE.match(err), f"expected a leading timestamp, got: {err!r}"
    assert "[init] Session abc123, model claude-test\n" in err


def test_each_line_of_a_multiline_message_is_stamped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`log_escalate`/`log_error` etc. can carry embedded newlines — every
    resulting line gets its own stamp, not just the first."""
    logger.write_log("first line\nsecond line\n")

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    for line in lines:
        assert _TS_RE.match(line), f"missing per-line stamp: {line!r}"
    assert lines[0].endswith("first line")
    assert lines[1].endswith("second line")


def test_streamed_fragments_without_trailing_newline_stamp_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cpp#123: `log_text` streams a turn's text as it arrives, one
    `write_log` call per fragment, with NO trailing newline until the turn's
    text block actually contains one. A fragment that continues the same line
    must NOT get a second stamp — that would visibly break up running prose
    with timestamps mid-sentence."""
    logger.write_log("Hello")
    logger.write_log(", world")
    logger.write_log("!\n")

    err = capsys.readouterr().err
    # Exactly one stamp for the whole accumulated line.
    assert err.count("] ") == 1 or err.startswith("[")
    assert _TS_RE.match(err)
    assert err.endswith("Hello, world!\n")


def test_write_log_and_write_file_log_stamp_independently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`write_file_log` (e.g. `log_prompt`) is a logically separate line
    stream from `write_log` — a mid-line `write_log` fragment must not affect
    whether the next `write_file_log` call gets its own stamp, or vice versa."""
    logger.write_log("streaming a fragment with no newline yet")
    logger.write_file_log("[prompt] do the thing\n")

    err = capsys.readouterr().err
    assert _TS_RE.match(err)
    # write_file_log went only to the file sink (no _file configured in this
    # test, so it silently no-ops) — stderr carries only the write_log call.
    assert "[prompt]" not in err


def test_timestamp_format_is_iso8601_millisecond_utc() -> None:
    ts = logger._timestamp()
    assert ts.endswith("Z")
    # Round-trips through the stdlib parser (Python 3.11+ accepts the
    # trailing "Z" as UTC).
    from datetime import datetime

    datetime.fromisoformat(ts.replace("Z", "+00:00"))
