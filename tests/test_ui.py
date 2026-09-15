"""UI log-renderer tests.

cpp#185 D1: `log_cache_usage` emits the per-response prompt-cache line
(`cache_read_input_tokens` / `cache_creation_input_tokens`, tagged with the
turn number) that agent.py calls for every closed turn — the observability
half of the `prompt_cache_dead` guardrail. See guardrails.py for the
detector; this file only pins the renderer's output shape.
"""

from __future__ import annotations

import pytest

from claude_pilot import logger, ui


@pytest.fixture(autouse=True)
def _fresh_stampers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each `_LineStamper` carries line-start state across calls — reset it
    per test so tests don't leak "mid-line" state into one another (mirrors
    tests/test_logger.py's fixture of the same name)."""
    monkeypatch.setattr(logger, "_combined_stamper", logger._LineStamper())
    monkeypatch.setattr(logger, "_file_only_stamper", logger._LineStamper())


def test_log_cache_usage_emits_a_stable_grepable_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line names the turn and both raw usage fields verbatim, under a
    `[cache]` tag stable enough for `grep '\\[cache\\]'` against
    `/var/log/claude-pilot/*.stderr`."""
    ui.log_cache_usage(7, 0, 142_000)

    err = capsys.readouterr().err
    assert "[cache]" in err
    assert "turn 7" in err
    assert "cache_read_input_tokens=0" in err
    assert "cache_creation_input_tokens=142000" in err


def test_log_cache_usage_renders_missing_data_as_unknown_not_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`None` (no usage observed for the turn — older SDK, malformed payload)
    must render as `?`, never as `0` — a genuine cache-read miss and an
    absent reading must stay visually distinct in the log."""
    ui.log_cache_usage(3, None, None)

    err = capsys.readouterr().err
    assert "cache_read_input_tokens=?" in err
    assert "cache_creation_input_tokens=?" in err


def test_log_guardrail_config_always_names_the_cache_dead_guardrail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unlike the configurable guardrails above it, `prompt_cache_dead` has no
    on/off switch — the config header names it unconditionally so an operator
    reading the log tail sees that it is armed."""
    from claude_pilot.types import GUARDRAIL_DEFAULTS

    ui.log_guardrail_config(GUARDRAIL_DEFAULTS)

    err = capsys.readouterr().err
    assert "promptCacheDead=3x>50000tok" in err
