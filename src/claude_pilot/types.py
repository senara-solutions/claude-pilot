"""Configuration, event, and result schemas.

Port of src/types.ts (zod → pydantic v2). Field names match the TS wire format
exactly so downstream consumers (mika-skills/claude-pilot/handlers/run.sh,
mika-dev relay) keep parsing unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GuardrailConfig(BaseModel):
    """Application-level guardrails. Fields are optional; defaults from GUARDRAIL_DEFAULTS."""

    model_config = ConfigDict(extra="forbid")

    maxTurns: int | None = Field(default=None, ge=1)
    maxBudgetUsd: float | None = Field(default=None, ge=0.01)
    stallThreshold: int | None = Field(default=None, ge=0)
    emptyResponseThreshold: int | None = Field(default=None, ge=0)
    idleTimeoutMs: int | None = Field(default=None, ge=0, le=3_600_000)
    minTurnsBeforeDetection: int | None = Field(default=None, ge=0)
    # cpp#133: how long the idle watchdog may keep a session alive while a
    # rate-limit signal (cpp#119) is armed, before it finally terminates it as
    # `rate_limited`. Under throttling the bundled SDK retries with its own
    # backoff and produces nothing on the wire; the idle window would fire
    # between retries and kill a session that is only waiting on quota. While
    # the flag is armed the watchdog defers to that backoff up to this bound
    # instead of aborting at `idleTimeoutMs`. 0 = no pilot ceiling (defer to the
    # SDK indefinitely). Bounded above so a permanently-throttled loop cannot
    # keep a zombie session alive forever (maxTurns does not bound a waiting
    # pilot — it burns no turn while it waits).
    rateLimitCeilingMs: int | None = Field(default=None, ge=0, le=21_600_000)


class ResolvedGuardrailConfig(BaseModel):
    """All-fields-present variant used internally after defaults are applied."""

    model_config = ConfigDict(extra="forbid")

    maxTurns: int
    maxBudgetUsd: float
    stallThreshold: int
    emptyResponseThreshold: int
    idleTimeoutMs: int
    minTurnsBeforeDetection: int
    # cpp#133: bound on the throttled-backoff wait. Defaulted so existing
    # all-fields constructors (and downstream callers) keep working unchanged.
    rateLimitCeilingMs: int = 1_800_000


GUARDRAIL_DEFAULTS = ResolvedGuardrailConfig(
    maxTurns=200,
    maxBudgetUsd=0.0,  # 0 = disabled
    stallThreshold=5,
    emptyResponseThreshold=5,
    idleTimeoutMs=300_000,
    minTurnsBeforeDetection=10,
    # cpp#133: 30 min comfortably outlasts the SDK's own ~5 min backoff (the
    # 2026-08-06 founding incident) and several retry cycles, while bounding a
    # session that would otherwise wait forever under continuous throttling.
    rateLimitCeilingMs=1_800_000,
)


class PilotConfig(BaseModel):
    """Relay configuration loaded from .claude/claude-pilot.json."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] | None = None
    timeout: int | None = Field(default=None, ge=1000, le=600_000)
    model: str | None = Field(default=None, min_length=1)
    guardrails: GuardrailConfig | None = None


class PilotEvent(BaseModel):
    """Event payload sent to the external relay agent via stdin."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["permission", "question"]
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    agent_id: str | None = None
    decision_reason: str | None = None
    blocked_path: str | None = None
    # cpp#56: additive ToolPermissionContext enrichment (SDK 0.2.x; these fields
    # landed upstream in v0.1.74). Optional so older relay payloads and SDK
    # minors lacking these fields stay valid; serialized absent via exclude_none.
    title: str | None = None
    display_name: str | None = None
    description: str | None = None
    error: str | None = None


class PilotResponseAllow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["allow"]


class PilotResponseDeny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["deny"]
    message: str | None = None


class PilotResponseAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["answer"]
    answers: dict[str, str]


PilotResponse = PilotResponseAllow | PilotResponseDeny | PilotResponseAnswer


class ResultJson(BaseModel):
    """Single-line JSON written to stdout on completion. Parsed by
    mika-skills/claude-pilot/handlers/run.sh.

    Subtype values:
        - "success" — SDK ResultMessage reported success.
        - "early_exit_zero_action" — fewer than CLAUDE_PILOT_MIN_TOOL_CALLS
          tool calls observed; session re-prompted or terminated.
        - "pipeline_incomplete" (mika#940) — CLAUDE_PILOT_REQUIRE_PR=1 set
          (dev-pilot sessions via dispatch-lib) and the session completed
          successfully but never invoked `gh pr create`. Indicates the
          premature-EndTurn family — model emits `[done] Success` after
          Edit/Compound phases without reaching git push + gh pr create.
          Work may be stranded in the worktree.
        - SDK termination subtypes (e.g. "error_max_turns", "error_during_execution")
          — see SDK_TERMINATION_SUBTYPES in agent.py.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error", "terminated"]
    subtype: str
    task_id: str | None = None
    session_id: str | None = None
    turns: int
    # Unknown when the session terminated before a ResultMessage arrived (e.g.
    # guardrail trip, fatal CLI error). Serialized as absent field via
    # `exclude_none` so downstream handlers parse it as unknown.
    cost_usd: float | None = None
    duration_ms: int
    errors: list[str] | None = None
    termination_reason: str | None = None
    # cpp#54: HTTP status of a Claude-API error (429/500/529) surfaced by SDK
    # 0.2.x `ResultMessage.api_error_status`, letting downstream (mika-dev
    # dispatch-lib) classify a transient overload deterministically vs. a
    # genuine failure. None when the session ended without an API error or the
    # SDK did not populate it; serialized absent via exclude_none.
    api_error_status: int | None = None

    def to_line(self) -> str:
        """Serialize to a single JSON line (no trailing newline)."""
        return self.model_dump_json(exclude_none=True)


class GuardrailAbortReason(BaseModel):
    """Reason attached when SessionGuardrails aborts the SDK session."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["guardrail"] = "guardrail"
    # cpp#119: `rate_limited` distinguishes a stall caused by Anthropic
    # throttling (429 / subscription rate-limit rejection) from a genuine
    # `idle_timeout` (the model simply stopped producing). Additive to the
    # three cpp#54-era values — consumers that only recognize the original
    # three still parse the JSON shape; they just do not special-case the new
    # value. See GuardrailAbortReason.api_error_status below.
    guardrail: Literal[
        "stall_detected", "empty_response", "idle_timeout", "rate_limited"
    ]
    turns: int
    detail: str
    # cpp#119: HTTP status of the API error that caused a `rate_limited` abort
    # (429). Lets agent.py surface `api_error_status` on the abort path — not
    # only on the terminal ResultMessage (cpp#54) which never arrives when the
    # idle guardrail fires mid-retry-storm. None for the other guardrail kinds.
    api_error_status: int | None = None


class TransportError(Exception):
    """Raised when the relay subprocess fails or returns malformed output."""
