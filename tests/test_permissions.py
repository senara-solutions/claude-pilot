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
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from claude_pilot import permissions as permissions_module
from claude_pilot.guardrails import SessionGuardrails
from claude_pilot.permissions import create_permission_handler, try_tier_1_5_auto_answer
from claude_pilot.types import (
    GUARDRAIL_DEFAULTS,
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
# cpp#144: absent-operator AskUserQuestion marks the session on policy:deny
# ────────────────────────────────────────────────────────────────────────────


def test_denied_ask_user_question_marks_the_session(tmp_path: Path) -> None:
    """The headless-pilot shape from cpp#144: a fail-closed default deny (no
    policy rule matches AskUserQuestion, so it falls to the policy default)
    refuses the call — non-terminally, since AskUserQuestion is not Bash — and
    records it on the guardrail so agent.py can reclassify a later "success"
    that never delivered."""
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=guardrails,
        policy_path=tmp_path / "nonexistent.yaml",  # missing -> fail-closed deny
    )
    result = asyncio.run(
        handler(
            "AskUserQuestion",
            {"questions": [{"question": "Which branch should I use?"}]},
            _mock_ctx(),
        )
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False, (
        "AskUserQuestion is not Bash — cpp#128's non-lethal-denial contract "
        "must leave the run alive so it can bypass or adapt"
    )
    assert guardrails.operator_question_denied is True
    assert guardrails.operator_question_summary is not None
    assert "Which branch should I use?" in guardrails.operator_question_summary


def test_denied_bash_command_does_not_mark_operator_question(tmp_path: Path) -> None:
    """Name-guard coverage (mirrors mika#940's pr_created tool-name guard): a
    denied Bash command must NOT flip operator_question_denied, even though
    it goes through the same deny branch."""
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=guardrails,
        policy_path=tmp_path / "nonexistent.yaml",
    )
    result = asyncio.run(handler("Bash", {"command": "curl https://example.com"}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert guardrails.operator_question_denied is False
    assert guardrails.operator_question_summary is None


def test_denied_ask_user_question_without_guardrails_does_not_crash(
    tmp_path: Path,
) -> None:
    """`guardrails=None` (e.g. a caller that doesn't wire session tracking)
    must not raise — the cpp#144 marker call is guarded the same way as the
    existing `pause_idle_timer` / `resume_idle_timer` calls in this module."""
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=None,
        policy_path=tmp_path / "nonexistent.yaml",
    )
    result = asyncio.run(
        handler("AskUserQuestion", {"questions": [{"question": "ok?"}]}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)


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


def test_devnull_redirect_denial_is_refused_but_not_lethal() -> None:
    """cpp#130: a read-only command whose only tier3 trigger is a redirect to the
    inert /dev/null sink is still REFUSED, but no longer ends the run.

    Negative control in the same test: `echo hi > /etc/passwd` is a real write and
    stays terminal — the two-character difference #130 names is now the difference
    between a fatal write and an adaptable refusal, not between life and death for
    an equally harmless command.
    """
    handler = _bundled_handler()

    devnull = asyncio.run(
        handler("Bash", {"command": "grep -c a b >/dev/null 2>&1"}, _mock_ctx())
    )
    assert isinstance(devnull, PermissionResultDeny), (
        "a >/dev/null redirect is still refused — cpp#130 does not widen any allow"
    )
    assert devnull.interrupt is False, (
        "a redirect to the inert /dev/null sink must not kill the session (cpp#130)"
    )

    real_write = asyncio.run(
        handler("Bash", {"command": "echo hi > /etc/passwd"}, _mock_ctx())
    )
    assert isinstance(real_write, PermissionResultDeny)
    assert real_write.interrupt is True, (
        "a redirect to a real write target stays terminal in both worlds"
    )


def test_denial_is_terminal_predicate(tmp_path: Path) -> None:
    """Unit-level truth table for the helper (cpp#128 R1-R5)."""
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    # cpp#130 — a redirect to the inert /dev/null sink is refused but NOT fatal;
    # a real write target, or danger chained alongside it, stays fatal.
    assert f("Bash", {"command": "grep -c a b >/dev/null"}, wt) is False
    assert f("Bash", {"command": "grep -c a b >/dev/null 2>&1"}, wt) is False
    assert f("Bash", {"command": "echo hi > /etc/passwd"}, wt) is True
    assert f("Bash", {"command": "rm -rf /tmp/y >/dev/null"}, wt) is True
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
    # cpp#143 — the ONE exception to R3: a `mkdir` scratch directory under
    # `/tmp` is outside the worktree but sanctioned (symmetric with the
    # existing `cat > /tmp/x <<'EOF'` file exception), so it is NOT lethal.
    # `cp`/`mv` into `/tmp` are untouched by this exception and stay lethal.
    assert f("Bash", {"command": "mkdir -p /tmp/rt005-scratch"}, wt) is False
    assert f("Bash", {"command": "cp a.txt /tmp/escaped.txt"}, wt) is True
    assert f("Bash", {"command": "mv a.txt /tmp/escaped.txt"}, wt) is True
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

    ``outside`` is a FIXED literal, not a ``tmp_path``-derived directory (cpp#143):
    pytest's ``tmp_path`` fixture itself lives under ``/tmp``, which is now a
    sanctioned ``mkdir`` scratch destination (see
    ``test_mkdir_tmp_scratch_is_permitted_but_other_outside_targets_stay_lethal``
    below) -- a genuinely-outside-the-worktree probe must be tested with a
    target that is unambiguously outside BOTH the worktree AND the scratch
    exception, or this test would silently stop testing what it says it tests.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    outside = "/definitely/outside/x"
    handler = _bundled_handler(cwd=str(worktree))

    allow_matched = asyncio.run(
        handler("Bash", {"command": f"mkdir -p {outside}"}, _mock_ctx())
    )
    assert isinstance(allow_matched, PermissionResultDeny)
    assert allow_matched.interrupt is True

    chain_vetoed = asyncio.run(
        handler("Bash", {"command": f'echo "go"; mkdir -p {outside}'}, _mock_ctx())
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


def test_mkdir_tmp_scratch_is_permitted_but_other_outside_targets_stay_lethal(
    tmp_path: Path,
) -> None:
    """cpp#143: the incoherence the issue reports, fixed and negatively controlled.

    Before this fix, ``mkdir -p /tmp/<scratch>`` was refused AND terminal --
    the exact shape that killed session ``0160cce6`` (72 tool calls, 2h52) --
    while ``cat > /tmp/x <<'EOF'`` was, and still is, routine. This test proves
    the asymmetry is gone for ``mkdir`` specifically, and that nothing broader
    was opened: a system path is still refused and still terminal (AC3), and a
    `..`-mediated escape THROUGH `/tmp` does not sneak out under the exception.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    # Positive: a /tmp scratch directory -- the exact 0160cce6 shape -- is now
    # ALLOWED (not merely non-terminal; it actually executes).
    scratch = asyncio.run(
        handler(
            "Bash",
            {"command": "mkdir -p /tmp/rt005-empty-nobatch /tmp/rt005-empty-runs/runs"},
            _mock_ctx(),
        )
    )
    assert isinstance(scratch, PermissionResultAllow), (
        "a working directory under /tmp must be permitted, symmetric with the "
        "existing cat-heredoc-to-/tmp file exception"
    )

    # AC3 negative control: a system path is unaffected -- refused, terminal.
    system_path = asyncio.run(
        handler("Bash", {"command": "mkdir -p /etc/cpp143-should-never-exist"}, _mock_ctx())
    )
    assert isinstance(system_path, PermissionResultDeny)
    assert system_path.interrupt is True, (
        "an out-of-scope mkdir (system path, not /tmp) must stay refused and "
        "terminal -- the scratch exception must not widen containment itself"
    )

    # Negative control: a literal `..` in the operand is rejected by the
    # exception's own pattern (mirrors the heredoc's `(?!.*\.\.)`) -- it never
    # reaches a resolve step that could be fooled.
    traversal = asyncio.run(
        handler("Bash", {"command": "mkdir -p /tmp/../etc/cpp143-traversal"}, _mock_ctx())
    )
    assert isinstance(traversal, PermissionResultDeny)
    assert traversal.interrupt is True, (
        "a /tmp-prefixed operand containing .. must not be exempted -- the "
        "exception's own pattern excludes it, no resolve required"
    )

    # Negative control: a symlink INSIDE the worktree that resolves into /tmp
    # is still a cpp#38 containment escape, not a scratch write. The exception
    # is lexical (the literal command text, not the resolved path), so a
    # route that never spells `/tmp/...` in the command does not qualify --
    # this is the one case an earlier (resolve-based) version of this fix got
    # wrong, and it must stay caught.
    (worktree / "esc").symlink_to("/tmp")
    symlink_escape = asyncio.run(
        handler("Bash", {"command": "mkdir -p esc/via-symlink"}, _mock_ctx())
    )
    assert isinstance(symlink_escape, PermissionResultDeny)
    assert symlink_escape.interrupt is True, (
        "a worktree symlink resolving into /tmp is a containment escape "
        "(cpp#38), not the sanctioned /tmp scratch exception (cpp#143) -- "
        "the exception only matches a command that itself spells /tmp/..."
    )

    # Unit-level pin on the helper itself, both arms, so a revert is caught
    # even if the handler-level assertions above are ever loosened.
    f = permissions_module._is_sanctioned_tmp_scratch
    assert f("/tmp/rt005-x") is True
    assert f("/tmp/rt005-x/nested/dir") is True
    assert f("/tmp") is False, "no trailing segment -- conservative on ambiguity"
    assert f("/tmp/../etc/passwd") is False
    assert f("/etc/passwd") is False
    assert f("esc/via-symlink") is False, "relative -- not a literal /tmp/ operand"
    assert f("") is False


# ────────────────────────────────────────────────────────────────────────────
# cpp#155 — `_segment_write_kind` classifies shell-redirect writes from any
# verb (`echo`, `cat`, `tee`, …), not just `cp`/`mv`/`mkdir`/`git show >`.
# `_destination_veto_reason` — the function claude-pilot#155 names as BLIND to
# redirections ("`_destination_veto_reason` returns None" for
# `echo hi > /etc/passwd`, measured against `main`) — now sees them too.
#
# `_denial_is_terminal`'s AGGREGATE verdict for these commands was already
# `True` via `is_tier3_dangerous_for_lethality` (cpp#130/#154/#157) and
# `_redirect_destination_veto_reason` (cpp#154) — this section proves
# `_destination_veto_reason` now ALSO proves it independently, which is what
# closes the gap on the function the ticket names, and on the ALLOWED path
# (`create_permission_handler`'s `pd.decision == "allow"` branch, `:1360`),
# where `_redirect_destination_veto_reason` is never consulted at all.
# ────────────────────────────────────────────────────────────────────────────


def test_segment_write_kind_classifies_redirects_from_any_verb() -> None:
    """Unit pin on the new fallback branch (cpp#155)."""
    f = permissions_module._segment_write_kind
    # New: a real file-target redirect from a verb none of the four leading-
    # word cases name.
    assert f("echo hi > /etc/passwd") == "bash-redirect"
    assert f("cat > /tmp/x") == "bash-redirect"
    assert f("tee /tmp/y < in > /tmp/z") == "bash-redirect"
    assert f("some_unknown_binary --flag > out.log") == "bash-redirect"
    assert f("echo hi >> /etc/passwd") == "bash-redirect"
    assert f("cmd &> /tmp/log") == "bash-redirect"
    # Unaffected: the four leading-word branches still win, unchanged, and the
    # redirect fallback is never reached for them.
    assert f("cp a.txt b.txt") == "bash-cp-mv"
    assert f("mv a.txt b.txt") == "bash-cp-mv"
    assert f("mkdir -p x") == "bash-mkdir"
    assert f("git show deadbeef:f > dest") == "bash-git-show-redirect"
    # No file-target redirect at all -- unclassified, exactly as before.
    assert f("grep -c a b") is None
    assert f("echo hi") is None
    # fd-duplication names no file -- unclassified, mirroring
    # `_redirect_destination_veto_reason`'s own "ignored" forms.
    assert f("cmd 2>&1") is None
    assert f("mika ask >&-") is None
    # `cmd > >(tee f)`: the FIRST `>`'s "target" text is `>(tee f)`, but the
    # target charset excludes `>` (it is the delimiter), so the match is an
    # EMPTY target -- `_redirect_targets` fails closed to `None` for the
    # WHOLE segment (same as `_redirect_destination_veto_reason` and
    # `is_tier3_dangerous_for_lethality` already do for this exact string,
    # pinned in `TestTier3ContainedRedirectLethality`/`test_non_file_redirect_
    # forms` in test_tier1.py). `None` classifies (this docstring's own
    # "targets is None ... STILL classified" clause), and the segment then
    # fails closed downstream too -- `_extract_write_destinations` also
    # returns `None`, so `_destination_veto_reason` vetoes it as an
    # unparseable destination. Conservative, not a false negative.
    assert f("cmd > >(tee f)") == "bash-redirect"
    assert (
        permissions_module._extract_write_destinations("bash-redirect", "cmd > >(tee f)")
        is None
    )
    # `tee FILE` (argument-based write, no shell redirect) is a DIFFERENT,
    # still-open gap -- out of scope for cpp#155, which teaches
    # `_segment_write_kind` about REDIRECTION OPERATORS specifically (the
    # ticket's own scope: `>`, `>>`, `N>`, `N>>`, `&>`, `&>>`), not about verbs
    # whose own argument names a write target. Flagged in the PR body.
    assert f("tee /etc/passwd") is None


def test_destination_veto_now_fires_for_redirects_to_system_or_escaped_paths(
    tmp_path: Path,
) -> None:
    """The exact gap claude-pilot#155 reports, closed and pinned directly on
    `_destination_veto_reason`.

    Anti-vacuity: every assertion below is `is None` on `main` (before this
    fix) -- pasted as the captured red in the PR body.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    f = permissions_module._destination_veto_reason
    wt = str(worktree)

    assert f("echo hi > /etc/passwd", wt) is not None
    assert f("echo hi > ../escape", wt) is not None
    assert f("echo hi > ~/x", wt) is not None
    assert f("echo hi > $VAR/x", wt) is not None
    assert f("echo hi >> /etc/passwd", wt) is not None
    assert f("cat /dev/stdin > /etc/shadow", wt) is not None
    assert f("echo hi > .git/hooks/pre-commit", wt) is not None


def test_destination_veto_still_none_for_tmp_scratch_and_worktree_relative(
    tmp_path: Path,
) -> None:
    """Negative controls: the SAME lexical `/tmp` exception cpp#143/#154 already
    grant (`_is_contained_redirect_target` + the `/tmp/` prefix), reused
    verbatim, plus an ordinary worktree-relative write."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    f = permissions_module._destination_veto_reason
    wt = str(worktree)

    assert f("echo hi > /tmp/scratch", wt) is None
    assert f("echo hi > /tmp/2158bodies/$n.md", wt) is None  # D3 residue, cpp#154
    assert f("echo hi > ./out.txt", wt) is None
    assert f("echo hi > notes.txt", wt) is None
    assert f("echo hi > docs/plans/x.md", wt) is None
    # /dev/null writes nowhere (cpp#130) -- must not regress into a hard veto
    # now that a bare redirect is a classified write-kind.
    assert f("grep -c a b >/dev/null", wt) is None
    assert f("grep -c a b >/dev/null 2>&1", wt) is None
    # fd-duplication names no file -- never reaches the veto loop.
    assert f("cmd 2>&1 | tail", wt) is None


def test_destination_veto_symlink_escape_via_bare_redirect(tmp_path: Path) -> None:
    """cpp#38 extended to a redirect target for the first time (cpp#155): a
    worktree symlink resolving OUT of the worktree is still an escape, caught
    by the same disk-resolving `is_within_project` the cp/mv/mkdir write-kinds
    already use. `_is_contained_redirect_target` lexically admits a plain
    relative target like `esc/x` (no `..`/`~`/leading-`$`), so the disk check
    gets a chance to run and catch the symlink -- exactly the cpp#143 lesson
    (grant exemptions lexically, but let escapes be caught by resolution).
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (worktree / "esc").symlink_to(tmp_path / "outside", target_is_directory=True)
    f = permissions_module._destination_veto_reason
    assert f("echo hi > esc/x", str(worktree)) is not None


def test_destination_veto_heredoc_body_text_not_misread_as_a_command(
    tmp_path: Path,
) -> None:
    """Regression guard for the trap named in `_destination_veto_reason`'s own
    docstring: `_split_compound_command` splits on a bare newline (cpp#103), so
    without the `_is_sanctioned_pure_heredoc` shortcut, a heredoc BODY line
    that happens to contain literal text shaped like a dangerous redirect
    (`echo hi > /etc/passwd`, never executed -- the quoted delimiter makes the
    body inert, cpp#47) would be misclassified as a live write and veto a
    routine, currently-ALLOWED heredoc.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    cmd = "cat > /tmp/cpp155_body.rs <<'EOF'\necho hi > /etc/passwd\nEOF"
    assert permissions_module._destination_veto_reason(cmd, str(worktree)) is None


def test_denial_is_terminal_redirect_gap_closed_at_destination_veto_too(
    tmp_path: Path,
) -> None:
    """`_denial_is_terminal` was already `True` for these via
    `is_tier3_dangerous_for_lethality` (the tier3 `>` pattern is never stripped
    for an un-contained target) -- this test pins that `_destination_veto_
    reason` now ALSO proves it independently. The aggregate verdict is
    unchanged; the specific function claude-pilot#155 names is no longer
    blind.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    for cmd in (
        "echo hi > /etc/passwd",
        "echo hi > ../escape",
        "echo hi > ~/x",
        "echo hi > $VAR/x",
    ):
        assert permissions_module._destination_veto_reason(cmd, wt) is not None, cmd
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
            is True
        ), cmd


def test_handler_vetoes_redirect_to_system_path_end_to_end(tmp_path: Path) -> None:
    """Handler-level proof, mirroring
    `test_containment_escape_is_lethal_on_the_default_deny_route` above: a
    redirect to a system path is refused AND terminal through the real
    `can_use_tool` callback, not merely at the unit level.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    result = asyncio.run(
        handler("Bash", {"command": "echo hi > /etc/passwd"}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True

    # Control: the same shape targeting /tmp scratch stays refused, not fatal.
    scratch = asyncio.run(
        handler("Bash", {"command": "echo hi > /tmp/cpp155-scratch"}, _mock_ctx())
    )
    assert isinstance(scratch, PermissionResultDeny)
    assert scratch.interrupt is False


# ────────────────────────────────────────────────────────────────────────────
# cpp#151 B0/B1 — the lethality of a refusal becomes readable, and the
# survivable half marks the session
#
# cpp#128 split the DECISION from the LETHALITY and left the second half
# unlogged: `ui.log_policy_deny` took no `terminal` argument and
# `_record_decision` emitted only `decision` + `rule_id`. Standing in front of
# the eight dead sessions in the cpp#151 body, nobody could say which ones
# claude-pilot had ASKED to kill (destination veto, tier3-dangerous Bash —
# correct by design) and which died DESPITE `interrupt=False`. These tests pin
# both halves at once: the stderr suffix an operator greps, and the session
# marker agent.py reads.
# ────────────────────────────────────────────────────────────────────────────


def _deny_lines(captured: str) -> list[str]:
    """Every `[policy:deny]`-family line in a captured stderr blob, ANSI intact.

    Matching on the bare tag rather than the colored prefix keeps the helper
    independent of the palette in `ui.py`.
    """
    return [ln for ln in captured.splitlines() if "[policy:deny" in ln]


def test_151_nonterminal_rule_deny_says_so_and_marks_the_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact shape that killed the ticket's sessions: a rule-based refusal
    of a non-dangerous Bash command.

    Three consumers of ONE lethality verdict are asserted together, because the
    bug cpp#151 B0 closes is precisely that they could disagree: the SDK result
    (`interrupt`), the operator-facing log line, and the session marker
    agent.py later reads."""
    policy_file = tmp_path / "rule_deny.yaml"
    policy_file.write_text(
        "rules:\n"
        "  - id: bash-grep\n"
        "    tool: Bash\n"
        "    pattern: '^env \\| grep'\n"
        "    decision: deny\n"
        "    reason: composed read-only command not allow-listed\n"
        "default:\n"
        "  decision: allow\n"
        "  reason: default allow (test fixture)\n"
    )
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=guardrails,
        policy_path=policy_file,
    )
    result = asyncio.run(
        handler("Bash", {"command": "env | grep -c MIKA"}, _mock_ctx())
    )

    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False
    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "(non-terminal)" in lines[0], lines[0]
    assert "bash-grep" in lines[0], lines[0]
    assert guardrails.nonterminal_policy_deny is True
    assert guardrails.nonterminal_policy_deny_summary is not None
    assert "env | grep -c MIKA" in guardrails.nonterminal_policy_deny_summary


def test_151_tier3_dangerous_deny_says_terminal_and_leaves_the_marker_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NON-REGRESSION, arm 1 of 2 (ticket AC4 / plan § "Ce que (B) ne fait pas").

    A tier3-dangerous Bash command is a refusal claude-pilot ASKS to be fatal.
    It must keep `interrupt=True`, must say `(terminal)` in the log, and must
    NOT arm the session marker — arming it would hand a deliberately lethal
    class a free resume in agent.py, which is the one way this change could
    have weakened the safety surface."""
    policy_file = tmp_path / "rule_deny.yaml"
    policy_file.write_text(
        "rules:\n"
        "  - id: deny-sed\n"
        "    tool: Bash\n"
        "    pattern: '^sed\\s'\n"
        "    decision: deny\n"
        "    reason: in-place edit refused\n"
        "default:\n"
        "  decision: allow\n"
        "  reason: default allow (test fixture)\n"
    )
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=guardrails,
        policy_path=policy_file,
    )
    result = asyncio.run(
        handler("Bash", {"command": "sed -i 's/a/b/' notes.txt"}, _mock_ctx())
    )

    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True, "cpp#128's deliberate lethal class must stay lethal"
    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "(terminal)" in lines[0], lines[0]
    assert "(non-terminal)" not in lines[0], lines[0]
    assert guardrails.nonterminal_policy_deny is False, (
        "a refusal we asked to be fatal must never arm the resume marker"
    )


def test_151_destination_veto_says_terminal_and_leaves_the_marker_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """NON-REGRESSION, arm 2 of 2: worktree containment.

    A write escaping the worktree reaches the destination-veto site, whose
    `interrupt=True` is unconditional by design (cpp#128's named exception).
    Same three assertions as the tier3 arm — and the marker stays clear, so a
    session that has already left its sandbox in intent cannot buy another
    turn."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        guardrails=guardrails,
        policy_path=_BUNDLED_POLICY,
    )
    result = asyncio.run(
        handler("Bash", {"command": "mkdir -p /definitely/outside/x"}, _mock_ctx())
    )

    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "(terminal)" in lines[0], lines[0]
    assert "(non-terminal)" not in lines[0], lines[0]
    assert guardrails.nonterminal_policy_deny is False


def test_151_chain_veto_reports_the_lethality_it_actually_returned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chain-veto site is the one whose verdict is COMPUTED rather than
    literal, so both of its outcomes are pinned over the same command shape.

    Inside the worktree the composed `mkdir` is refused and survivable; the
    identical shape pointing outside is refused and fatal. If a future edit
    made the log line quote a second, independently-computed verdict, one of
    these two arms would disagree with its `interrupt`."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        guardrails=guardrails,
        policy_path=_BUNDLED_POLICY,
    )

    inside = asyncio.run(
        handler("Bash", {"command": 'echo "go"; mkdir -p docs/plans'}, _mock_ctx())
    )
    assert isinstance(inside, PermissionResultDeny)
    assert inside.interrupt is False
    inside_lines = _deny_lines(capsys.readouterr().err)
    assert len(inside_lines) == 1, inside_lines
    assert "(non-terminal)" in inside_lines[0], inside_lines[0]
    assert guardrails.nonterminal_policy_deny is True

    outside = asyncio.run(
        handler(
            "Bash",
            {"command": 'echo "go"; mkdir -p /definitely/outside/x'},
            _mock_ctx(),
        )
    )
    assert isinstance(outside, PermissionResultDeny)
    assert outside.interrupt is True
    outside_lines = _deny_lines(capsys.readouterr().err)
    assert len(outside_lines) == 1, outside_lines
    assert "(terminal)" in outside_lines[0], outside_lines[0]
    assert "(non-terminal)" not in outside_lines[0], outside_lines[0]


def test_151_deny_with_notify_is_terminal_and_leaves_the_marker_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`escalate` (deny-with-notify) was untouched by cpp#128 and stays
    untouched here: terminal on the wire, `(terminal)` in the log, marker
    clear. An escalate exists to put a human in the loop; resuming past one
    would defeat its only purpose."""
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
    guardrails = SessionGuardrails(GUARDRAIL_DEFAULTS.model_copy())
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd="/tmp",
        guardrails=guardrails,
        policy_path=policy_file,
    )

    original = permissions_module._fire_notify
    permissions_module._fire_notify = lambda *_a: None  # type: ignore[assignment]
    try:
        result = asyncio.run(handler("Skill", {"skill": "test-target"}, _mock_ctx()))
    finally:
        permissions_module._fire_notify = original  # type: ignore[assignment]

    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "[policy:deny_with_notify]" in lines[0], lines[0]
    assert "(terminal)" in lines[0], lines[0]
    assert guardrails.nonterminal_policy_deny is False


def test_151_terminal_flag_reaches_the_audit_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B0 step 3: the same verdict travels on the cm#99 side-channel.

    Both arms in one test — a wire field that is always the same value carries
    exactly as little as the absent field it replaces."""
    emitted: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(permissions_module.permission_events, "emit", _capture)

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        policy_path=_BUNDLED_POLICY,
    )
    asyncio.run(handler("Bash", {"command": 'echo "go"; mkdir -p docs/plans'}, _mock_ctx()))
    asyncio.run(
        handler("Bash", {"command": "mkdir -p /definitely/outside/x"}, _mock_ctx())
    )

    assert len(emitted) == 2, emitted
    assert emitted[0]["decision"] == "deny"
    assert emitted[0]["terminal"] is False
    assert emitted[1]["decision"] == "deny"
    assert emitted[1]["terminal"] is True
