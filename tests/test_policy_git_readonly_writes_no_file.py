"""A git subcommand admitted as read-only writes no file.

`git log`, `git show` and `git diff` accept `--output`, which makes git write
its output to a file. tier1 already refuses it; `git_option_writes_a_file` is
the veto that keeps a policy allow (`bash-git-readonly` is a prefix match) from
honoring it. Each clause and the call site has a test that turns red when it
alone is removed.

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
from claude_pilot.tier1 import git_option_writes_a_file

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)

# Every spelling git accepts for these three subcommands (git rejects every
# abbreviation of `--output`), alone and inside a compound.
_OUTPUT_FORMS = [
    "git diff --output=out.txt",
    "git diff --output out.txt",
    "git diff HEAD~1 HEAD --output=out.txt",
    "git log -p --output=out.txt",
    "git log -p --output out.txt",
    "git show --output=out.txt HEAD",
    "git show --output out.txt HEAD",
    "git status && git diff --output=out.txt",
    "git log -1 -p --output out.txt | head",
    "git diff -- --output=out.txt",
]

# Quoted or escaped spellings: already refused on the policy path by the
# hidden-value veto; kept here as witnesses.
_QUOTED_FORMS = [
    "git diff '--output=out.txt'",
    'git diff --"output"=out.txt',
    "git diff \\--output out.txt",
]

# Ordinary reads: decision unchanged (admitted).
_ADMITTED = [
    "git diff --stat",
    "git diff HEAD --stat",
    "git log --oneline -5",
    "git log -p -1",
    "git show HEAD:README.md",
    "git show --stat HEAD",
    "git diff --output-indicator-new=+ HEAD",
    "git log -p --output-indicator-old=- -1",
    "git status && git diff --name-only",
    "git show HEAD:README.md > notes.md",
]


@pytest.fixture()
def handler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for var in ("MIKA_GATEWAY_URL", "MIKA_INTERNAL_TOKEN", "MIKA_PILOT_POLICY_PATH",
                "MIKA_PERMISSION_POLICY_MODE", "MIKA_PILOT_POLICY_DISABLED",
                "MIKA_PILOT_CONTAINED"):
        monkeypatch.delenv(var, raising=False)
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
        return "deny"

    return decide


class TestPredicate:
    @pytest.mark.parametrize("cmd", _OUTPUT_FORMS + _QUOTED_FORMS)
    def test_output_option_is_detected(self, cmd: str) -> None:
        assert git_option_writes_a_file(cmd) is True

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_read_is_not(self, cmd: str) -> None:
        assert git_option_writes_a_file(cmd) is False

    def test_only_a_git_invocation_counts(self) -> None:
        assert git_option_writes_a_file("echo --output=out.txt") is False

    def test_every_segment_is_read(self) -> None:
        assert git_option_writes_a_file("echo x; git log --output out.txt") is True

    def test_a_comment_ends_its_line_only(self) -> None:
        # The lexer stops at `#`; the next line is still a command bash runs.
        assert git_option_writes_a_file("git status # note\ngit diff --output=out.txt") is True


class TestHandler:
    @pytest.mark.parametrize("cmd", _OUTPUT_FORMS + _QUOTED_FORMS)
    def test_output_option_is_refused(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_read_is_admitted(self, handler, cmd: str) -> None:
        assert handler(cmd) == "allow"
