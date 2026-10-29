"""Log I/O off the event-loop thread — cpp#171.

A dispatched pilot writes up to ~5,600 lines in one second, each a write +
flush on stderr and on the file sink. Synchronous, those writes ran on the
thread that also runs `_idle_watchdog`; a slow sink could starve it (the
cpp#168 mechanism). These tests stall the sink on purpose and check that the
event loop does not notice, that nothing is lost or reordered, and that the
failure rules of the synchronous sink still hold.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from claude_pilot import logger


class _SlowStream:
    """A stderr whose every write takes `delay` seconds, recording what it got."""

    def __init__(self, delay: float, gate: threading.Event | None = None) -> None:
        self.delay = delay
        self.gate = gate
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.delay)
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "".join(self.chunks)


@pytest.fixture(autouse=True)
def _fresh_logger(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Each test gets its own queue and writer thread, and no file sink. A
    previous test's writer thread stays parked on its own queue (daemon)."""
    monkeypatch.setattr(logger, "_queue", None)
    monkeypatch.setattr(logger, "_file", None)
    monkeypatch.setattr(logger, "_dropped", 0)
    monkeypatch.setattr(logger, "_error_reported", False)
    monkeypatch.setattr(logger, "_combined_stamper", logger._LineStamper())
    monkeypatch.setattr(logger, "_file_only_stamper", logger._LineStamper())
    yield
    logger.flush_log(timeout=5)


def test_writes_stay_synchronous_until_the_writer_is_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control, and the reason the writer is opt-in: without it a
    slow sink blocks the caller for the full write."""
    slow = _SlowStream(delay=0.2)
    monkeypatch.setattr("sys.stderr", slow)

    started = time.monotonic()
    logger.write_log("line\n")

    assert time.monotonic() - started >= 0.2
    assert logger.flush_log() is True


@pytest.mark.asyncio
async def test_a_stalled_sink_does_not_stall_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slow = _SlowStream(delay=0.5)
    monkeypatch.setattr("sys.stderr", slow)
    logger.start_background_writer()

    started = time.monotonic()
    for i in range(50):
        logger.write_log(f"line {i}\n")
    write_cost = time.monotonic() - started

    tick = time.monotonic()
    await asyncio.sleep(0.01)
    loop_latency = time.monotonic() - tick

    assert write_cost < 0.05
    assert loop_latency < 0.05
    assert logger.flush_log(timeout=5) is True
    assert "line 49" in slow.text


def test_every_line_reaches_the_file_in_order(tmp_path: Path) -> None:
    log = tmp_path / "pilot.log"
    logger.start_background_writer()
    logger.init_file_log(str(log))

    for i in range(2_500):
        logger.write_log(f"\x1b[2mline {i}\x1b[0m\n")
    logger.write_file_log("file only\n")
    logger.close_file_log()

    lines = log.read_text().splitlines()
    assert [line.split("] ", 1)[1] for line in lines[:-1]] == [f"line {i}" for i in range(2_500)]
    assert lines[-1].endswith("] file only")


def test_a_failing_file_sink_warns_once_and_is_cut(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Broken:
        def write(self, _text: str) -> int:
            raise OSError("No space left on device")

        def flush(self) -> None:
            return None

    logger.start_background_writer()
    monkeypatch.setattr(logger, "_file", _Broken())

    logger.write_log("one\n")
    logger.write_log("two\n")
    assert logger.flush_log() is True

    err = capsys.readouterr().err
    assert err.count("log file write error") == 1
    assert logger._file is None
    assert "two" in err


def test_a_full_queue_drops_without_blocking_and_reports_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    slow = _SlowStream(delay=0.0, gate=gate)
    monkeypatch.setattr("sys.stderr", slow)
    monkeypatch.setattr(logger, "_QUEUE_MAX_ITEMS", 3)
    logger.start_background_writer()

    started = time.monotonic()
    for i in range(20):
        logger.write_log(f"line {i}\n")
    assert time.monotonic() - started < 0.05

    gate.set()
    assert logger.flush_log(timeout=5) is True
    logger.write_log("after\n")
    assert logger.flush_log(timeout=5) is True

    assert "[logger] dropped" in slow.text
    assert "lines: sink too slow" in slow.text
    assert "after" in slow.text


def test_flush_reports_a_sink_that_does_not_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    monkeypatch.setattr("sys.stderr", _SlowStream(delay=0.0, gate=gate))
    logger.start_background_writer()

    logger.write_log("stuck\n")

    assert logger.flush_log(timeout=0.1) is False
    gate.set()
    assert logger.flush_log(timeout=5) is True
