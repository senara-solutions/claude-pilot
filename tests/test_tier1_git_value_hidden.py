"""A git argument whose value the text does not show is never auto-admitted.

The git guards (`_MAIN_MASTER_RE`, `_FORCE_FLAG_RE`, `_GIT_DENIED_GLOBAL_FLAG_RE`,
`TIER3_PATTERNS`) read raw text; bash evaluates that text before git sees it.
`git_argument_value_hidden` is the one predicate both layers consult — tier1
admission and the policy chain-safe veto. Each test below is the only one that
turns red when its own clause or call site is removed (see mutations in the PR).

No test touches the network or GitHub: the handler tests use the bundled policy
with the cm emitter and the notify hook replaced by no-ops.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

import claude_pilot.permissions as permissions_module
from claude_pilot.permissions import create_permission_handler
from claude_pilot.tier1 import git_argument_value_hidden, is_safe_bash_command

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)


@pytest.fixture(params=["0", "1"], ids=["uncontained", "contained"])
def containment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "1":
        monkeypatch.setenv("MIKA_PILOT_CONTAINED", "1")
    else:
        monkeypatch.delenv("MIKA_PILOT_CONTAINED", raising=False)
    return str(request.param)


@pytest.fixture()
def handler(monkeypatch: pytest.MonkeyPatch, containment: str):
    for var in ("MIKA_GATEWAY_URL", "MIKA_INTERNAL_TOKEN", "MIKA_PILOT_POLICY_PATH",
                "MIKA_PERMISSION_POLICY_MODE", "MIKA_PILOT_POLICY_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(permissions_module.permission_events, "emit", lambda **_: None)
    monkeypatch.setattr(permissions_module, "_fire_notify", lambda *_a, **_k: None)
    h = create_permission_handler(
        config=None, relay=False, verbose=False, cwd="/tmp", policy_path=_BUNDLED_POLICY,
    )
    ctx = ToolPermissionContext(signal=None, suggestions=[], tool_use_id="t", agent_id=None)

    def decide(command: str) -> str:
        r = asyncio.run(h("Bash", {"command": command}, ctx))
        if isinstance(r, PermissionResultAllow):
            return "allow"
        assert isinstance(r, PermissionResultDeny)
        return "deny-terminal" if r.interrupt else "deny"

    return decide


# ── Refused: the push target is an expansion (clause c) ──────────────────────

_PUSH_BY_EXPANSION = [
    "printf -v X %s main; git push origin $X",
    "git push origin $X",
    "git push origin $BRANCH",
    "git push origin ${X}",
    'git push origin "$X"',
    "git push origin HEAD:$X",
    "git push origin HEAD:refs/heads/${X}",
    "git push $REMOTE HEAD",
    "git push $F origin HEAD",
    "git push origin :$X",
]

# ── Refused: an expansion that is not a plain reference (clause b) ───────────

_NON_PLAIN_EXPANSION = [
    "git diff ${HOME:+--output=/tmp/a}",
    "git diff ${OTHER:+--output=/tmp/a}",
    "git log ${X:-main}",
    "git log ${X/a/b}",
    "git log ${X#a}",
    "git log ${X%a}",
    "git log ${!X}",
    "git log ${#X}",
    "git log $1",
    'git log "$@"',
    "git diff $_",
    "git diff ${_}",
    "git log $[B]",
    # Only the PID is inert; the other special parameters are not.
    'git stash push -u -m "wip-$!"',
    'git stash push -u -m "wip-$-"',
]

# ── Refused: an assignment the text does not spell (clause a) ────────────────

_HIDDEN_ASSIGNMENT = [
    "printf -v X %s --output=/tmp/a; git diff $X",
    "printf -vX %s --output=/tmp/a; git diff $X",
    'printf "-v" X %s --output=/tmp/a; git diff $X',
    "echo ${X:=--output=/tmp/a}; git diff $X",
    "echo ${X=--output=/tmp/a}; git diff $X",
    # Review: nested and array forms, and a quoted / escaped `printf`.
    "echo ${Z:-${X:=--output=/tmp/a}}; git diff $X",
    "echo ${A[0]:=x}; git diff $A",
    '"printf" -v X %s --output=/tmp/a; git diff $X',
    "\\printf -v X %s --output=/tmp/a; git diff $X",
]

# ── Refused: what git receives differs from the text read (clause d) ─────────

_PUSH_TARGET_SPLICED = [
    'git push origin HEAD:"ma"in',
    "git push origin HEAD:mai\\n",
    "git push origin HEAD:'master'",
    'git push -"f" origin feat/x',
    "git push origin HEAD:ma*",
    "git push origin HEAD:{ma,x}in",
    "git push origin +HEAD:feat/x",
    "git push --mirror origin",
    "git push origin --all",
    # Literal, but the tier3 pattern only looks right after the remote and the
    # policy re-admitted it: destination main via a refspec.
    "git push origin HEAD:main",
    "git push origin HEAD:refs/heads/main",
    # Review: git resolves these DWIM spellings to refs/heads/main too.
    "git push origin HEAD:heads/main",
    "git push origin heads/master",
    "git push origin @:heads/main",
    'git push origin HEAD:"heads/ma"in',
]

# ── Refused: a quoted flag, a brace, a leading glob (clause e) ───────────────

_SPLICED_OR_EXPANDED_ARG = [
    'git branch -"D" feat/x',
    "git branch -\\D feat/x",
    'git branch --"delete" feat/x',
    'git diff --"output"=/tmp/a',
    'git fetch --"upload-pack"=x origin',
    "git diff {--output=/tmp/a,HEAD}",
    "git diff HEAD{,--output=/tmp/a}",
    "git log {1..3}",
    "git log *",
    "git log ?ain",
    # Arity: a global option before the subcommand proves nothing about it.
    "git --git-dir x status $X",
    "git -c a=b log $X",
]

# ── Admitted before AND after: legitimate forms that must not regress ────────

_STILL_ADMITTED = [
    "git status",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git diff $BASE HEAD",
    "git diff ${BASE} HEAD",
    'git log "$BASE"..HEAD --oneline',
    "git push -u origin fix/foo",
    "git push origin HEAD",
    "git push -u origin feat/x-y",
    'git commit -m "fix [ci] thing?"',
    "git commit -m 'costs $5'",
    # Parity (3 pilots): the PID is digits the pilot does not choose.
    'git stash push -u -m "mika2118-wip-$$"',
    "git add src/a.py tests/test_a.py",
    "printf '%s\\n' a",
    "printf -- -v",
    'grep -n "git push origin $X" docs/x.md',
    "git diff -- *.py",
    "git add src/*.py",
    "git log --oneline -3  # don't care",
    'git log --grep="fix"',
    "git push -u origin feat/heads-up",
    # Parity (10 pilots): git revision syntax is not brace expansion.
    "git log --oneline @{u}..HEAD",
    "git rev-parse @{upstream}",
    "git rev-parse HEAD^{tree}",
]


@pytest.mark.parametrize(
    "command",
    _PUSH_BY_EXPANSION + _NON_PLAIN_EXPANSION + _HIDDEN_ASSIGNMENT + _PUSH_TARGET_SPLICED
    + _SPLICED_OR_EXPANDED_ARG,
)
def test_predicate_flags_hidden_values(command: str) -> None:
    assert git_argument_value_hidden(command)


@pytest.mark.parametrize("command", _STILL_ADMITTED)
def test_predicate_leaves_visible_values_alone(command: str) -> None:
    assert not git_argument_value_hidden(command)


@pytest.mark.parametrize(
    "command",
    _PUSH_BY_EXPANSION + _NON_PLAIN_EXPANSION + _HIDDEN_ASSIGNMENT + _PUSH_TARGET_SPLICED
    + _SPLICED_OR_EXPANDED_ARG,
)
def test_tier1_refuses_hidden_values(command: str, containment: str) -> None:
    assert not is_safe_bash_command(command)


@pytest.mark.parametrize(
    "command",
    [c for c in _STILL_ADMITTED if not c.startswith("grep")],
)
def test_tier1_still_admits_visible_values(command: str, containment: str) -> None:
    assert is_safe_bash_command(command)


@pytest.mark.parametrize(
    "command",
    _PUSH_BY_EXPANSION + _NON_PLAIN_EXPANSION + _HIDDEN_ASSIGNMENT + _PUSH_TARGET_SPLICED
    + _SPLICED_OR_EXPANDED_ARG,
)
def test_handler_refuses_hidden_values_without_ending_the_run(command: str, handler) -> None:
    # The policy's `bash-git-push` / `bash-git-readonly` prefix rules used to
    # re-admit these once tier1 refused them. Refusal only: lethality unchanged.
    assert handler(command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin fix/foo",
        "git push -u origin $(git branch --show-current)",
        # Parity: an allowlisted substitution is redacted to `_SUB_` BEFORE the
        # veto runs; checked on the raw text, these real pilot reads would flip.
        "git diff --name-only $(git merge-base HEAD origin/main)",
        "git diff $BASE HEAD",
        "git status",
        'git commit -m "fix [ci] thing?"',
    ],
)
def test_handler_still_admits_visible_values(command: str, handler) -> None:
    assert handler(command) == "allow"


def test_handler_literal_push_to_main_stays_terminal(handler) -> None:
    assert handler("git push origin main") == "deny-terminal"


# ── tier1-only guards re-run on the quote-removed text ───────────────────────


@pytest.mark.parametrize(
    "command",
    [
        'git checkout -"f" feat/x',
        "git checkout -\\f feat/x",
        'git branch -"D" feat/x',
        'git diff --"output"=/tmp/a',
    ],
)
def test_tier1_guards_see_through_quote_splicing(command: str, containment: str) -> None:
    assert not is_safe_bash_command(command)


@pytest.mark.parametrize(
    "command",
    [
        'git push origin "unterminated',
        "git log ${X",
        "git log ${X:-a",
        "git $SUB origin HEAD",
        # An unlexable segment must not end the scan: the next one is judged.
        "echo ${X\ngit push origin $X",
        "printf -v X %s ${Y",
    ],
)
def test_unlexable_or_dynamic_git_command_fails_closed(command: str) -> None:
    assert git_argument_value_hidden(command)


def test_unlexable_segment_without_git_or_assignment_is_not_flagged() -> None:
    # Fail-closed is scoped: an unbalanced quote elsewhere is bash's problem.
    assert not git_argument_value_hidden("echo it's fine")
