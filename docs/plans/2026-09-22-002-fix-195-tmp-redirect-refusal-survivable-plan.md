---
issue: claude-pilot#195
title: git show > /tmp/x refusé TERMINAL malgré cpp#154 — rendre le refus SURVIVABLE - Plan
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-22
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# git show > /tmp/x refusé TERMINAL malgré cpp#154 — rendre le refus SURVIVABLE - Plan

## Goal Capsule

**Objectif.** La commande de halt mika#2471 (rejeu MPC contre le classifier de
`main`, transcript `117d7a20`) est `git show origin/main:crates/mika-common/
src/home.rs > /tmp/ck_home_main.rs`. Elle est refusée (correct — écriture
hors worktree) mais TERMINALE — elle tue la session — alors que cpp#154 avait
déjà rendu ce type de redirect non létal pour la classe générique. Ce plan
établit la cause précise (elle diffère de l'hypothèse initiale du ticket sur
UN point de mécanisme, confirmé par sonde), pose la décision (a)/(b), et
répare — sans toucher au refus, seulement à sa létalité.

## Cause établie à la source (HEAD 4bc6105, sondée dans ce worktree)

`_denial_is_terminal` (`permissions.py:767`) est :

```python
if is_tier3_dangerous_for_lethality(command):
    return True
if _redirect_destination_veto_reason(command, cwd) is not None:
    return True
return _destination_veto_reason(command, cwd) is not None
```

Sonde (`_is_tier3_dangerous_for_lethality`, `_redirect_destination_veto_reason`,
`_destination_veto_reason`, `_denial_is_terminal`, `_segment_write_kind`) sur
la commande exacte de mika#2471 et ses contrôles, `cwd` = worktree temporaire :

