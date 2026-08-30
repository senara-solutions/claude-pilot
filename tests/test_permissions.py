"""Permission handler tests covering the Tier 1.5 fast path (mika#1191 Phase A).

The full `create_permission_handler` flow is exercised by the CLI/agent tests;
this module unit-tests the deterministic short-circuits introduced for the
mika-relay deprecation milestone, where the relay-bound LLM hop must not fire
for events that are equivalent to TIER 1.5 in
`mika/skills/bundled/permission-policy/system_prompt.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from claude_pilot import permissions as permissions_module
from claude_pilot.permissions import create_permission_handler, try_tier_1_5_auto_answer
from claude_pilot.types import (
    PilotConfig,
    PilotEvent,
    PilotResponseAllow,
    PilotResponseAnswer,
)


def test_compact_safe_question_auto_answered() -> None:
    question = "Choose between full compound and compact-safe compaction modes:"
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"question": question, "options": []}]},
    )
    assert isinstance(result, PilotResponseAnswer)
    assert result.action == "answer"
    assert result.answers == {question: "compact-safe"}


def test_compact_safe_keyword_match_case_insensitive() -> None:
    question = "Run Compact-Safe mode for this session?"
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"question": question}]},
    )
    assert isinstance(result, PilotResponseAnswer)
    assert result.answers == {question: "compact-safe"}


def test_non_compact_safe_question_returns_none() -> None:
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"question": "What's the capital of France?"}]},
    )
    assert result is None


def test_non_ask_user_question_tool_returns_none() -> None:
    # The short-circuit is gated on tool_name; never fire for Bash/Write/etc.
    result = try_tier_1_5_auto_answer(
        "Bash",
        {"command": "echo compact-safe"},
    )
    assert result is None


def test_partial_match_falls_through_to_relay() -> None:
    # Mixed AskUserQuestion: one question matches compact-safe, another does
    # not. Returning a partial answer would leave the non-matching question
    # unanswered and break the SDK contract — fall through instead.
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {
            "questions": [
                {"question": "Choose compact-safe or full compound:"},
                {"question": "Pick a database flavor:"},
            ],
        },
    )
    assert result is None


def test_empty_questions_returns_none() -> None:
    assert try_tier_1_5_auto_answer("AskUserQuestion", {}) is None
    assert try_tier_1_5_auto_answer("AskUserQuestion", {"questions": []}) is None
    assert try_tier_1_5_auto_answer("AskUserQuestion", {"questions": "not a list"}) is None


def test_malformed_question_shape_returns_none() -> None:
    # A non-dict entry inside the questions list is malformed; fall through.
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": ["compact-safe"]},
    )
    assert result is None


def test_compact_safe_word_boundary_excludes_compact_safer() -> None:
    # Word boundary (\bcompact-safe\b) prevents matching substrings like
    # "compact-safer" or "compact-safety", which could otherwise hijack
    # unrelated questions through the lexical loophole flagged in ce:review.
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"question": "Is compact-safer mode preferred?"}]},
    )
    assert result is None


def test_compact_safe_word_boundary_matches_punctuated_forms() -> None:
    # Word boundary still matches "compact-safe?", "(compact-safe)", etc.
    for question_text in (
        "Choose: compact-safe.",
        "Pick (compact-safe) or full compound?",
        'Answer with "compact-safe".',
    ):
        result = try_tier_1_5_auto_answer(
            "AskUserQuestion",
            {"questions": [{"question": question_text}]},
        )
        assert isinstance(result, PilotResponseAnswer), question_text


def test_non_string_question_field_returns_none() -> None:
    # Defensive guard: PilotEvent payloads from older mika versions may have
    # malformed question shapes. Fall through to relay rather than crash.
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"question": 42}]},
    )
    assert result is None


def test_missing_question_key_returns_none() -> None:
    # Dict without a "question" key gets q.get("question", "") -> "", which
    # has no compact-safe substring, so falls through.
    result = try_tier_1_5_auto_answer(
        "AskUserQuestion",
        {"questions": [{"options": ["a", "b"]}]},
    )
    assert result is None


# ────────────────────────────────────────────────────────────────────────────
# Denial lethality (cpp#20 joint 2, NARROWED by cpp#128)
#
# A refusal is always a refusal. `interrupt=True` — which additionally aborts
# the SDK agent loop — is reserved for a destination veto and for
# tier3-dangerous Bash. Every test below asserts the refusal FIRST; the
# lethality assertion is secondary and is the only thing cpp#128 moved.
# ────────────────────────────────────────────────────────────────────────────


def _mock_ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id="tool_test",
        agent_id=None,
    )


def test_handler_default_deny_of_a_tier3_command_is_terminal() -> None:
    """Handler under fail-closed policy (missing file → empty Policy →
    default-deny) refuses, and — because the fixture command is tier3-dangerous
    — also halts, which is the contract dispatch-lib relies on to see a terminal
    ResultJson rather than continue past a silent denial.

    Post-cpp#128 the halt follows from the COMMAND being tier3, not from the
    denial being a default-deny: a default-deny of an ordinary command is
    refused non-terminally. The non-terminal half is pinned by
    ``test_handler_rule_deny_refuses_without_killing_the_session`` and by
    ``test_denial_is_terminal_predicate``.
    """
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        policy_path=Path("/nonexistent/policy.yaml"),
    )
    result = asyncio.run(handler("Bash", {"command": "rm -rf /"}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny), (
        f"expected PermissionResultDeny, got {type(result)}: {result!r}"
    )
    assert result.interrupt is True, (
        f"expected interrupt=True for cpp#20 joint 2 contract, got {result!r}"
    )


def test_handler_rule_deny_refuses_without_killing_the_session(tmp_path: Path) -> None:
    """An explicit rule-based deny REFUSES the command -- and, since cpp#128,
    does so without aborting the run, because ``curl https://example.com`` is
    not tier3-dangerous. The decision is unchanged from the pre-cpp#128
    contract; only the lethality is.

    Uses ``curl`` because Tier 1 fast-path auto-approves common safe
    binaries (echo, awk, find, etc.); we need a command that misses
    Tier 1 so the request reaches the policy evaluator.
    """
    policy_file = tmp_path / "rule_deny.yaml"
    policy_file.write_text(
        "rules:\n"
        "  - id: deny-curl\n"
        "    tool: Bash\n"
        "    pattern: '^curl\\s'\n"
        "    decision: deny\n"
        "    reason: rule-based test deny\n"
        "default:\n"
        "  decision: allow\n"
        "  reason: default allow (test fixture)\n"
    )
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        policy_path=policy_file,
    )
    result = asyncio.run(handler("Bash", {"command": "curl https://example.com"}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "rule-based test deny"
    assert result.interrupt is False, (
        "cpp#128: a non-tier3 rule deny is refused but must not abort the loop"
    )


def test_handler_returns_interrupt_true_on_escalate_decision(tmp_path: Path) -> None:
    """The wire-format ``escalate`` decision (renamed in source to
    deny-with-notify) returns interrupt=True.

    cpp#128 deliberately did NOT touch this path. ``escalate`` exists to put a
    human in the loop, so continuing past it defeats its purpose; it is outside
    the class cpp#128 measured (every killed session logged ``[policy:deny]``,
    none ``[policy:deny_with_notify]``); and ``_fire_notify`` has no dedup, so a
    non-terminal escalate would turn a retry loop into a notification flood.
    """
    policy_file = tmp_path / "escalate.yaml"
    policy_file.write_text(
        "rules:\n"
        "  - id: escalate-skill\n"
        "    tool: Skill\n"
        "    pattern: '^test-target$'\n"
        "    decision: escalate\n"
        "    reason: rule-based test escalate\n"
        "default:\n"
        "  decision: allow\n"
        "  reason: default allow (test fixture)\n"
    )
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        policy_path=policy_file,
    )
    # Use monkeypatched notify so the test does not actually call mika notify.
    from claude_pilot import permissions as permissions_module

    fired: list[tuple[str, str, str]] = []

    def _fake_notify(tool_name: str, detail: str, reason: str) -> None:
        fired.append((tool_name, detail, reason))

    original = permissions_module._fire_notify
    permissions_module._fire_notify = _fake_notify  # type: ignore[assignment]
    try:
        result = asyncio.run(handler("Skill", {"skill": "test-target"}, _mock_ctx()))
    finally:
        permissions_module._fire_notify = original  # type: ignore[assignment]

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "rule-based test escalate"
    assert result.interrupt is True, (
        "escalate (deny-with-notify) must also halt the loop"
    )
    # Notify fired exactly once on this path.
    assert len(fired) == 1


# ────────────────────────────────────────────────────────────────────────────
# cpp#56: PilotEvent enriched from ToolPermissionContext
# ────────────────────────────────────────────────────────────────────────────


def _enriched_ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id="tool_test",
        agent_id="agent_x",
        decision_reason="needs review",
        blocked_path="/etc/passwd",
        title="Read sensitive file",
        display_name="Read",
        description="reads a file outside the workspace",
    )


def _capture_relay_event(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolPermissionContext
) -> PilotEvent:
    """Drive the relay path (policy disabled) and capture the PilotEvent that
    permissions.py constructs from ``ctx``."""
    monkeypatch.setenv("MIKA_PILOT_POLICY_DISABLED", "1")
    captured: dict[str, PilotEvent] = {}

    async def _fake_invoke(_config: PilotConfig, event: PilotEvent, *_a: object) -> PilotResponseAllow:
        captured["event"] = event
        return PilotResponseAllow(action="allow")

    monkeypatch.setattr(permissions_module, "invoke_command", _fake_invoke)

    handler = create_permission_handler(
        config=PilotConfig(command="true"),
        relay=True,
        verbose=False,
        cwd="/tmp",
    )
    # "rm -rf /" misses Tier 1 / Tier 1.5; with policy disabled it reaches relay.
    asyncio.run(handler("Bash", {"command": "rm -rf /"}, ctx))
    return captured["event"]


def test_pilot_event_carries_enriched_context_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cpp#56 present path: all five enriched ToolPermissionContext fields are
    captured onto the relay PilotEvent."""
    event = _capture_relay_event(monkeypatch, _enriched_ctx())
    assert event.decision_reason == "needs review"
    assert event.blocked_path == "/etc/passwd"
    assert event.title == "Read sensitive file"
    assert event.display_name == "Read"
    assert event.description == "reads a file outside the workspace"


