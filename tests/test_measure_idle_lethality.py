"""Fixture tests for scripts/measure-idle-lethality.sh (cpp#145, AC6).

The script is the instrument that decides whether the fix worked. Its first
version scored a session from the FIRST `[guardrail]` line — but agent.py:277
logs a `[guardrail] rate_limited` for a TRANSIENT throttle and the session
keeps running, while the terminal abort (agent.py:209) is written last. So a
session that survived a passing throttle and later died of `idle_timeout` was
scored `rate_limited`, and the bias ran in the flattering direction: it
understated the exact rate the ticket wants to see fall.

Three reviewers independently observed that two synthetic logs would have
caught it. These are those logs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure-idle-lethality.sh"

# Verbatim shapes from /var/log/claude-pilot, ANSI stripped.
_TOOL_RESULT = "[debug] user message (tool result) received"
_TRANSIENT_THROTTLE = "[guardrail] rate_limited: Anthropic rate limit rejected (429)"
_TERMINAL_IDLE = (
    "[guardrail] idle_timeout: No meaningful progress for 300s "
    "(1722 content stream events this session)"
)
_TERMINAL_CEILING = (
    "[guardrail] rate_limited: Rate-limited beyond ceiling: throttled ~1800s "
    "(ceiling 1800s) with no progress"
)
_DONE = "[done] Success"


def _write(log_dir: Path, name: str, lines: list[str]) -> None:
    (log_dir / f"{name}.stderr").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(log_dir: Path) -> str:
    if shutil.which("bash") is None:  # pragma: no cover - bash is a hard dep here
        pytest.skip("bash unavailable")
    proc = subprocess.run(
        ["bash", str(_SCRIPT), str(log_dir), "3650"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _rate(stdout: str, label: str) -> tuple[int, int]:
    """Parse `label:  <killed>/<terminated>  (pct)` out of the report."""
    match = re.search(rf"^{re.escape(label)}:\s+(\d+)/(\d+)", stdout, re.MULTILINE)
    assert match, f"{label} row missing from:\n{stdout}"
    return int(match.group(1)), int(match.group(2))


def test_transient_throttle_before_an_idle_death_is_not_the_cause_of_death(
    tmp_path: Path,
) -> None:
    """The regression this file exists for. A session that logged a passing
    throttle and then died of idle_timeout must be scored `idle_timeout`.

    Under the original `grep -m1` it scored `rate_limited`, quietly removing a
    kill from the numerator of the rate the fix is judged by.
    """
    _write(
        tmp_path,
        "sess-throttled-then-idle",
        [_TRANSIENT_THROTTLE, _TOOL_RESULT, _TOOL_RESULT, _TERMINAL_IDLE],
    )

    out = _run(tmp_path)

    assert _rate(out, "idle_timeout") == (1, 1)
    assert _rate(out, "rate_limited") == (0, 1)


def test_a_live_session_carrying_only_a_transient_throttle_is_not_terminated(
    tmp_path: Path,
) -> None:
    """A still-running session must stay out of BOTH terms. Counting it as
    terminated would depress the rate for a reason unrelated to the fix — the
    raw-count failure mode AC6 refuses."""
    _write(tmp_path, "sess-still-running", [_TRANSIENT_THROTTLE, _TOOL_RESULT])
    _write(tmp_path, "sess-done", [_TOOL_RESULT, _DONE])

    out = _run(tmp_path)

    assert _rate(out, "idle_timeout") == (0, 1), "only the [done] session counts"


def test_a_ceiling_abort_is_terminal_and_named_rate_limited(tmp_path: Path) -> None:
    """The other half of the control: `rate_limited` IS terminal when it names
    the ceiling. Without this the previous test's rule would silently discard
    every genuine cpp#133 termination."""
    _write(tmp_path, "sess-ceiling", [_TOOL_RESULT, _TERMINAL_CEILING])

    out = _run(tmp_path)

    assert _rate(out, "rate_limited") == (1, 1)
    assert _rate(out, "idle_timeout") == (0, 1)


def test_the_new_wait_reasons_are_counted_in_their_own_rows(tmp_path: Path) -> None:
    """`awaiting_tool` / `awaiting_model` are terminal and must not fall into
    the idle_timeout row — the whole point of AC6 is telling the deaths apart
    across the change boundary."""
    _write(
        tmp_path,
        "sess-await-model",
        [_TOOL_RESULT, "[guardrail] awaiting_model: Model wait exceeded ceiling"],
    )
    _write(
        tmp_path,
        "sess-await-tool",
        [_TOOL_RESULT, "[guardrail] awaiting_tool: Tool wait exceeded ceiling"],
    )
    _write(tmp_path, "sess-idle", [_TERMINAL_IDLE])

    out = _run(tmp_path)

    assert _rate(out, "awaiting_model") == (1, 3)
    assert _rate(out, "awaiting_tool") == (1, 3)
    assert _rate(out, "idle_timeout") == (1, 3)


def test_a_window_with_no_terminated_session_fails_loudly(tmp_path: Path) -> None:
    """An undefined rate must not render as zero. `0%` would read as "the fix
    worked perfectly" when the truth is "nothing was measured"."""
    _write(tmp_path, "sess-still-running", [_TOOL_RESULT])

    proc = subprocess.run(
        ["bash", str(_SCRIPT), str(tmp_path), "3650"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "undefined" in proc.stderr
