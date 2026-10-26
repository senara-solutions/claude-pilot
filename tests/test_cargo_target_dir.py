"""cargo's build directory stays in the worktree (cpp#248).

`--target-dir V`, `--target-dir=V` and `CARGO_TARGET_DIR=V` name the directory
cargo writes its whole build into. Each spelling is tested on its own, so
removing the check for one of them turns its own cases red.

No test touches the network or GitHub: the handler uses the bundled policy with
the cm emitter and the notify hook replaced by no-ops.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

import claude_pilot.permissions as permissions_module
from claude_pilot.permissions import _denial_is_terminal, create_permission_handler
from claude_pilot.tier1 import (
    _lex_shell_words,
    cargo_target_dir_value_is_literal,
    cargo_target_dir_values,
    is_tier1_auto_approve,
)

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)

_SPELLINGS = {
    "flag": "cargo build --target-dir {v}",
    "flag=": "cargo build --target-dir={v}",
    "env": "CARGO_TARGET_DIR={v} cargo build",
}

# The forms of the ticket's table, verbatim.
_TICKET_FORMS = [
    "cargo build --target-dir /home/samidarko/.ssh",
    "CARGO_TARGET_DIR=/home/samidarko/.ssh cargo build",
    "CARGO_TARGET_DIR=.claude/x cargo build",
    "CARGO_TARGET_DIR=$HOME/x cargo build",
    "CARGO_TARGET_DIR=~/.config/x cargo build",
    "CARGO_TARGET_DIR=../../other cargo build",
    "CARGO_TARGET_DIR=/tmp/t cargo build",
]

_ESCAPING = [
    "/home/samidarko/.ssh",
    "/etc/x",
    "/tmp/t",
    "../../x",
    "sub/../../x",
    "$HOME/x",
    "${HOME}/x",
    "~/.config/x",
    ".claude/x",
    ".git/x",
    "./.git/hooks",
    ".github/workflows/x",
]

_CONTAINED = [".pilot-scratch/t", "target-alt", "target/x", "./target-alt"]


def _cases(values: list[str]) -> list[Any]:
    return [
        pytest.param(form.format(v=v), id=f"{name}:{v}")
        for v in values
        for name, form in _SPELLINGS.items()
    ]


@pytest.fixture()
def decide(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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

    def run(cmd: str) -> PermissionResultAllow | PermissionResultDeny:
        r = asyncio.run(h("Bash", {"command": cmd}, ctx))
        assert isinstance(r, (PermissionResultAllow, PermissionResultDeny))
        return r

    return run


class TestRefused:
    @pytest.mark.parametrize("cmd", _TICKET_FORMS)
    def test_ticket_forms(self, decide, cmd: str) -> None:
        assert isinstance(decide(cmd), PermissionResultDeny)

    @pytest.mark.parametrize("cmd", _cases(_ESCAPING))
    def test_value_outside_the_worktree(self, decide, cmd: str) -> None:
        assert isinstance(decide(cmd), PermissionResultDeny)

    @pytest.mark.parametrize(
        "cmd",
        [
            "cargo build '--target-dir' /etc/x",
            'cargo build --target-dir "/etc/x"',
            "cargo test -p x --target-dir /etc/x --lib",
            "RUST_LOG=debug CARGO_TARGET_DIR=/etc/x cargo test",
            "cargo build && CARGO_TARGET_DIR=/etc/x cargo test",
            "cargo test -- --target-dir /etc/x",
            # bash would expand the glob; the value is not what the text shows
            "cargo build --target-dir tar*",
            # an empty value is not a contained directory
            "CARGO_TARGET_DIR= cargo build",
            # unterminated quote: the value cannot be read
            "cargo build --target-dir '/etc/x",
        ],
    )
    def test_other_spellings(self, decide, cmd: str) -> None:
        assert isinstance(decide(cmd), PermissionResultDeny)

    @pytest.mark.parametrize("name", list(_SPELLINGS))
    def test_symlink_leaving_the_worktree(
        self, decide, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, name: str
    ) -> None:
        outside = tmp_path_factory.mktemp("outside")
        (tmp_path / "esc").symlink_to(outside)
        assert isinstance(decide(_SPELLINGS[name].format(v="esc/t")), PermissionResultDeny)

    @pytest.mark.parametrize("cmd", ["CARGO_TARGET_DIR=/tmp/t cargo build",
                                     "cargo build --target-dir /home/samidarko/.ssh"])
    def test_refusal_is_survivable(self, decide, tmp_path: Path, cmd: str) -> None:
        # Lethality is left to the chain-safe veto's own judgment (cpp#248
        # point 3 is not decided here): these forms do not end the run.
        r = decide(cmd)
        assert isinstance(r, PermissionResultDeny)
        assert r.interrupt is False
        assert _denial_is_terminal("Bash", {"command": cmd}, str(tmp_path)) is False


class TestAdmitted:
    @pytest.mark.parametrize("cmd", _cases(_CONTAINED))
    def test_value_inside_the_worktree(self, decide, cmd: str) -> None:
        assert isinstance(decide(cmd), PermissionResultAllow)

    @pytest.mark.parametrize("name", list(_SPELLINGS))
    def test_absolute_path_inside_the_worktree(
        self, decide, tmp_path: Path, name: str
    ) -> None:
        cmd = _SPELLINGS[name].format(v=tmp_path / "target-alt")
        assert isinstance(decide(cmd), PermissionResultAllow)

    @pytest.mark.parametrize(
        "cmd",
        ["cargo build", "CARGO_INCREMENTAL=0 cargo build", "cargo test --target x86_64"],
    )
    def test_cargo_without_a_build_directory(self, decide, cmd: str) -> None:
        assert isinstance(decide(cmd), PermissionResultAllow)


class TestTier1:
    def test_build_directory_is_not_judged_by_tier1(self, tmp_path: Path) -> None:
        # tier1 has no worktree root to resolve against: the policy path decides.
        cmd = "cargo build --target-dir /etc/x"
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(tmp_path)) is False

    def test_plain_cargo_stays_tier1(self, tmp_path: Path) -> None:
        assert is_tier1_auto_approve("Bash", {"command": "cargo build"}, str(tmp_path)) is True


class TestValues:
    def test_values_are_collected_quote_removed(self) -> None:
        cmd = "CARGO_TARGET_DIR='a b' cargo build --target-dir \"c\" --target-dir=d"
        values = cargo_target_dir_values(cmd)
        assert values is not None
        assert [v.text for v in values] == ["a b", "c", "d"]

    def test_target_dir_before_cargo_is_not_cargos(self) -> None:
        assert cargo_target_dir_values("cp --target-dir /etc/x y && cargo build") == []

    def test_unlexable_segment(self) -> None:
        assert cargo_target_dir_values("cargo build --target-dir '/etc") is None

    @pytest.mark.parametrize(
        ("text", "literal"),
        [("target", True), ("/abs/x", True), ("", False), ("$X", False),
         ("~/x", False), ("a/../b", False), ("t*", False)],
    )
    def test_literal(self, text: str, literal: bool) -> None:
        words = _lex_shell_words(f"x {text}" if text else "x ''")
        assert words is not None
        assert cargo_target_dir_value_is_literal(words[1]) is literal
