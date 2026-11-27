"""Extraction de plage en ajout seul vers une bibliothèque de fixtures (cpp#328).

Élargissement borné de cpp#325. Une seule forme de plus est admise, seule sur
sa ligne, en ajout seulement :

    sed -n '<N>,<M>p' skills/bundled/_shared/test-dispatch-lib.sh >> skills/bundled/_shared/tests/lib-dispatch-lib-<nom>-fixtures.sh

AC1 : la forme est admise de bout en bout par le handler de production, sans
destination-veto. AC2 : dans le même test, chaque négatif du ticket est
refusé, et les négatifs d'AC2 de cpp#325 sont rejoués sur la nouvelle cible.
`>` vers ce nom reste refusé par la politique ET par le plan de contrôle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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

RULE_ID = "bash-sed-range-extract-3178-fixtures"
CPP325_RULE_ID = "bash-sed-range-extract-3178"
SRC = "skills/bundled/_shared/test-dispatch-lib.sh"
TESTS_DIR = "skills/bundled/_shared/tests"
TGT = f"{TESTS_DIR}/lib-dispatch-lib-groom-gate-fixtures.sh"
_CP = "is on the agent control plane (cpp#42 denylist)"

ADMITTED = (
    f"sed -n '2824,2855p' {SRC} >> {TGT}",
    f"sed -n '1,1p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-a_2-fixtures.sh",
    f"sed -n '10,20p' {SRC}  >>  {TESTS_DIR}/lib-dispatch-lib-x-y-fixtures.sh ",
)

# Négatifs propres à cpp#328.
REFUSED_328 = (
    # `>` vers un nom conforme : une bibliothèque commence par un en-tête
    # écrit par Write, et `>` l'écraserait.
    f"sed -n '1,5p' {SRC} > {TGT}",
    f"sed -n '1,5p' {SRC} >{TGT}",
    f"sed -n '1,5p' {SRC} >| {TGT}",
    f"sed -n '1,5p' {SRC} > > {TGT}",
    f"sed -n '1,5p' {SRC} >>> {TGT}",
    # une autre forme Bash vers un nom conforme
    f"cat x >> {TESTS_DIR}/lib-dispatch-lib-a-fixtures.sh",
    f"echo x >> {TGT}",
    f"sed -n '1,5p' {SRC} | tee -a {TGT}",
    # un autre nom sous tests/
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-suite.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-mika-bin.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-a.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-../x-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-..-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-A-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-a-fixtures.bash",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-a-fixtures.sh.bak",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/xlib-dispatch-lib-a-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-a-fixtures",
)

# Négatifs d'AC2 de cpp#325, rejoués sur la nouvelle cible et en `>>`.
REFUSED_325_REPLAYED = (
    # cible absolue
    f"sed -n '1,5p' {SRC} >> /srv/wt/{TGT}",
    # cible avec ..
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/../tests/lib-dispatch-lib-x-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/../../../../../x/lib-dispatch-lib-x-fixtures.sh",
    # cible hors de tests/
    f"sed -n '1,5p' {SRC} >> skills/bundled/_shared/lib-dispatch-lib-x-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/sub/lib-dispatch-lib-x-fixtures.sh",
    # cible d'un autre nom
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/test_callout_plan_identity_3174.sh",
    # source autre que l'agrégat
    f"sed -n '1,5p' skills/bundled/_shared/dispatch-lib.sh >> {TGT}",
    f"sed -n '1,5p' {SRC}.bak >> {TGT}",
    f"sed -n '1,5p' ./{SRC} >> {TGT}",
    # script sed qui écrit, exécute ou lit
    f"sed -n '1,5w x' {SRC} >> {TGT}",
    f"sed -n '1,5e' {SRC} >> {TGT}",
    f"sed -n '1r x' {SRC} >> {TGT}",
    f"sed -n 's/a/b/w x' {SRC} >> {TGT}",
    f"sed -n '1,5p;w x' {SRC} >> {TGT}",
    f"sed -n '1,5pw x' {SRC} >> {TGT}",
    # option -i, option -e, autre option
    f"sed -i -n '1,5p' {SRC} >> {TGT}",
    f"sed -n -i '1,5p' {SRC} >> {TGT}",
    f"sed -n -e '1,5p' {SRC} >> {TGT}",
    f"sed -n -E '1,5p' {SRC} >> {TGT}",
    f"sed '1,5p' {SRC} >> {TGT}",
    # chaînage
    f"sed -n '1,5p' {SRC} >> {TGT}; rm x",
    f"sed -n '1,5p' {SRC} >> {TGT} && x",
    f"sed -n '1,5p' {SRC} >> {TGT} || x",
    f"sed -n '1,5p' {SRC} | tee x",
    f"sed -n '1,5p' {SRC} >> {TGT} | tee x",
    f"sed -n '1,5p' {SRC} >> {TGT}\nrm x",
    f"sed -n '1,5p' {SRC}\n>> {TGT}",
    # substitution dans la plage ou dans la cible
    f"sed -n '$(echo 1),5p' {SRC} >> {TGT}",
    f"sed -n '1,`echo 5`p' {SRC} >> {TGT}",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-$(whoami)-fixtures.sh",
    f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/lib-dispatch-lib-$X-fixtures.sh",
    # cible entre guillemets
    f"sed -n '1,5p' {SRC} >> \"{TGT}\"",
    f"sed -n '1,5p' {SRC} >> '{TGT}'",
    # & final
    f"sed -n '1,5p' {SRC} >> {TGT} &",
    # heredoc, here-string, seconde redirection
    f"sed -n '1,5p' {SRC} >> {TGT} <<'EOF'",
    f"sed -n '1,5p' {SRC} >> {TGT} <<< x",
    f"sed -n '1,5p' {SRC} >> {TGT} 2> x",
    f"sed -n '1,5p' {SRC} >> {TGT} > x",
    f"sed -n '1,5p' {SRC} >> {TGT} >> x",
    # plage non décimale
    f"sed -n '1,$p' {SRC} >> {TGT}",
    f"sed -n '/a/,5p' {SRC} >> {TGT}",
    f"sed -n '-1,5p' {SRC} >> {TGT}",
)

REFUSED = REFUSED_328 + REFUSED_325_REPLAYED


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
    (wt / "skills" / "bundled" / "_shared" / "dispatch-lib.sh").write_text("x\n")
    return wt


def _handler(wt: Path):  # type: ignore[no-untyped-def]
    return create_permission_handler(
        config=None, relay=False, verbose=False, cwd=str(wt), policy_path=_BUNDLED
    )


# ── AC1 + AC2 : un seul test, positifs et négatifs ensemble ─────────────────


def test_cpp328_fixtures_append_admitted_and_neighbours_refused(
    tmp_path: Path,
) -> None:
    wt = _worktree(tmp_path)
    handler = _handler(wt)

    # AC1 : politique, chaîne sûre, plan de contrôle et handler de production.
    for cmd in ADMITTED:
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.decision == "allow", cmd
        assert d.rule_id == RULE_ID, (cmd, d.rule_id)
        assert _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True, cmd
        assert _segment_write_kind(cmd.strip()) == "bash-redirect", cmd
        assert _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID) is None, cmd
        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultAllow), (cmd, result)

    # AC2 : aucun négatif n'est admis, ni par la politique ni par le handler.
    for cmd in REFUSED:
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.rule_id not in (RULE_ID, CPP325_RULE_ID), (cmd, d.rule_id)
        effective = (
            d.decision == "allow"
            and _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True
        )
        assert effective is False, cmd
        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultDeny), cmd

    # `>` vers un nom conforme, refusé AUSSI au plan de contrôle, sous
    # chacun des deux rule_id.
    trunc = f"sed -n '1,5p' {SRC} > {TGT}"
    for rid in (RULE_ID, CPP325_RULE_ID):
        veto = _destination_veto_reason(trunc, str(wt), rule_id=rid)
        assert veto is not None and _CP in veto, (rid, veto)

    # Une autre forme Bash vers un nom conforme, même sous le rule_id neuf.
    other_form = f"cat x >> {TESTS_DIR}/lib-dispatch-lib-a-fixtures.sh"
    veto = _destination_veto_reason(other_form, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto

    # Les autres noms sous tests/, même en forme admise et sous le rule_id neuf.
    for name in (
        "lib-dispatch-lib-suite.sh",
        "lib-mika-bin.sh",
        "lib-dispatch-lib-a.sh",
    ):
        cmd = f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/{name}"
        veto = _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID)
        assert veto is not None and _CP in veto, (name, veto)

    # La forme admise sous un autre rule_id (ou aucun) : échec fermé.
    for rid in (
        None,
        CPP325_RULE_ID,
        "bash-git-show-redirect",
        RULE_ID + "-renamed",
        RULE_ID.upper(),
    ):
        for cmd in ADMITTED:
            veto = _destination_veto_reason(cmd, str(wt), rule_id=rid)
            assert veto is not None and _CP in veto, (rid, cmd, veto)

    # Les paires ne se croisent pas : la forme de cpp#325 vers sa propre cible
    # sous le rule_id neuf reste refusée.
    cpp325_form = f"sed -n '1,5p' {SRC} >> {TESTS_DIR}/test_dispatch_lib_a.sh"
    veto = _destination_veto_reason(cpp325_form, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto

    # La forme admise vers skills/bundled/_shared/dispatch-lib.sh, sous le
    # rule_id neuf.
    to_lib = f"sed -n '1,5p' {SRC} >> skills/bundled/_shared/dispatch-lib.sh"
    veto = _destination_veto_reason(to_lib, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto

    # Les appelants du chemin de létalité ne passent aucun rule_id.
    for cmd in ADMITTED:
        assert _destination_veto_reason(cmd, str(wt), for_lethality=True) is not None


def test_cpp328_symlinked_target_outside_worktree_refused_at_admission(
    tmp_path: Path,
) -> None:
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    (wt / "skills" / "bundled" / "_shared").mkdir(parents=True)
    (wt / SRC).write_text("x\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (wt / TESTS_DIR).symlink_to(outside, target_is_directory=True)

    handler = _handler(wt)
    for cmd in ADMITTED:
        d = evaluate(_POLICY, "Bash", _bash(cmd))
        assert d.decision == "allow" and d.rule_id == RULE_ID, cmd
        veto = _destination_veto_reason(cmd, str(wt))
        assert veto is not None and "outside the worktree" in veto, (cmd, veto)
        result = asyncio.run(handler("Bash", _bash(cmd), _mock_ctx()))
        assert isinstance(result, PermissionResultDeny), cmd
        assert result.message == veto, (cmd, result.message)
        assert result.interrupt is True, cmd
    assert list(outside.iterdir()) == []


def test_cpp328_exception_resolves_symlinks_before_matching(tmp_path: Path) -> None:
    """Une cible conforme qui est un lien vers l'agrégat, ou vers une cible de
    cpp#325, reste refusée : l'ancre porte sur le chemin résolu."""
    wt = _worktree(tmp_path)
    cmd = f"sed -n '1,5p' {SRC} >> {TGT}"
    (wt / TGT).symlink_to(wt / SRC)
    veto = _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto

    (wt / TGT).unlink()
    (wt / TESTS_DIR / "test_dispatch_lib_a.sh").write_text("x\n")
    (wt / TGT).symlink_to(wt / TESTS_DIR / "test_dispatch_lib_a.sh")
    veto = _destination_veto_reason(cmd, str(wt), rule_id=RULE_ID)
    assert veto is not None and _CP in veto, veto


def test_cpp328_exception_segment_shape_equals_the_yaml_rule() -> None:
    """La forme exigée par l'exception ne peut pas dériver de la règle."""
    from claude_pilot.permissions import _CPP328_FIXTURES_SEGMENT_RE

    rules = [r for r in _POLICY.rules if r.id == RULE_ID]
    assert len(rules) == 1
    assert rules[0].pattern == _CPP328_FIXTURES_SEGMENT_RE.pattern
