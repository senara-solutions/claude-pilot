"""The wait ceilings are derived, and the derivation is held — cpp#146.

`toolWaitCeilingMs` is not borrowed from `rateLimitCeilingMs`: it sits above the
Bash tool's raised max timeout (`BASH_MAX_TIMEOUT_MS`, cpp#267 follow-up), so a
command allowed to run to that cap is never killed by the pilot first. Measured
on 2026-10-29 over the timestamped logs since 2026-09-10, the longest silent tool
wait was 1680 s — a `gh pr checks --watch` with `timeout: 1680000`, i.e. the cap
itself.

The existing assertion in `test_agent.py` compares the cap with a literal 1800 s.
This one compares the two constants, so lowering either alone goes red.
"""

from __future__ import annotations

from claude_pilot import agent as agent_module
from claude_pilot.types import GUARDRAIL_DEFAULTS, ResolvedGuardrailConfig


def _bash_max_timeout_ms() -> int:
    return int(agent_module._CLI_FORCE_FOREGROUND_ENV["BASH_MAX_TIMEOUT_MS"])


def test_the_tool_ceiling_stays_above_the_bash_cap() -> None:
    assert _bash_max_timeout_ms() < GUARDRAIL_DEFAULTS.toolWaitCeilingMs


def test_the_resolved_default_agrees_with_the_published_one() -> None:
    """Two places carry the default; the coupling must hold for both."""
    resolved = ResolvedGuardrailConfig.model_fields["toolWaitCeilingMs"].default
    assert resolved == GUARDRAIL_DEFAULTS.toolWaitCeilingMs
    assert _bash_max_timeout_ms() < resolved


def test_the_model_ceiling_stays_below_the_tool_ceiling() -> None:
    """A model wait is bounded tighter than a tool wait: nothing the model does
    between turns is a 28-minute build. Measured longest silent model wait:
    512 s, against a 900 s ceiling."""
    assert GUARDRAIL_DEFAULTS.modelWaitCeilingMs < GUARDRAIL_DEFAULTS.toolWaitCeilingMs
