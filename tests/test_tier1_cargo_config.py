"""A cargo invocation that carries `--config` is not a safe build.

`cargo_carries_config` is the one predicate both layers consult — tier1's
`is_safe_build_command` and the policy chain-safe veto. Cargo's own
configuration files leave the `Write`/`Edit` auto-approval, and a Bash command
that may write into a `.cargo` directory (`bash_names_cargo_dir`) is refused by
tier1's git admission and by the same veto. Each clause and call site has a
test that turns red when it alone is removed (see the mutations in the PR).

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
from claude_pilot.permissions import (
    _bash_allow_is_chain_safe,
    create_permission_handler,
)
from claude_pilot.policy import load_policy
from claude_pilot.tier1 import (
    bash_names_cargo_dir,
    cargo_carries_config,
    is_safe_build_command,
    is_tier1_auto_approve,
)

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)
_POLICY = load_policy(_BUNDLED_POLICY)

# Every spelling of the flag cargo itself accepts, with and without the `env.`
# key, before and after the subcommand.
_CONFIG_FORMS = [
    "cargo test --config env.FOO=1",
    "cargo test --config=env.FOO=1",
    "cargo --config env.FOO=1 test",
    "cargo --config=env.FOO=1 test",
    "cargo build --config build.jobs=1",
    "cargo build --config=build.jobs=1",
    "cargo test -p x --config 'env.FOO=\"1\"' --lib",
    "cargo test --config cfg.toml",
    "cargo test --lib -- --config env.FOO=1",
    "cargo build && cargo test --config env.FOO=1",
    "RUST_LOG=debug cargo test --config env.FOO=1",
]

# The same flag once bash removes quotes and escapes.
_QUOTED_FORMS = [
    "cargo test '--config' env.FOO=1",
    'cargo test --"config"=env.FOO=1',
    "cargo test \\--config env.FOO=1",
]

# The cpp#242 admission corpus and ordinary cargo commands: decision unchanged.
_ADMITTED = [
    "CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=target-alt cargo test --no-run",
    "RUST_LOG=debug cargo build",
    "RUST_BACKTRACE=1 cargo test",
    "CARGO_TERM_COLOR=always cargo clippy",
    "cargo --version",
    "cargo metadata --format-version 1",
    "cargo build",
    "cargo test --workspace",
    "cargo test -p mika-agent --lib",
    "cargo test --test eval some_case",
    "cargo clippy --all-targets -- -D warnings",
    "cargo fmt --check",
    "cargo build && cargo test",
    "cargo build 2>&1",
    "cargo test -- --configure",
    "cargo build --target-dir target/x",
    "cargo test --features 'a b'",
    "cargo build --message-format=json",
]

# The cpp#242 refusal corpus: still refused.
_REFUSED_242 = [
    "PATH=/tmp cargo build",
    "HOME=/x cargo test",
    "LD_PRELOAD=/tmp/x.so cargo build",
    "HTTPS_PROXY= cargo build",
    "HTTP_PROXY= cargo build",
    "FOO=1 cargo build",
    "CARGO_INCREMENTAL=0 bash -c 'cargo build'",
    # cpp#248: an on-list name whose value leaves the worktree.
    "CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=/x cargo test --no-run",
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

    def decide(tool: str, tool_input: dict[str, str]) -> str:
        r = asyncio.run(h(tool, tool_input, ctx))
        if isinstance(r, PermissionResultAllow):
            return "allow"
        assert isinstance(r, PermissionResultDeny)
        return "deny"

    return decide


# ── Predicate clauses ────────────────────────────────────────────────────────


class TestLiteralFlag:
    @pytest.mark.parametrize("cmd", _CONFIG_FORMS + _QUOTED_FORMS)
    def test_flag_is_detected(self, cmd: str) -> None:
        assert cargo_carries_config(cmd) is True

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_cargo_is_not(self, cmd: str) -> None:
        assert cargo_carries_config(cmd) is False


class TestValueTheTextDoesNotShow:
    @pytest.mark.parametrize("cmd", ["cargo test $X env.FOO=1", "cargo test ${X} env.FOO=1"])
    def test_word_starting_with_an_expansion(self, cmd: str) -> None:
        assert cargo_carries_config(cmd) is True

    def test_option_name_carrying_an_expansion(self) -> None:
        assert cargo_carries_config("cargo test --con$X env.FOO=1") is True

    def test_option_name_carrying_a_glob(self) -> None:
        assert cargo_carries_config("cargo test --conf* env.FOO=1") is True

    def test_leading_glob(self) -> None:
        assert cargo_carries_config("cargo test * env.FOO=1") is True

    def test_brace_expansion(self) -> None:
        assert cargo_carries_config("cargo test --con{fig,x} env.FOO=1") is True

    def test_unlexable_segment(self) -> None:
        assert cargo_carries_config("cargo test --features 'a b") is True

    def test_line_continuation_inside_the_flag(self) -> None:
        assert cargo_carries_config("cargo test --con\\\nfig env.FOO=1") is True

    def test_path_qualified_cargo(self) -> None:
        assert cargo_carries_config("/usr/bin/cargo test --config env.FOO=1") is True

    def test_option_value_expansion_is_not_a_flag(self) -> None:
        assert cargo_carries_config("cargo build --target-dir=$PWD/target") is False

    def test_non_cargo_segment_is_ignored(self) -> None:
        assert cargo_carries_config("grep --config x && cargo build") is False


# ── Call sites ───────────────────────────────────────────────────────────────


class TestTier1BuildCommand:
    @pytest.mark.parametrize("cmd", [c for c in _CONFIG_FORMS if c.startswith("cargo test")])
    def test_config_is_not_a_safe_build(self, cmd: str) -> None:
        assert is_safe_build_command(cmd) is False

    @pytest.mark.parametrize("cmd", ["cargo build", "cargo test --workspace", "cargo clippy"])
    def test_ordinary_build_stays_safe(self, cmd: str) -> None:
        assert is_safe_build_command(cmd) is True


class TestPolicyVeto:
    @pytest.mark.parametrize("cmd", _CONFIG_FORMS)
    def test_policy_allow_is_not_honored(self, cmd: str) -> None:
        assert _bash_allow_is_chain_safe(_POLICY, "Bash", {"command": cmd}) is False

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_policy_allow_is_honored(self, cmd: str) -> None:
        assert _bash_allow_is_chain_safe(_POLICY, "Bash", {"command": cmd}) is True


class TestHandlerEndToEnd:
    @pytest.mark.parametrize("cmd", _CONFIG_FORMS + _QUOTED_FORMS)
    def test_config_is_refused(self, handler, cmd: str) -> None:
        assert handler("Bash", {"command": cmd}) == "deny"

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_cpp242_and_ordinary_cargo_unchanged(self, handler, cmd: str) -> None:
        assert handler("Bash", {"command": cmd}) == "allow"

    @pytest.mark.parametrize("cmd", _REFUSED_242)
    def test_cpp242_refusals_unchanged(self, handler, cmd: str) -> None:
        assert handler("Bash", {"command": cmd}) == "deny"


# ── Cargo configuration files ────────────────────────────────────────────────

_CONFIG_PATHS = [
    ".cargo/config.toml",
    ".cargo/config",
    "sub/dir/.cargo/config.toml",
    "./.cargo/../.cargo/config.toml",
    "sub/../.cargo/config.toml",
]


class TestWriteEditConfigPath:
    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    @pytest.mark.parametrize("path", _CONFIG_PATHS)
    def test_not_auto_approved(self, tmp_path: Path, tool: str, path: str) -> None:
        assert is_tier1_auto_approve(tool, {"file_path": path}, str(tmp_path)) is False

    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    def test_absolute_path_inside_worktree(self, tmp_path: Path, tool: str) -> None:
        path = str(tmp_path / ".cargo" / "config.toml")
        assert is_tier1_auto_approve(tool, {"file_path": path}, str(tmp_path)) is False

    def test_symlink_onto_a_config_file(self, tmp_path: Path) -> None:
        (tmp_path / ".cargo").mkdir()
        (tmp_path / ".cargo" / "config.toml").write_text("")
        (tmp_path / "alias.toml").symlink_to(tmp_path / ".cargo" / "config.toml")
        assert is_tier1_auto_approve("Write", {"file_path": "alias.toml"}, str(tmp_path)) is False

    @pytest.mark.parametrize("path", [".cargo/audit.toml", "src/lib.rs", "cargo/config.toml"])
    def test_other_files_stay_auto_approved(self, tmp_path: Path, path: str) -> None:
        assert is_tier1_auto_approve("Write", {"file_path": path}, str(tmp_path)) is True

    def test_handler_refuses_the_write(self, handler) -> None:
        assert handler("Write", {"file_path": ".cargo/config.toml", "content": ""}) == "deny"


# Every Bash shape that writes into a `.cargo` directory without naming the
# config file itself: a directory operand, a `mkdir`, a scratch-root heredoc
# that a later `cd` would put above the build, a flag value.
_CARGO_DIR_WRITES = [
    "cp cfg.toml .cargo/config.toml",
    "mv cfg.toml sub/.cargo/config",
    "cp sub/config.toml .cargo/",
    "cp sub/config.toml .cargo",
    "cp -t .cargo sub/config.toml",
    "mv dotcargo .cargo",
    "mv dotcargo sub/.cargo",
    "mkdir -p .cargo",
    "mkdir -p /tmp/x/.cargo",
    "cat > /tmp/x/.cargo/config.toml <<'EOF'\nx\nEOF",
    "cp --target-directory=.cargo sub/config.toml",
]


class TestCargoDirPredicate:
    @pytest.mark.parametrize("cmd", _CARGO_DIR_WRITES)
    def test_literal_component(self, cmd: str) -> None:
        assert bash_names_cargo_dir(cmd) is True

    @pytest.mark.parametrize("cmd", ["cp f .carg?/", "cp f .c*o/"])
    def test_unquoted_glob(self, cmd: str) -> None:
        assert bash_names_cargo_dir(cmd) is True

    def test_brace_expansion(self) -> None:
        assert bash_names_cargo_dir("cp f .{cargo,x}/") is True

    def test_assignment_value(self) -> None:
        assert bash_names_cargo_dir("D=.cargo") is True

    def test_unlexable_segment(self) -> None:
        assert bash_names_cargo_dir("cp 'x .cargo") is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "cp f *",                       # a bare glob never reaches a dotfile
            "cp f '.carg?'/",               # a quoted glob is literal
            "sed -n 's/=.*/x/p' f",         # nor is a quoted `.*`
            "cp f .cargo-old",
            "git show HEAD:.cargo/audit.toml",
            "for f in *.rs; do echo \"$f\"; done",
        ],
    )
    def test_other_words_are_not(self, cmd: str) -> None:
        assert bash_names_cargo_dir(cmd) is False


class TestCargoDirPolicyVeto:
    @pytest.mark.parametrize("cmd", _CARGO_DIR_WRITES)
    def test_policy_allow_is_not_honored(self, handler, cmd: str) -> None:
        assert handler("Bash", {"command": cmd}) == "deny"

    def test_heredoc_body_is_not_read(self, handler) -> None:
        cmd = "cat > /tmp/pr-body.md <<'EOF'\nfiles under .cargo/ stay closed\nEOF"
        assert handler("Bash", {"command": cmd}) == "allow"


class TestCargoDirTier1Git:
    @pytest.mark.parametrize(
        "cmd",
        ["git checkout HEAD -- .cargo", "git checkout HEAD -- .cargo/config.toml"],
    )
    def test_git_write_into_cargo_dir_is_not_auto_approved(
        self, tmp_path: Path, cmd: str
    ) -> None:
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(tmp_path)) is False

    @pytest.mark.parametrize("cmd", ["cat .cargo/config.toml", "git checkout HEAD -- src/main.rs"])
    def test_reads_and_other_git_stay_auto_approved(self, tmp_path: Path, cmd: str) -> None:
        assert is_tier1_auto_approve("Bash", {"command": cmd}, str(tmp_path)) is True
