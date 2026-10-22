"""`bash-git-readonly` admits only real reads.

A read writes nothing, runs no program, and touches neither local refs, config
nor worktrees. `git_option_names_a_program` refuses, on both layers, an option
whose value git runs as a program; `git_read_overreaches` holds the closed list
of forms the rule's subcommands are admitted in on a policy allow; the rule's
pattern takes the subcommand as a whole word. Each clause and each call site
has a test that turns red when it alone is removed.

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
    _segment_refusal_cause,
    create_permission_handler,
)
from claude_pilot.policy import evaluate, load_policy
from claude_pilot.tier1 import git_option_names_a_program, git_read_overreaches

_BUNDLED_POLICY = (
    Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
)

# Options whose value git runs as a program, in every spelling git accepts,
# and unquoted globs bash could expand into one.
_PROGRAM_FORMS = [
    "git fetch --upload-pack=X origin",
    "git fetch --upload-pack X origin",
    "git fetch --upload-pac=X origin",
    "git fetch --upl=X origin",
    "git pull --upload-pack=X origin main",
    "git ls-remote --upload-pack=X origin",
    "git ls-remote --exec=X origin",
    "git ls-remote --exec X origin",
    "git ls-remote --exe=X origin",
    "git ls-remote --u=X origin",
    "git push --receive-pack=X origin feat",
    "git push --exe=X origin feat",
    "git status && git fetch --upl=X origin",
    # `--` here is the value of the option before it, not the end of options.
    "git push -o -- --receive-pack=X origin feat",
    "git push --repo -- --exe=X origin feat",
    "git status && git push -o -- --receive-pack=X origin feat",
    "git fetch --upl*=X origin",
    "git ls-remote --e?ec=X origin",
]

# Not a read, refused by tier1 already; the policy re-admitted them.
_NOT_A_READ_FORMS = [
    "git branch -f feat HEAD",
    "git branch --force feat HEAD",
    "git branch -C a b",
    "git branch -m a b",
    "git branch -M a b",
    "git branch -d a",
    "git branch --delete a",
    "git branch --move a b",
    "git worktree remove --force x",
    "git worktree remove -f x",
    "git fetch -f origin main:main",
    "git fetch --force origin main:main",
    "git ls-remote 'ext::x'",
    "git ls-remote --heads origin -- main",
    "git status && git branch -f feat HEAD",
    # A quoted or escaped subcommand word that bash joins into another one.
    "git fetch''-pack --depth=1 ../other refs/heads/main",
    "git show\\-ref",
    "git diff''-files",
]

# The same, refused by tier1 in and out of containment.
_EXT_DIFF_FORMS = [
    "git diff --ext-diff",
    "git log -p --ext-diff",
    "git show --ext-diff HEAD",
    # Two paths, one outside the repository: git diffs them with no index.
    "git diff --no-ext-diff /etc/os-release /dev/null",
    "git diff --ext-dif? HEAD",
]

# Not one of the rule's subcommands: the prefix used to admit them.
_NOT_THE_SUBCOMMAND = [
    "git show-ref",
    "git statusx",
    "git diff-tree HEAD",
    "git fetch-pack origin",
]

# Not a read, judged on the policy path by the closed list. tier1 decides these
# on its own; tested with tier1 refusing every segment, the policy layer alone
# does not honor them.
_POLICY_REFUSES = [
    "git branch newname",
    "git branch -v newname",
    "git branch --copy a b",
    "git branch --set-upstream-to=origin/main",
    "git branch --edit-description",
    "git branch --contains HEAD -u origin/main",
    "git branch -f 'x>' HEAD",
    "git worktree add ../x -b y",
    "git worktree prune",
    "git worktree list --expire now",
    "git worktree",
    "git fetch origin main:feat",
    "git fetch origin +main",
    "git fetch --set-upstream origin main",
    "git fetch 'ext::x' main",
    "git fetch origin feat*",
    "git fetch --depth 1 origin",
    "git ls-remote --symref --foo origin",
    "git diff --no-index a b",
    "git diff ../outside.txt f",
    "git diff -- /etc/hostname /dev/null",
    "git diff ~/x f",
    "git diff a/../../x f",
    "git diff --ext-diff HEAD",
    "git diff --no-ind?x a b",
]

# Ordinary reads, including the real forms the corpus sends through the policy.
_ADMITTED = [
    "git status",
    "git status --short 2>&1",
    "git log --oneline -5",
    "git log --oneline --exclude=refs/stash --all -3",
    "git diff --stat",
    "git diff -U5 HEAD~1 HEAD",
    "git diff --no-ext-diff HEAD",
    "git diff -- --ext-diff",
    "git diff HEAD..main -- src/claude_pilot/tier1.py",
    "git diff main...HEAD --stat",
    "git diff --relative=src HEAD",
    "git show --stat HEAD",
    "git rev-parse --abbrev-ref HEAD",
    "git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>&1",
    "git merge-base HEAD origin/main",
    "git branch",
    "git branch --show-current 2>/dev/null",
    "git branch -a",
    "git branch -vv",
    "git branch --list 'feat/*'",
    "git branch --contains HEAD",
    "git branch --sort -committerdate --list 'feat/*'",
    "git branch --format '%(refname)' --merged main",
    "git branch --sort=-committerdate",
    "git branch --merged=main",
    "git worktree list",
    "git worktree list --porcelain",
    "git fetch",
    "git fetch origin",
    "git fetch --prune origin",
    "git fetch origin ci/2566/publish 2>&1",
    "git fetch https://github.com/senara-solutions/claude-pilot main",
    "git ls-remote origin",
    "git ls-remote --heads origin main 2>&1",
    "git ls-remote --exit-code origin \"refs/heads/main\" 2>&1",
    "git ls-remote --get-url origin",
    "git ls-remote -h origin",
    "git ls-remote origin 'refs/heads/fix/*' 2>&1",
    "git ls-remote --sort -version:refname --tags origin",
    "git ls-remote --sort=-version:refname origin",
    "git ls-remote origin 'x::y'",
    "git status && git ls-remote --heads origin main",
]


@pytest.fixture(params=[False, True], ids=["uncontained", "contained"])
def handler(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for var in ("MIKA_GATEWAY_URL", "MIKA_INTERNAL_TOKEN", "MIKA_PILOT_POLICY_PATH",
                "MIKA_PERMISSION_POLICY_MODE", "MIKA_PILOT_POLICY_DISABLED",
                "MIKA_PILOT_CONTAINED"):
        monkeypatch.delenv(var, raising=False)
    if request.param:
        monkeypatch.setenv("MIKA_PILOT_CONTAINED", "1")
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


@pytest.fixture(scope="module")
def policy():
    return load_policy(_BUNDLED_POLICY)


class TestProgramPredicate:
    @pytest.mark.parametrize("cmd", _PROGRAM_FORMS)
    def test_detected(self, cmd: str) -> None:
        assert git_option_names_a_program(cmd) is True

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_read_is_not(self, cmd: str) -> None:
        assert git_option_names_a_program(cmd) is False

    def test_only_a_git_invocation_counts(self) -> None:
        assert git_option_names_a_program("echo --upload-pack=X") is False

    def test_an_option_that_cannot_spell_one_is_not(self) -> None:
        assert git_option_names_a_program("git fetch --no-upload-pack origin") is False
        assert git_option_names_a_program("git log --=x") is False

    def test_a_glob_that_cannot_spell_one_is_not(self) -> None:
        assert git_option_names_a_program("git log --grep=fix* -1") is False

    def test_reading_stops_at_an_unquoted_double_dash(self) -> None:
        assert git_option_names_a_program("git diff -- --exec") is False
        assert git_option_names_a_program("git log '--' --exec") is True

    def test_a_transport_subcommand_is_read_whole(self) -> None:
        assert git_option_names_a_program("git fetch origin -- --upl=X") is True

    def test_a_global_option_before_the_subcommand_reads_it_whole(self) -> None:
        assert git_option_names_a_program("git --no-pager push -o -- --exe=X origin feat") is True

    def test_every_segment_is_read(self) -> None:
        assert git_option_names_a_program("echo x; git fetch --upl=X origin") is True
        assert git_option_names_a_program("git status # note\ngit fetch --upl=X origin") is True


class TestReadFormPredicate:
    # The predicate judges one segment; compounds go through the handler.
    @pytest.mark.parametrize(
        "cmd", [c for c in _NOT_A_READ_FORMS if "&&" not in c] + _EXT_DIFF_FORMS + _POLICY_REFUSES
    )
    def test_not_a_read(self, cmd: str) -> None:
        assert git_read_overreaches(cmd) is True

    @pytest.mark.parametrize("cmd", _ADMITTED[:-1])
    def test_read(self, cmd: str) -> None:
        assert git_read_overreaches(cmd) is False

    def test_only_a_git_segment_counts(self) -> None:
        assert git_read_overreaches("echo branch -f x HEAD") is False
        assert git_read_overreaches("git commit -m x") is False

    def test_reading_stops_at_an_unquoted_double_dash(self) -> None:
        assert git_read_overreaches("git diff -- --ext-diff") is False
        assert git_read_overreaches("git diff '--' --ext-diff") is True

    def test_pure_reads_take_any_option(self) -> None:
        assert git_read_overreaches("git status --porcelain -uno") is False
        assert git_read_overreaches("git rev-parse --verify --quiet HEAD") is False


class TestHandler:
    @pytest.mark.parametrize("cmd", _PROGRAM_FORMS + _NOT_A_READ_FORMS + _NOT_THE_SUBCOMMAND)
    def test_refused(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"

    @pytest.mark.parametrize("cmd", _EXT_DIFF_FORMS)
    def test_refused_in_and_out_of_containment(self, handler, cmd: str) -> None:
        assert handler(cmd) == "deny"

    @pytest.mark.parametrize("cmd", _ADMITTED)
    def test_ordinary_read_is_admitted(self, handler, cmd: str) -> None:
        assert handler(cmd) == "allow"


class TestPolicyPath:
    """The policy layer on its own: tier1 refuses every segment."""

    @pytest.fixture()
    def no_tier1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(permissions_module, "is_safe_bash_command", lambda *_a, **_k: False)

    @pytest.mark.parametrize("cmd", _POLICY_REFUSES + _EXT_DIFF_FORMS[:-1])
    def test_policy_allow_is_not_honored(self, policy, no_tier1, cmd: str) -> None:
        assert evaluate(policy, "Bash", {"command": cmd}).decision == "allow"
        assert _bash_allow_is_chain_safe(policy, "Bash", {"command": cmd}) is False

    @pytest.mark.parametrize("cmd", [c for c in _ADMITTED if "2>" not in c])
    def test_read_is_honored(self, policy, no_tier1, cmd: str) -> None:
        assert _bash_allow_is_chain_safe(policy, "Bash", {"command": cmd}) is True

    @pytest.mark.parametrize("cmd", _NOT_THE_SUBCOMMAND)
    def test_rule_takes_the_subcommand_as_a_whole_word(self, policy, cmd: str) -> None:
        assert evaluate(policy, "Bash", {"command": cmd}).rule_id != "bash-git-readonly"

    def test_refusal_diagnosis_names_the_segment(self, policy, tmp_path: Path) -> None:
        assert _segment_refusal_cause(policy, "git branch -f feat HEAD", str(tmp_path)) == "chain-unsafe"
        assert _segment_refusal_cause(policy, "git ls-remote origin", str(tmp_path)) is None
