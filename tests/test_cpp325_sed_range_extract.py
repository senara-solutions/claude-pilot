"""Permission nommée d'extraction de plage pour la scission de mika#3178 (cpp#325).

Une seule forme est admise, seule sur sa ligne :

    sed -n '<N>,<M>p' skills/bundled/_shared/test-dispatch-lib.sh > skills/bundled/_shared/tests/test_dispatch_lib_<nom>.sh
    sed -n '<N>,<M>p' skills/bundled/_shared/test-dispatch-lib.sh >> skills/bundled/_shared/tests/test_dispatch_lib_<nom>.sh

AC1 : les deux formes sont admises par la politique, par
`_bash_allow_is_chain_safe` et par le handler de production, sous le `rule_id`
neuf. AC2 : chaque voisine de la liste du ticket est refusée. AC4 : la cible
passe par `_destination_veto_reason` au point d'admission ; une cible
symlinkée hors du worktree est refusée là, alors que le motif seul l'admet.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from claude_pilot.permissions import (
    _bash_allow_is_chain_safe,
    _destination_veto_reason,
    _segment_write_kind,
    create_permission_handler,
)
from claude_pilot.policy import evaluate, load_policy

_BUNDLED = Path(__file__).parent.parent / "src" / "claude_pilot" / "policies" / "permissions.yaml"
_POLICY = load_policy(_BUNDLED)

RULE_ID = "bash-sed-range-extract-3178"
SRC = "skills/bundled/_shared/test-dispatch-lib.sh"
TESTS_DIR = "skills/bundled/_shared/tests"
TGT = f"{TESTS_DIR}/test_dispatch_lib_ready_label.sh"

ADMITTED = (
    f"sed -n '120,348p' {SRC} > {TGT}",
    f"sed -n '120,348p' {SRC} >> {TGT}",
    f"sed -n '1,1p' {SRC} >> {TESTS_DIR}/test_dispatch_lib_a_2.sh",
)

REFUSED = (
    # cible absolue
    f"sed -n '1,5p' {SRC} > /srv/wt/{TGT}",
    # cible avec ..
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/../tests/test_dispatch_lib_x.sh",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/../../../../../x/test_dispatch_lib_x.sh",
    # cible hors de tests/
    f"sed -n '1,5p' {SRC} > skills/bundled/_shared/test_dispatch_lib_x.sh",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/sub/test_dispatch_lib_x.sh",
    # cible d'un autre nom
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/test_callout_plan_identity_3174.sh",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/test_dispatch_lib_X.sh",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/test_dispatch_lib_x.bash",
    # source autre que l'agrégat
    f"sed -n '1,5p' skills/bundled/_shared/dispatch-lib.sh > {TGT}",
    f"sed -n '1,5p' {SRC}.bak > {TGT}",
    f"sed -n '1,5p' ./{SRC} > {TGT}",
    # script sed qui écrit, exécute ou lit
    f"sed -n '1,5w x' {SRC} > {TGT}",
    f"sed -n '1,5e' {SRC} > {TGT}",
    f"sed -n '1r x' {SRC} > {TGT}",
    f"sed -n 's/a/b/w x' {SRC} > {TGT}",
    f"sed -n '1,5p;w x' {SRC} > {TGT}",
    f"sed -n '1,5pw x' {SRC} > {TGT}",
    # option -i, option -e, autre option
    f"sed -i -n '1,5p' {SRC} > {TGT}",
    f"sed -n -i '1,5p' {SRC} > {TGT}",
    f"sed -n -e '1,5p' {SRC} > {TGT}",
    f"sed -n -E '1,5p' {SRC} > {TGT}",
    f"sed '1,5p' {SRC} > {TGT}",
    # chaînage
    f"sed -n '1,5p' {SRC} > {TGT}; rm x",
    f"sed -n '1,5p' {SRC} > {TGT} && x",
    f"sed -n '1,5p' {SRC} > {TGT} || x",
    f"sed -n '1,5p' {SRC} | tee x",
    f"sed -n '1,5p' {SRC} > {TGT} | tee x",
    f"sed -n '1,5p' {SRC} > {TGT}\nrm x",
    f"sed -n '1,5p' {SRC}\n> {TGT}",
    # substitution dans la plage ou dans la cible
    f"sed -n '$(echo 1),5p' {SRC} > {TGT}",
    f"sed -n '1,`echo 5`p' {SRC} > {TGT}",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/test_dispatch_lib_$(whoami).sh",
    f"sed -n '1,5p' {SRC} > {TESTS_DIR}/test_dispatch_lib_$X.sh",
    # cible entre guillemets
    f"sed -n '1,5p' {SRC} > \"{TGT}\"",
    f"sed -n '1,5p' {SRC} > '{TGT}'",
    # & final
    f"sed -n '1,5p' {SRC} > {TGT} &",
    # heredoc, here-string, seconde redirection
    f"sed -n '1,5p' {SRC} > {TGT} <<'EOF'",
    f"sed -n '1,5p' {SRC} > {TGT} <<< x",
    f"sed -n '1,5p' {SRC} > {TGT} 2> x",
    f"sed -n '1,5p' {SRC} > {TGT} > x",
    # plage non décimale
    f"sed -n '1,$p' {SRC} > {TGT}",
    f"sed -n '/a/,5p' {SRC} > {TGT}",
    f"sed -n '-1,5p' {SRC} > {TGT}",
)


def _mock_ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None, suggestions=[], tool_use_id="tool_test", agent_id=None
    )


def _bash(cmd: str) -> dict[str, str]:
    return {"command": cmd}


def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    (wt / TESTS_DIR).mkdir(parents=True)
    (wt / SRC).write_text("x\n")
    return wt


# ── AC1 + AC2 : un seul test, positifs et négatifs ensemble ─────────────────


def test_cpp325_range_extract_admitted_and_neighbours_refused() -> None:
    for cmd in ADMITTED:
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.decision == "allow", cmd
        assert d.rule_id == RULE_ID, (cmd, d.rule_id)
        assert _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True, cmd
    for cmd in REFUSED:
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.rule_id != RULE_ID, cmd
        effective = (
            d.decision == "allow"
            and _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True
        )
        assert effective is False, cmd


def test_cpp325_admitted_through_the_production_handler(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    handler = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt), policy_path=_BUNDLED
    )
    for cmd in ADMITTED:
        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultAllow), (cmd, result)


@pytest.mark.parametrize("cmd", REFUSED[:12])
def test_cpp325_neighbours_refused_through_the_production_handler(
    tmp_path: Path, cmd: str
) -> None:
    wt = _worktree(tmp_path)
    handler = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt), policy_path=_BUNDLED
    )
    result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
    assert isinstance(result, PermissionResultDeny), cmd


def test_cpp325_sed_in_place_on_the_aggregate_still_refused() -> None:
    """La permission ne retire rien de l'agrégat : `sed -i` reste refusé."""
    for cmd in (
        f"sed -i '1,5d' {SRC}",
        f"sed -i -n '1,5p' {SRC}",
    ):
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert not (
            d.decision == "allow"
            and _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd))
        ), cmd