def test_pilot_event_absent_context_fields_are_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cpp#56 absent path: a ctx without the enriched fields yields None on the
    PilotEvent (getattr defaults) and does not crash. `_mock_ctx()` is exactly
    such a bare context (only tool_use_id + agent_id set)."""
    event = _capture_relay_event(monkeypatch, _mock_ctx())
    assert event.decision_reason is None
    assert event.blocked_path is None
    assert event.title is None
    assert event.display_name is None
    assert event.description is None
    # exclude_none keeps the absent fields out of the serialized payload.
    assert "title" not in event.model_dump_json(exclude_none=True)


# ────────────────────────────────────────────────────────────────────────────
# cpp#128 — denial lethality, with an explicit negative control
#
# Founding measurement (cpp#128 body): across the 60 most recent pilot sessions
# in `/var/log/claude-pilot/*.stderr`, 11 carried a `[policy:deny]`, 11 ended in
# `error_during_execution`, and they were the SAME 11 -- tool-call counts
# 22, 5, 5, 4, 4, 3, 3, 2, 2, 2, 1, no zero among them. Every session that did
# any work was killed by a refusal. The reference session is
# `09fee003-b3db-432f-b3c2-331bfaa6ee05` (mika#1963, 19:53->20:23, 4 calls,
# zero output), killed by the read-only `for` shape replayed below.
# ────────────────────────────────────────────────────────────────────────────

_BUNDLED_POLICY = (
    Path(__file__).parent.parent
    / "src"
    / "claude_pilot"
    / "policies"
    / "permissions.yaml"
)

# The exact shape that killed session 09fee003 (cpp#128 body). Read-only: a
# glob over directories, an `echo` label, a `cat`.
_SESSION_09FEE003_COMMAND = (
    'for d in /tmp/wt/worktrees/*/; do echo "=== $d ==="; cat "$d/mika/.git"; done | head -40'
)


def _bundled_handler(cwd: str = "/tmp"):
    return create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=cwd,
        policy_path=_BUNDLED_POLICY,
    )


def test_session_09fee003_shape_is_refused_but_no_longer_lethal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-VACUITY NEGATIVE CONTROL (cpp#128).

    Two arms over the SAME command, so the test cannot pass in both worlds.
    Both arms route through the chain-veto site (rule
    ``bash-for-loop-orientation:chain-veto``), and that is exactly what they
    pin -- no more:

    * Arm 1 -- with the fix, the command is still REFUSED (nothing was widened)
      and the run survives. Revert that site to ``interrupt=True`` and this arm
      fails. It does NOT distinguish the helper from a hard-coded ``False``.
    * Arm 2 -- forcing ``_denial_is_terminal`` to ``True`` must kill the session
      again, which pins that the site READS the module-level helper rather than
      a constant. Hard-code ``interrupt=False`` there and this arm fails.

    The helper's own classification is pinned separately by
    ``test_denial_is_terminal_predicate``; the deny and escalate sites by their
    own paired tests.
    """
    handler = _bundled_handler()

    # Arm 1 — post-fix behavior.
    result = asyncio.run(
        handler("Bash", {"command": _SESSION_09FEE003_COMMAND}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny), (
        "cpp#128 widens no rule: the shape must still be refused"
    )
    assert result.interrupt is False, (
        "cpp#128: a refused read-only probe must not abort the agent loop"
    )

    # Arm 2 — negative control: pre-fix contract restored, session dies again.
    monkeypatch.setattr(
        permissions_module,
        "_denial_is_terminal",
        lambda tool_name, tool_input, cwd: True,
    )
    pre_fix = asyncio.run(
        handler("Bash", {"command": _SESSION_09FEE003_COMMAND}, _mock_ctx())
    )
    assert isinstance(pre_fix, PermissionResultDeny)
    assert pre_fix.interrupt is True, (
        "negative control: with interrupt=True restored the session must die -- "
        "if this passes with the helper bypassed, the call sites ignore it"
    )


def test_nonlethal_denial_still_emits_the_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that no longer kills the session must still be VISIBLE.

    cpp#128 changes lethality only: the cm#99 permission event fires with
    ``decision == "deny"`` and the producing rule id, exactly as before.
    """
    captured: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(permissions_module.permission_events, "emit", _capture)

    result = asyncio.run(
        _bundled_handler()("Bash", {"command": _SESSION_09FEE003_COMMAND}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False

    assert len(captured) == 1, f"expected exactly one permission event, got {captured}"
    event = captured[0]
    assert event["decision"] == "deny"
    assert event["tool_name"] == "Bash"
    assert event["rule_id"], "the refusal must carry the rule id that produced it"


def test_tier3_dangerous_denial_stays_lethal() -> None:
    """The class cpp#128 deliberately did NOT touch. A dangerous tail chained
    onto an allowed prefix is caught by the whole-string tier3 search and still
    ends the run.
    """
    result = asyncio.run(
        _bundled_handler()("Bash", {"command": "mkdir x && rm -rf /tmp/y"}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True


def test_denial_is_terminal_predicate(tmp_path: Path) -> None:
    """Unit-level truth table for the helper (cpp#128 R1-R5)."""
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    # R1 — ordinary refused Bash: non-lethal.
    assert f("Bash", {"command": 'echo "label"; grep -c foo bar.rs'}, wt) is False
    assert f("Bash", {"command": _SESSION_09FEE003_COMMAND}, wt) is False
    # R2 — tier3-dangerous Bash: lethal, including as a chained tail.
    assert f("Bash", {"command": "rm -rf /"}, wt) is True
    assert f("Bash", {"command": "mkdir x && rm -rf /tmp/y"}, wt) is True
    assert f("Bash", {"command": "git push --force origin x"}, wt) is True
    # R3 — a containment escape is lethal on EVERY route, not only on the one
    # that happens to match a write-capable allow rule. Coverage is exactly
    # `_segment_write_kind`'s: `mkdir`, `cp`/`mv`, and `git show >`. A verb it
    # does not classify (`touch`, `tee`, ...) is still REFUSED — nothing is
    # written — but non-terminally; closing that gap means teaching
    # `_segment_write_kind` more verbs, which is a separate change.
    assert f("Bash", {"command": "mkdir -p /definitely/outside/x"}, wt) is True
    assert f("Bash", {"command": "cp a.txt /definitely/outside/b.txt"}, wt) is True
    assert f("Bash", {"command": "mv a.txt /definitely/outside/b.txt"}, wt) is True
    # ...and an in-worktree write of the same shape is not.
    assert f("Bash", {"command": "mkdir -p docs/plans"}, wt) is False
    # R4 — non-Bash tools have no tier3 or destination notion: non-lethal.
    assert f("Skill", {"skill": "test-target"}, wt) is False
    assert f("Write", {"file_path": "/etc/passwd", "content": "x"}, wt) is False
    # R5 — no parseable command fails closed, in BOTH of its forms: a missing
    # key and a non-string value are the same condition and must not classify
    # oppositely.
    assert f("Bash", {"command": None}, wt) is True
    assert f("Bash", {"command": ["rm", "-rf", "/"]}, wt) is True
    assert f("Bash", {}, wt) is True
    # An explicitly empty command IS parseable, and is not dangerous.
    assert f("Bash", {"command": ""}, wt) is False


def test_containment_escape_is_lethal_on_the_default_deny_route(
    tmp_path: Path,
) -> None:
    """The security gap the review found, closed and pinned.

    ``mkdir -p <outside>`` matches the ``bash-mkdir`` allow rule, so it reaches
    ``_destination_veto_reason`` at its own call site and halts. The SAME escape
    with a label prefixed -- ``echo "go"; mkdir -p <outside>`` -- fails
    chain-safety, never reaches that call site, and before this change came back
    non-terminal. Both must halt, otherwise the containment boundary degrades
    from a one-shot tripwire into an oracle a prompt-injected pilot can probe
    once per turn for the rest of its budget.

    The paired control is the same command pointing INSIDE the worktree: it must
    stay non-terminal, or the fix would have made ordinary refusals fatal again.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    handler = _bundled_handler(cwd=str(worktree))

    allow_matched = asyncio.run(
        handler("Bash", {"command": f"mkdir -p {outside}/x"}, _mock_ctx())
    )
    assert isinstance(allow_matched, PermissionResultDeny)
    assert allow_matched.interrupt is True

    chain_vetoed = asyncio.run(
        handler("Bash", {"command": f'echo "go"; mkdir -p {outside}/x'}, _mock_ctx())
    )
    assert isinstance(chain_vetoed, PermissionResultDeny)
    assert chain_vetoed.interrupt is True, (
        "a containment escape must end the run on the chain-veto route too"
    )

    # Control: the identical shape INSIDE the worktree is refused, not fatal.
    inside = asyncio.run(
        handler("Bash", {"command": 'echo "go"; mkdir -p docs/plans'}, _mock_ctx())
    )
    assert isinstance(inside, PermissionResultDeny)
    assert inside.interrupt is False
