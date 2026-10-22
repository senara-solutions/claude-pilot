"""A command the splitter may cut where bash does not is never admitted.

`_split_compound_command` is read by every admission guard of tier1 and of the
policy chain check. `split_is_ambiguous` names the forms where its segments may
differ from the statements bash runs — a carriage return, a line continuation
(unquoted or inside double quotes), an escaped separator — and both admission
paths refuse them before reading a segment. The refusal is not terminal, and
the splitter itself is unchanged, so the denial lethality is too.

Each form is pinned through the full handler, hors and sous confinement, on
each admission path: tier1, and a policy allow judged by the chain check. Each term has a test that turns red when it alone is removed.

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
from claude_pilot.policy import evaluate, load_policy
from claude_pilot.tier1 import is_tier1_auto_approve, split_is_ambiguous

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)

# Refused on the tier1 path: each head is one tier1 admits on its own.
_TIER1_PATH = {
    "carriage-return": "git status \r echo done",
    "line-continuation": "git status \\\n echo done",
    "line-continuation-in-double-quotes": 'git status "a\\\nb" && echo done',
    "escaped-semicolon": "git status \\; echo done",
    "escaped-pipe": "git status \\| echo done",
    "escaped-ampersand": "git status \\& echo done",
    "carriage-return-cd": "cd src \r echo done",
    "line-continuation-git-add": "git add x \\\n echo done",
    "escaped-semicolon-git-add": "git add x \\; echo done",
    "line-continuation-after-redirection-char": "git status <\\\n& echo done",
}

# Refused on the policy path: tier1 refuses each head and a policy rule allows
# the whole command, so the refusal is the chain check's. All but the escaped
# ampersand were honored there before.
_POLICY_PATH = {
    "carriage-return": "uv run pytest \r echo done",
    "line-continuation": "uv run pytest \\\n echo done",
    "line-continuation-in-double-quotes": 'mkdir -p "out\\\nx" && echo done',
    "escaped-semicolon": "uv run pytest \\; echo done",
    "escaped-pipe": "uv run pytest \\| echo done",
    "escaped-ampersand": "uv run pytest \\& echo done",
    "for-loop-carriage-return": "for f in a b; do\r\n  cat $f\r\ndone",
    "for-loop-line-continuation-inside-a-word": "for f in a b; do\n  cat\\\n$f\ndone",
}

# A cut that moves nothing: only blanks follow the escaped separator or the
# continuation, up to a real separator or the end, so the splitter's statement
# boundary is bash's.
_CUT_MOVES_NOTHING = {
    "find-exec-terminator": "find . -name x -exec grep -l y {} \\;",
    "find-exec-terminator-then-pipe": "find . -name x -exec grep -l y {} \\; | wc -l",
    "find-exec-terminator-then-and": "find . -name x -exec cat {} \\; && echo done",
    "find-exec-terminator-then-semicolon": "find . -name x -exec cat {} \\; ; echo done",
    "continuation-before-blank-line": "git status \\\n\necho done",
    "continuation-before-pipe": "git status \\\n  | head -5",
    "continuation-before-and": "git status \\\n  && echo done",
    "continuation-at-end": "git status \\\n",
    "cpp151-founding-fixture": (
        "for n in 2127 2140 2108 1772 2151 2117; do\n"
        '  echo "===== $n ====="\n'
        "  gh issue view $n --repo senara-solutions/mika --json body -q .body \\\n"
        '    | grep -n "Grooming history\\|> - \\*\\*Branch\\|> - \\*\\*Plan"\n'
        "done"
    ),
    "policy-continuation-before-pipe": "uv run pytest \\\n  | tail -5",
}

# Where bash gives the character no such meaning, nothing changes.
_NOT_AMBIGUOUS = {
    "continuation-in-single-quotes": "git status 'a\\\nb'",
    "escaped-backslash-then-newline": "git status \\\\\necho done",
    "escaped-backslash-then-newline-in-double-quotes": 'git status "a\\\\\nb"',
    "separator-in-single-quotes": "git status '\\;' && echo done",
    "separator-in-double-quotes": 'git status "\\;" && echo done',
    "fd-duplication": "git status 2>&1 | head -5",
    "policy-chain": "uv run pytest && echo done",
    "policy-chain-single-quoted-continuation": "mkdir -p out && echo 'a\\\nb'",
}

# The sanctioned heredoc is a whole-command proof that never consults the
# split; a continuation in its body is data bash does not join.
_SANCTIONED_HEREDOC = "cat > /tmp/cpp-split.sh <<'EOF'\nfoo \\\nbar\nEOF"


@pytest.fixture(params=[False, True], ids=["uncontained", "contained"])
def contained(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    for var in ("MIKA_GATEWAY_URL", "MIKA_INTERNAL_TOKEN", "MIKA_PILOT_POLICY_PATH",
                "MIKA_PERMISSION_POLICY_MODE", "MIKA_PILOT_POLICY_DISABLED",
                "MIKA_PILOT_CONTAINED"):
        monkeypatch.delenv(var, raising=False)
    if request.param:
        monkeypatch.setenv("MIKA_PILOT_CONTAINED", "1")
    return bool(request.param)


@pytest.fixture()
def handler(contained: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(permissions_module.permission_events, "emit", lambda **_: None)
    monkeypatch.setattr(permissions_module, "_fire_notify", lambda *_a, **_k: None)
    h = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(tmp_path),
        policy_path=_BUNDLED_POLICY,
    )
    ctx = ToolPermissionContext(signal=None, suggestions=[], tool_use_id="t", agent_id=None)

    def decide(command: str) -> str:
        r = asyncio.run(h("Bash", {"command": command}, ctx))
        if isinstance(r, PermissionResultAllow):
            return "allow"
        assert isinstance(r, PermissionResultDeny)
        return "deny (terminal)" if r.interrupt else "deny"

    return decide


class TestSplitIsAmbiguous:
    @pytest.mark.parametrize(
        "cmd", [*_TIER1_PATH.values(), *_POLICY_PATH.values()],
        ids=[*(f"tier1-{k}" for k in _TIER1_PATH), *(f"policy-{k}" for k in _POLICY_PATH)],
    )
    def test_form_is_ambiguous(self, cmd: str) -> None:
        assert split_is_ambiguous(cmd) is True

    @pytest.mark.parametrize(
        "cmd", [*_NOT_AMBIGUOUS.values(), *_CUT_MOVES_NOTHING.values()],
        ids=[*_NOT_AMBIGUOUS, *_CUT_MOVES_NOTHING],
    )
    def test_form_is_not_ambiguous(self, cmd: str) -> None:
        assert split_is_ambiguous(cmd) is False


    def test_redirection_after_the_cut_is_text(self) -> None:
        # `&>` is a redirection, not the end of the statement.
        assert split_is_ambiguous("git status \\; &>/dev/null") is True


class TestTier1Path:
    @pytest.mark.parametrize("cmd", list(_TIER1_PATH.values()), ids=list(_TIER1_PATH))
    def test_tier1_refuses(self, contained: bool, tmp_path: Path, cmd: str) -> None:
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(tmp_path)) is False

    @pytest.mark.parametrize("cmd", list(_TIER1_PATH.values()), ids=list(_TIER1_PATH))
    def test_handler_refuses_not_terminal(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"


class TestPolicyPath:
    @pytest.mark.parametrize("cmd", list(_POLICY_PATH.values()), ids=list(_POLICY_PATH))
    def test_reaches_the_chain_check(self, contained: bool, tmp_path: Path, cmd: str) -> None:
        # The policy allows the whole command and tier1 does not decide it:
        # the refusal below is the chain check's.
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(tmp_path)) is False
        decision = evaluate(load_policy(_BUNDLED_POLICY), "Bash", {"command": cmd})
        assert decision.decision == "allow"

    @pytest.mark.parametrize("cmd", list(_POLICY_PATH.values()), ids=list(_POLICY_PATH))
    def test_handler_refuses_not_terminal(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"


class TestNeighboursStayAdmitted:
    @pytest.mark.parametrize("cmd", list(_NOT_AMBIGUOUS.values()), ids=list(_NOT_AMBIGUOUS))
    def test_admitted(self, handler, cmd: str) -> None:
        assert handler(cmd) == "allow"

    def test_sanctioned_heredoc_with_continuation_in_body(self, handler) -> None:
        assert handler(_SANCTIONED_HEREDOC) == "allow"

    @pytest.mark.parametrize("cmd", list(_CUT_MOVES_NOTHING.values()), ids=list(_CUT_MOVES_NOTHING))
    def test_cut_that_moves_nothing(self, handler, cmd: str) -> None:
        assert handler(cmd) == "allow"
