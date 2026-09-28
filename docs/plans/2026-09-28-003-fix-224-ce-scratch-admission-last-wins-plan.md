---
ticket: cpp#224
kind: fix
class: tightening (closes an admission; no new admission, no bearing)
status: ready-for-qa
---

# Rendre last-wins l'axe A d'ADMISSION du scratch ce-* — Plan

Resserrement du défaut préexistant repéré pendant cpp#223 (résidu suivi noté au
plan mika#2562). Reprend `docs/solutions/security-issues/ce-scratch-admission-must-be-last-wins-not-any-assignment.md`.

## Goal Capsule

L'axe A d'admission scratch ce-* (`_ce_scratch_variable_names` →
`_is_ce_scratch_variable_ref` → `permissions._is_sanctioned_tmp_scratch`)
reconnaît une variable si **une** affectation même-commande s'enracine dans un
littéral scratch `/tmp` — il n'est **pas** last-wins. Donc
`X=/tmp/ok; X=$HOME/evil; mkdir "$X"` est **ADMIS** (veto None) et **survivable**,
alors que sa valeur EFFECTIVE (dernière) est `$HOME/evil` (cible d'exfiltration).
**But** : rendre la reconnaissance last-wins → cette forme devient **refusée +
terminale** (veto cpp#218). Resserrement pur : aucune admission nouvelle.

## Cause établie à la source

Sondé sur la base (`9f6ea0c`, worktree git temporaire réel), `_denial_is_terminal`
+ `_destination_veto_reason(..., for_lethality=False)` :

| commande | admis (veto=None) | terminal | correct ? |
|---|---|---|---|
| `X=/tmp/ok; X=$HOME/evil; mkdir -p "$X"` | **True** | **False** | **NON** — dernière valeur `$HOME/evil` |
| `X=/tmp/ok; X=/tmp/still-ok; mkdir "$X"` | True | False | oui (dernière = scratch) |
| `SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; mkdir -p "$SCRATCH_ROOT"` | True | False | oui |

`_ce_scratch_variable_names` scannait toutes les affectations `/tmp` et ajoutait
le nom de var sur la **première** rencontre, sans revérifier la dernière valeur.

## Le fix (resserrement, admission)

`tier1.py` : `_ce_scratch_variable_names` prend les vars candidates (au moins une
affectation `/tmp`, via `_CE_SCRATCH_ASSIGN_RE`), puis pour chacune ré-résout la
**dernière** affectation toutes-valeurs (`_last_assignment_value`, landé par
cpp#223) et exige que CETTE valeur soit un scratch uid-tolérant
(`_is_uid_tolerant_tmp_scratch`). `_is_ce_scratch_variable_ref` inchangé (lit
toujours `var in _ce_scratch_variable_names(command)`). Effet :
`X=/tmp/ok; X=$HOME/evil; mkdir "$X"` → dernière = `$HOME/evil` → non reconnue →
veto cpp#218 → **refusée + terminale**.

## Hors portée / ce qu'on ne touche PAS

- **Chemin de LÉTALITÉ** (`_is_transitive_ce_scratch_mkdir_target` + son carve
  `for_lethality`-gaté, cpp#223) : déjà last-wins et correct — **inchangé**. Le
  préambule canonique ce-* reste survivable de bout en bout.
- **Gate tier1** `is_tier1_auto_approve` : jamais touché.
- Aucune nouvelle admission : le resserrement ne fait que **fermer** une
  admission → pas de bearing.

## Acceptance criteria

- [x] **AC1 — le négatif direct devient refusé + terminal.** `X=/tmp/ok; X=$HOME/evil; mkdir -p "$X"` → `_destination_veto_reason(..., for_lethality=False)` **non-None** ET handler `PermissionResultDeny` `interrupt=True`. → `TestCpp224AxisAAdmissionLastWins::test_reassign_out_of_scratch_now_refused_and_terminal`.
- [x] **AC2 — scratch→scratch pas sur-resserré.** `X=/tmp/ok; X=/tmp/still-ok; mkdir "$X"` → veto **None** (admis), handler survivable. → `test_scratch_to_scratch_reassign_still_admitted` + unit `TestCeScratchSanctionUnit::test_axis_a_names_are_last_wins` / `test_axis_a_var_ref_is_last_wins`.
- [x] **AC3 — positifs conservés.** affectation scratch unique, littéral standalone, `S="/tmp/ce-$UID"; mkdir "$S"` → toujours admis. → `test_single_scratch_assignment_stays_admitted` + tests mika#2562 inchangés verts.
- [x] **AC4 — préambule canonique reste survivable (non-terminal).** Chemin de létalité inchangé → `TestCeScratchCanonicalPreambleLethality` toujours vert (préambule + `mkdir "$RUN_DIR"` refusé mais `interrupt=False`).
- [x] **AC5 — non-régression.** cpp#143/#154 D3/#201/#209/#211/#218/#223 + mika#2562 inchangés ; suite complète verte ; jamais `is_tier1_auto_approve`.

## Fire-Disposition

- **Feu** : trou d'admission — un `mkdir` vers `$HOME/evil` (ou toute réassignation hors scratch) admis + survivable parce qu'une valeur scratch morte, plus ancienne, avait été reconnue. **Éteint** par ce correctif : la reconnaissance résout la dernière valeur ; la forme réassignée-hors-scratch redevient refusée + terminale.
- **Vérification de sortie de feu** : sonde source AVANT (admis+survivable) / APRÈS (refusé+terminal) sur `X=/tmp/ok; X=$HOME/evil; mkdir "$X"` ; suite complète + verify-pipeline verts.
- **Résidu suivi** : aucun. Le chemin de létalité (cpp#223) était déjà last-wins ; les deux axes (admission + létalité) sont maintenant alignés.

## Preuve / vérif (verbatim)

`uv run pytest` → **1515 passed** ; `uv run ruff check .` → All checks passed! ;
`uv run mypy src` → Success: no issues found in 23 source files ;
`./scripts/verify-pipeline.sh main-updated` → passed (docs+source).

## Tests basculés

Aucun test existant ne bascule : tous les tests mika#2562/#223 de l'axe A portent
sur des cas à **affectation unique** (dernière valeur = seule valeur), inchangés
par le passage en last-wins. Aucun test n'affirmait l'ancienne admission
non-last-wins d'une réassignation. Ajouts seulement (5 méthodes).

## Références

- Solution : `docs/solutions/security-issues/ce-scratch-admission-must-be-last-wins-not-any-assignment.md`.
- Base : mika#2562 (axe A admission) ; cpp#223 (`_last_assignment_value`, carve létalité last-wins). Régression d'origine : cpp#218 (veto `$`/`~`-rooté). Parent : cpp#143/#211.
