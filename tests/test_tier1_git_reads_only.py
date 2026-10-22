"""tier1 admits only real git reads.

`git_read_overreaches` holds the closed list of forms `bash-git-readonly`'s
subcommands are admitted in. `is_safe_bash_command` consults it on every
segment of the command, as bash splits it, so no tier1 path admits a form the
list refuses: the simple path, each segment of a policy-judged chain, the
contained read-only compound, and the review scope probe. Each term has a test
that turns red when it alone is removed.

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
from claude_pilot.tier1 import is_safe_bash_command

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)

# Not a read: each changes a ref, its config, a worktree, or leaves the read
# mode. tier1 used to admit every one of them on its own.
_NOT_A_READ = [
    "git branch newb",
    "git branch newb origin/main",
    "git branch -c a b",
    "git branch --copy a b",
    "git branch -u origin/main",
    "git branch --set-upstream-to=origin/main",
    "git branch --unset-upstream",
    "git branch --edit-description",
    "git branch --track newb origin/main",
    "git branch --no-track newb main",
    "git worktree add ../x",
    "git worktree add -b nb ../x main",
    "git worktree remove ../x",
    "git worktree prune",
    "git worktree move a b",
    "git worktree lock a",
    "git worktree unlock a",
    "git worktree repair",
    "git fetch origin main:main",
    "git fetch origin main:refs/remotes/origin/x",
    "git fetch origin +main",
    "git fetch --set-upstream origin main",
    "git fetch --depth=1 origin",
    "git diff --no-index a b",
    "git diff /etc/hostname README.md",
    "git diff -- ../x README.md",
    "git diff --ext-diff",
    "git log --ext-diff -p",
    "git show --ext-diff HEAD",
]

# A separator inside a quoted argument does not split the segment.
_QUOTED_SEPARATOR = [
    "git log '--grep=x;y' --ext-diff -p",
    "git branch newb 'HEAD^{/x;y}'",
    "git log '--grep=a|b' --ext-diff -p",
]

# The review scope probe reads BASE with these.
_PROBE_NOT_A_READ = [
    "BASE=$(git merge-base HEAD main 2>/dev/null); git diff --ext-diff $BASE",
    "BASE=$(git merge-base HEAD main 2>/dev/null); git log --ext-diff -p $BASE..HEAD",
    "BASE=$(git merge-base HEAD main 2>/dev/null); git diff --no-index $BASE x",
]


def _wrapped(forms: list[str]) -> list[str]:
    return [
        c for f in forms for c in (f, f"git status && {f}", f"{f} | head -5", f"{f} 2>/dev/null")
    ]


_ADMITTED = [
    "git status",
    "git branch --show-current",
    "git branch -a",
    "git branch --list 'fix/*'",
    "git worktree list --porcelain",
    "git fetch origin main",
    "git fetch --prune origin",
    "git diff --stat origin/main",
    "git log --oneline -5",
    "git show HEAD:README.md",
    "git log '--grep=a|b' --oneline",
    "git branch --show-current && git status --short | head -30",
    "git log --oneline -5 2>/dev/null",
    "BASE=$(git merge-base HEAD main 2>/dev/null); git diff --stat $BASE",
]


@pytest.fixture(params=[False, True], ids=["uncontained", "contained"])
def contained(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    for var in ("MIKA_GATEWAY_URL", "MIKA_INTERNAL_TOKEN", "MIKA_PILOT_POLICY_PATH",
                "MIKA_PERMISSION_POLICY_MODE", "MIKA_PILOT_POLICY_DISABLED",
                "MIKA_PILOT_CONTAINED", "BASE"):
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
        return "deny"

    return decide


class TestTier1:
    @pytest.mark.parametrize("cmd", _wrapped(_NOT_A_READ) + _QUOTED_SEPARATOR + _PROBE_NOT_A_READ)
    def test_not_a_read_is_not_safe(self, contained: bool, cmd: str) -> None:
        assert is_safe_bash_command(cmd) is False

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_read_is_safe(self, contained: bool, cmd: str) -> None:
        assert is_safe_bash_command(cmd) is True


class TestHandler:
    @pytest.mark.parametrize("cmd", _wrapped(_NOT_A_READ) + _QUOTED_SEPARATOR + _PROBE_NOT_A_READ)
    def test_refused(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_read_is_admitted(self, handler, cmd: str) -> None:
        assert handler(cmd) == "allow"
