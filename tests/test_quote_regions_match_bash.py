"""A command whose quoted regions open where bash opens no quote is never admitted.

`_quote_spans` opens a region at every unescaped quote character outside a
region. Bash opens none in a comment or after a heredoc opener, and inside
double quotes it reads the quotes of a `${…}` expansion by its own rules.
Quoted regions must open where bash opens a quote. `split_is_ambiguous` names
the forms whose regions open elsewhere and both admission paths refuse them
before reading a segment. The refusal is not terminal, and the
scanner and the splitter are unchanged, so the denial lethality is too.

Each form is pinned through the full handler, hors and sous confinement, on
each admission path where it is reachable: tier1, and a policy allow judged by
the chain check. The policy path already refuses every heredoc but the
sanctioned one, so the heredoc form is pinned on tier1.

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
    "single-quote-in-comment": "git status # it's\necho done",
    "double-quote-in-comment": 'git status # a"b\necho done\n# "',
    "quote-in-comment-after-ls": "ls # it's\necho done",
    "quote-in-comment-after-tab": "git status\t# it's\necho done",
    "quote-after-heredoc-opener": "git status <<EOF\ngit log '\nEOF\necho done\n'",
    "quote-after-quoted-heredoc-opener": "git status <<'EOF'\ngit log '\nEOF\necho done\n'",
    "quote-after-dash-heredoc-opener": "git status <<-EOF\n\tgit log '\n\tEOF\necho done\n'",
    "quote-after-empty-single-quoted-heredoc-delimiter": "cat <<''\necho '\n\necho done\n# '",
    "quote-after-empty-double-quoted-dash-heredoc-delimiter": (
        "cat <<-\"\"\n\techo '\n\t\necho done\n# '"
    ),
    "heredoc-opener-after-a-hash-that-follows-an-escaped-blank": (
        "echo a\\ #x <<EOF\necho '\nEOF\necho done\n# '"
    ),
    "heredoc-opener-after-a-hash-that-follows-an-escaped-tab": (
        "echo a\\\t#x <<EOF\necho '\nEOF\necho done\n# '"
    ),
    "heredoc-opener-after-a-hash-that-follows-an-escaped-paren": (
        "echo \\(#x <<EOF\necho '\nEOF\necho done\n# '"
    ),
    "single-quote-in-expansion-in-double-quotes": (
        "echo \"${x#'\"'}\"; echo done; echo 'x'"
    ),
    "expansion-cut-by-double-quote": "echo \"${x:-\"'\"}\"; echo done; echo 'x'",
    "single-quote-in-closed-expansion-in-double-quotes": (
        "echo \"${x#'}\"; echo a '}\"; echo done; echo 'x'"
    ),
    "single-quote-in-nested-expansion-in-double-quotes": (
        "echo \"${x:-${y#'\"'}}\"; echo done; echo 'x'"
    ),
    "single-quote-after-nested-expansion-in-double-quotes": (
        "echo \"${x:-${y}'\"'}\"; echo done; echo 'x'"
    ),
}

# Refused on the policy path: tier1 refuses each head and a policy rule allows
# the whole command, so the refusal is the chain check's.
_POLICY_PATH = {
    "single-quote-in-comment": "uv run pytest # it's\necho done",
    "double-quote-in-comment": 'uv run pytest # a"b\necho done\n# "',
    "single-quote-in-expansion-in-double-quotes": (
        "uv run pytest \"${x#'\"'}\"; echo done; echo 'x'"
    ),
    "expansion-cut-by-double-quote": "uv run pytest \"${x:-\"'\"}\"; echo done; echo 'x'",
    "single-quote-in-closed-expansion-in-double-quotes": (
        "uv run pytest \"${x#'}\"; echo a '}\"; echo done; echo 'x'"
    ),
}

# Where bash reads the quotes as the scanner does, nothing changes.
_NOT_AMBIGUOUS = {
    "comment-without-quote": "git status # comment\necho done",
    "escaped-quote-in-comment": "git status # it\\'s\necho done",
    "quote-in-comment-ending-the-command": "git status # it's",
    "quotes-closed-within-the-comment": "git status # 'a'\necho done",
    "hash-inside-a-word": "echo a#'b'",
    "hash-inside-a-word-before-a-multiline-region": "echo a#'b\nc'",
    "hash-after-an-escaped-blank": "echo a\\ #'\necho done\n# '",
    "hash-after-dollar": "echo $# 'x'",
    "hash-after-dollar-before-a-multiline-region": "echo $# 'x\ny'",
    "hash-in-single-quotes": "git log --format='%h #1'",
    "expansion-in-double-quotes": "echo \"${HOME}\" 'x'",
    "expansion-with-default-in-double-quotes": 'echo "${x:-a}"',
    "quotes-in-expansion-outside-double-quotes": "echo ${x:-'a b'}",
    "escaped-dollar-in-double-quotes": "echo \"\\${x#'}\"",
    "policy-comment-without-quote": "uv run pytest # comment",
    "policy-hash-inside-a-word-before-a-multiline-region": "uv run pytest a#'b\nc'",
    "policy-expansion-in-double-quotes": "uv run pytest \"${x:-a}\" 'k'",
}

# The sanctioned heredoc is a whole-command proof that never consults the
# split; a quote in its body is data.
_SANCTIONED_HEREDOC = "cat > /tmp/cpp-quote.sh <<'EOF'\nit's\nEOF"


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

    @pytest.mark.parametrize("cmd", list(_NOT_AMBIGUOUS.values()), ids=list(_NOT_AMBIGUOUS))
    def test_form_is_not_ambiguous(self, cmd: str) -> None:
        assert split_is_ambiguous(cmd) is False

    # Other guards already refuse these on both paths; the predicate reads
    # every place where bash starts a word.
    @pytest.mark.parametrize("before", [";", "|", "&", "(", ")", "\n"],
                             ids=["semicolon", "pipe", "ampersand", "open-paren",
                                  "close-paren", "newline"])
    def test_hash_at_the_start_of_a_word_opens_a_comment(self, before: str) -> None:
        assert split_is_ambiguous(f"git status{before}# it's\necho done") is True

    def test_hash_at_the_start_of_the_command_opens_a_comment(self) -> None:
        assert split_is_ambiguous("# it's\necho done") is True

    def test_hash_after_an_escaped_backslash_then_blank_opens_a_comment(self) -> None:
        assert split_is_ambiguous("git status a\\\\ # it's\necho done") is True

    def test_hash_after_a_redirection_char_opens_a_comment(self) -> None:
        assert split_is_ambiguous("git status >#x '\necho done\n'") is True
        assert split_is_ambiguous("git status <#x '\necho done\n'") is True

    def test_heredoc_opener_after_a_comment_line(self) -> None:
        assert split_is_ambiguous("git status # x\ncat <<EOF 'y'") is True

    def test_heredoc_opener_after_a_hash_glued_to_a_closing_paren(self) -> None:
        # Bash reads the `#` mid-word there; the opener after it still counts.
        assert split_is_ambiguous("cat <(true)#x <<EOF\necho '\nEOF\necho done\n# '") is True

    def test_comment_text_without_quote_after_it_is_not_an_opener(self) -> None:
        assert split_is_ambiguous("git status # a << b") is False

    def test_escaped_less_than_is_not_a_heredoc_opener(self) -> None:
        assert split_is_ambiguous("echo \\<<EOF 'x'") is False

    def test_quote_on_the_heredoc_opener_line_counts(self) -> None:
        # Any quote after the opener counts, body or not.
        assert split_is_ambiguous("cat <<EOF > 'out'\nfoo\nEOF") is True

    def test_here_string_is_not_a_heredoc_opener(self) -> None:
        assert split_is_ambiguous("cat <<< 'x'") is False


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

    def test_sanctioned_heredoc_with_quote_in_body(self, handler) -> None:
        assert handler(_SANCTIONED_HEREDOC) == "allow"