# ── AC4 : la cible est revalidée au point d'admission ───────────────────────


def test_cpp325_target_is_classified_as_a_write() -> None:
    """Sans cette classification, la cible ne serait vérifiée que par le motif
    (classe cpp#155 / cpp#174)."""
    for cmd in ADMITTED:
        assert _segment_write_kind(cmd) == "bash-redirect", cmd


def test_cpp325_symlinked_target_outside_worktree_refused_at_admission(
    tmp_path: Path,
) -> None:
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    (wt / "skills" / "bundled" / "_shared").mkdir(parents=True)
    (wt / SRC).write_text("x\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (wt / TESTS_DIR).symlink_to(outside, target_is_directory=True)

    handler = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt), policy_path=_BUNDLED
    )
    for cmd in ADMITTED[:2]:
        # Le motif seul admet la commande, et la chaîne est sûre : le refus ne
        # vient donc pas du motif.
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.decision == "allow" and d.rule_id == RULE_ID, cmd
        assert _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd), str(wt)) is True
        veto = _destination_veto_reason(cmd, str(wt))
        assert veto is not None, cmd

        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultDeny), cmd
        assert result.message == veto, (cmd, result.message)
        assert result.interrupt is True, cmd
    assert list(outside.iterdir()) == []


# ── AC7 + AC8 : l'exception au plan de contrôle (amendement du 2026-11-27) ──