| commande | t3_lethality | redirect_veto (whole-cmd) | dest_veto (per-write-kind) | **TERMINAL** | write-kind |
|---|---|---|---|---|---|
| `git show origin/main:home.rs > /tmp/ck_home_main.rs` (2471) | False (cpp#154) | **None** | **non-None** ← le bug | **True** | `bash-git-show-redirect` |
| `git show origin/main:x > /etc/ck.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `git show origin/main:x > /var/outside/ck.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `git show origin/main:x > ../escape.rs` | True | None | non-None | True (correct) | `bash-git-show-redirect` |
| `sed -i 's/a/b/' f` | True | None | None (kind=None) | True (correct, via tier3) | — |
| `rm -rf x` | True | None | None (kind=None) | True (correct, via tier3) | — |
| `echo hi > /tmp/scratch.txt` (control, non git-show) | False | None | **None** (already fixed) | **False** | `bash-redirect` |

**Correction assumée par rapport à l'hypothèse du ticket/dispatch.** Le
ticket cpp#195 et le message de dispatch supposaient que le veto TERMINAL
venait de `_redirect_destination_veto_reason` (le fallback whole-command que
cpp#154/#155/#176 documentent). La sonde le contredit : cette fonction rend
déjà `None` pour la commande #2471 — elle a bien le carve-out `/tmp/`. Le
vrai coupable est **`_destination_veto_reason`** (la fonction PER-WRITE-KIND,
`permissions.py:1088`), qui a deux branches spécifiques — `bash-mkdir`
(cpp#143) et `bash-redirect` (cpp#154/#155, avec le carve-out `/tmp/`) — mais
**aucune** pour `bash-git-show-redirect` (cpp#35/#128, antérieure à la
généralisation cpp#155). Un dest `bash-git-show-redirect` tombe directement
sur le check générique `is_within_project(dest, cwd)` quelques lignes plus
bas, qui rend `False` pour tout chemin hors worktree — y compris `/tmp/…` —
et vétote : `"destination '/tmp/ck_home_main.rs' resolves outside the
worktree (cpp#38 symlink-traversal containment)"`.

Le chemin d'exécution réel emprunté par la commande #2471 a aussi été vérifié
(pas supposé) : `policy.evaluate` la classe `allow, rule_id=bash-git-readonly`
(le pattern `bash-git-show-redirect`, plus strict, exige une cible RELATIVE —
`(?!/)` — et ne matche donc jamais une cible absolue `/tmp/…`; `evaluate` est
first-match-wins et retombe sur `bash-git-readonly`, un pattern non ancré qui
matche n'importe quel `git show …` quelle que soit la suite). `_bash_allow_is_
chain_safe` rend `False` pour cette commande (le motif `>` générique de
`is_tier3_dangerous` matche toujours sur ce chemin — cpp#154 ne touche QUE le
classificateur de létalité, jamais le classificateur de refus). La commande
passe donc par le site du **veto de chaîne** (`create_permission_handler`,
`permissions.py:1493-1520`), qui appelle `_denial_is_terminal` — PAS par le
site du veto de destination à `interrupt=True` littéral et inconditionnel
(`:1534-1560`, l'exception cpp#128 qui ne consulte jamais `_denial_is_
terminal`). Ce dernier site n'est atteignable pour `bash-git-show-redirect`
que via le rule_id `bash-git-show-redirect` lui-même, dont le pattern exige
une cible relative — une cible `/tmp/…` absolue n'y arrive donc jamais. C'est
la garantie qui rend le fix ci-dessous sûr (§ Décisions).

## Décision (a) vs (b)

**(a) — inconsistance, pas invariant ratifié.** Confirmé.

- cpp#154 a explicitement blanchi `is_tier3_dangerous_for_lethality` pour un
  redirect lexicalement contenu (`/tmp/…` ou relatif) — preuve directe
  d'intention : un research-write vers `/tmp` n'est pas létal.
- Le docstring de `_redirect_destination_veto_reason` (cpp#155) affirme que
  `_destination_veto_reason` est censée être "a STRICT SUPERSET of what this
  function proves for redirects" — c'était FAUX pour `bash-git-show-redirect`
  jusqu'à ce fix : c'est le bug, nommé dans le code lui-même sans avoir été
  fermé.
- Aucun document sous `docs/solutions/` (recherché : rien daté du 18/09 ni
  ailleurs sur "le classifier ne décide jamais sur le contenu" appliqué à ce
  cas précis) ne ratifie la létalité de l'échappement de confinement comme
  invariant indépendant du carve-out `/tmp` de cpp#154. Toute la trace écrite
  va dans l'autre sens.

Remède = le classifier (`_destination_veto_reason`), pas le contexte de
dispatch.

## Le fix

`src/claude_pilot/permissions.py`, dans `_destination_veto_reason`, la boucle
per-destination (`:1132` et suivantes). Avant :

```python
        for dest in dests:
            if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
                continue
            if kind == "bash-redirect":
                ...
```

Après (diff net, un seul branchement ajouté) :

```python
        for dest in dests:
            if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
                continue
            if (
                kind == "bash-git-show-redirect"
                and _is_contained_redirect_target(dest)
                and dest.startswith("/tmp/")
            ):
                continue
            if kind == "bash-redirect":
                ...
```

Le prédicat réutilisé, verbatim, est celui que `bash-redirect` (cpp#154/#155)
et `_redirect_destination_veto_reason` (cpp#154/#155/#176) utilisent déjà :
`_is_contained_redirect_target(dest) and dest.startswith("/tmp/")`. Rien n'est
réinventé. `is_tier3_dangerous` (le classificateur du REFUS) n'est pas
touché ; `_redirect_destination_veto_reason` n'est pas touché ; aucune règle
YAML n'est touchée.

**Pourquoi ce n'est PAS un élargissement de ce qui est admis.** La branche
n'est atteignable, en pratique, que depuis le site DENY (`_denial_is_
terminal`, via le veto de chaîne ou le refus par défaut) — jamais depuis le
site ALLOW à `interrupt=True` inconditionnel (`:1534`), qui n'atteint le
write-kind `bash-git-show-redirect` que via le rule_id du même nom, dont le
pattern YAML exige une cible relative. Une cible `/tmp/…` (absolue) ne peut
donc jamais porter ce rule_id et n'atteint ce site qu'en étant déjà refusée
par la policy. La commande reste refusée dans tous les cas ; seule sa
létalité change.

**Écart assumé par rapport au libellé initial du dispatch.** Le dispatch
demandait de ne toucher QUE `_denial_is_terminal`. La cause vérifiée montre
que le bug vit dans `_destination_veto_reason`, pas dans `_denial_is_
terminal` (qui se contente d'agréger trois verdicts déjà corrects sauf un).
Dupliquer le carve-out /tmp dans `_denial_is_terminal` sans toucher `_destination_
veto_reason` aurait recréé exactement la dérive que cpp#151 B0 et le docstring
de `_redirect_destination_veto_reason` mettent en garde ("two components
answering the same question and drifting apart is the exact failure mode
cpp#151 B0 closed") — et cela aurait aussi laissé `_destination_veto_reason`
elle-même incohérente avec sa propre documentation ("STRICT SUPERSET").
Corriger à la source évite la duplication et n'élargit rien (voir preuve du
paragraphe précédent). Jugement assumé, signalé ici pour revue.

## Preuve deux sens (both-directions)

**Positif (doit devenir NON-terminal) — replay exact #2471 :**
`_denial_is_terminal("Bash", {"command": "git show origin/main:crates/
mika-common/src/home.rs > /tmp/ck_home_main.rs"}, wt)` passe de `True`
(rouge, pré-fix) à `False` (vert, post-fix). Confirmé aussi au niveau handler
(`create_permission_handler`) : `PermissionResultDeny(interrupt=False)` — la
commande reste `PermissionResultDeny` (refusée), seul `interrupt` change.

**Négatif (doit rester terminal, les deux mondes) :**
`> /etc/ck.rs`, `> /var/outside/ck.rs` (hors worktree, non-/tmp), `>
../escape.rs` (traversal), `sed -i`, `rm -rf`, `git push --force` — tous
`_denial_is_terminal(...) is True` avant et après.

Tests : `tests/test_permissions.py`, section `cpp#195`
(`test_cpp195_destination_veto_git_show_tmp_carve_out`,
`test_cpp195_destination_veto_git_show_non_tmp_still_vetoed`,
`test_cpp195_denial_is_terminal_mika_2471_replay_survivable`,
`test_cpp195_negative_stays_terminal` (paramétré),
`test_cpp195_handler_end_to_end_still_denied_but_survivable`). Rouge-avant
capturé en stashant uniquement `permissions.py` et en rejouant `-k cpp195` :
3 échecs (`destination_veto_git_show_tmp_carve_out`,
`denial_is_terminal_mika_2471_replay_survivable`,
`handler_end_to_end_still_denied_but_survivable`), collés dans le corps de la
PR.

## Non-régression

Suite complète : `uv run pytest` — 1162 passed (aucune suite cpp#128/#151/
#154/#155/#166/#176 modifiée ni cassée). `uv run ruff check .` — clean.
`uv run mypy src` — clean.

## Hors portée

- `_GIT_SHOW_REDIRECT_DEST_RE`'s charset (`[\w./-]+`, sans `$`) ne supporte
  pas les cibles à expansion de paramètre (`/tmp/2158bodies/$n.md`, le résidu
  D3 de cpp#154) pour le write-kind `git show >` spécifiquement — c'est une
  propriété PRÉEXISTANTE et non liée à ce bug (elle vaut aussi bien pour
  `/tmp` que pour toute autre cible git-show) ; hors de la portée de cpp#195,
  qui ne porte que sur la létalité d'une cible /tmp DÉJÀ extractible.
- `git show ...:x > /dev/null` reste vétoté par `_destination_veto_reason`
  pour le write-kind `bash-git-show-redirect` (jamais couvert par le skip
  `/dev/null` de la branche `bash-redirect`, qui ne s'applique pas à ce
  write-kind) — préexistant, non touché, non demandé par ce ticket.

## Références

- `src/claude_pilot/permissions.py:767` — `_denial_is_terminal`.
- `src/claude_pilot/permissions.py:1088` — `_destination_veto_reason`, la
  fonction corrigée.
- `src/claude_pilot/permissions.py:1204` — `_redirect_destination_veto_reason`
  (déjà correcte, non touchée).
- `src/claude_pilot/tier1.py:355` — `_is_contained_redirect_target`, le
  prédicat réutilisé verbatim.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md` —
  cpp#154, le carve-out /tmp d'origine.
- `docs/plans/2026-08-30-002-fix-128-nonlethal-policy-denial-plan.md` —
  cpp#128, la distinction refus/létalité.
- `docs/plans/2026-09-04-001-fix-151-refus-non-letal-residuel-plan.md` —
  cpp#151 B0, "one lethality computation".
- claude-pilot#195, mika#2471, transcript `117d7a20`.