_CP = "is on the agent control plane (cpp#42 denylist)"


def test_cpp325_control_plane_exception_admits_only_the_rule(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    (wt / "skills" / "bundled" / "_shared" / "dispatch-lib.sh").write_text("x\n")
    handler = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt), policy_path=_BUNDLED
    )

    # AC7 : le positif passe le plan de contrôle sous son propre rule_id, et
    # le handler de production l'admet sans destination-veto.
    for cmd in ADMITTED:
        assert _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID) is None, cmd
        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultAllow), (cmd, result)

    # AC8 (1) : une autre forme de redirection Bash vers une cible conforme.
    other_form = f"cat x > {TESTS_DIR}/test_dispatch_lib_a.sh"
    for rid in (evaluate(_POLICY, "Bash", _bash(other_form)).rule_id, RULE_ID):
        veto = _destination_veto_reason(other_form, str(wt), rule_id=rid)
        assert veto is not None and _CP in veto, (rid, veto)
    result = asyncio.run(handler("Bash", _bash(other_form), _mock_ctx()))
    assert isinstance(result, PermissionResultDeny), other_form

    # AC8 (2) : une redirection vers skills/bundled/_shared/dispatch-lib.sh,
    # même sous le rule_id de la règle.
    to_lib = f"sed -n '1,5p' {SRC} > skills/bundled/_shared/dispatch-lib.sh"
    veto = _destination_veto_reason(to_lib, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto
    result = asyncio.run(handler("Bash", _bash(to_lib), _mock_ctx()))
    assert isinstance(result, PermissionResultDeny), to_lib

    # AC8 (3) : la forme admise vers une cible conforme, sous un autre rule_id
    # (ou aucun) : le couplage échoue fermé.
    for rid in (None, "bash-git-show-redirect", RULE_ID + "-renamed", RULE_ID.upper()):
        for cmd in ADMITTED:
            veto = _destination_veto_reason(cmd, str(wt), rule_id=rid)
            assert veto is not None and _CP in veto, (rid, cmd, veto)
    # Les appelants du chemin de létalité ne passent aucun rule_id.
    for cmd in ADMITTED:
        assert _destination_veto_reason(cmd, str(wt), for_lethality=True) is not None


def test_cpp325_exception_segment_shape_equals_the_yaml_rule() -> None:
    """La forme exigée par l'exception ne peut pas dériver de la règle."""
    from claude_pilot.permissions import _CPP325_RANGE_EXTRACT_SEGMENT_RE

    rules = [r for r in _POLICY.rules if r.id == RULE_ID]
    assert len(rules) == 1
    assert rules[0].pattern == _CPP325_RANGE_EXTRACT_SEGMENT_RE.pattern


def test_cpp325_exception_resolves_symlinks_before_matching(tmp_path: Path) -> None:
    """L'ancre porte sur le chemin résolu : une cible conforme qui est un lien
    vers l'agrégat, ou un `tests/` lié vers `.git/hooks`, reste refusée."""
    wt = _worktree(tmp_path)
    tgt = wt / TGT
    tgt.symlink_to(wt / SRC)
    cmd = f"sed -n '1,5p' {SRC} > {TGT}"
    veto = _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto

    wt2 = tmp_path / "wt2"
    (wt2 / ".git" / "hooks").mkdir(parents=True)
    (wt2 / "skills" / "bundled" / "_shared").mkdir(parents=True)
    (wt2 / SRC).write_text("x\n")
    (wt2 / TESTS_DIR).symlink_to(wt2 / ".git" / "hooks", target_is_directory=True)
    veto = _destination_veto_reason(cmd, str(wt2), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto
    handler = create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt2), policy_path=_BUNDLED
    )
    result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
    assert isinstance(result, PermissionResultDeny) and result.interrupt is True
    assert list((wt2 / ".git" / "hooks").iterdir()) == []
