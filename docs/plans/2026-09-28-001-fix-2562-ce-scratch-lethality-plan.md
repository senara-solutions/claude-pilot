---
ticket: mika#2562
kind: fix
class: lethality-only (regression of cpp#218)
status: ready-for-qa
---

# Rendre survivable le préambule scratch ce-* que cpp#218 a rendu fatal — Plan

Plan a posteriori (correctif passé hors pipeline /mika, par spawn direct sous P0 ; on garde la trace, pas d'exemption). Reprend `docs/solutions/security-issues/re-admitting-the-ce-tmp-scratch-a-uid-only-substitution-whitelist.md` + les deux revues MPC sur la PR.

## Goal Capsule

Un pilote meurt (`_denial_is_terminal = True`) au préambule standard des skills compound-engineering (ce-*), étape 6 de tout pipeline /mika. mika#2562 : pilote `b8f2b4b6` mort (55 tours, 11 USD), 1er pilote sous cpp a40493c. **But** : rendre le préambule complet **SURVIVABLE** (deny non-terminal = posture pré-#218), sans admettre plus. Létalité seule.

## Cause établie à la source

- cpp#218 a ajouté un veto `_destination_veto_reason` : `kind == "bash-mkdir" and dest.startswith("$"/"~")` → veto + terminal.
- Le préambule affecte `SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"` puis `RUN_DIR="$SCRATCH_ROOT/ce-code-review/$RUN_ID"` puis `mkdir -p "$RUN_DIR"`. `$RUN_DIR` s'enracine dans une **variable reconnue** (pas un littéral `/tmp`), donc l'axe A du 1er correctif (80c4238) ne la voit pas → veto #218 → **fatal**.
- **Ligne fautive** (MPC, par retrait ligne-à-ligne) : `mkdir -p "$RUN_DIR"`.
- **Vérité pré-#218 (commit 8335f1f, sondé)** : ce même compound était `deny / terminal=False / veto=None` = **survivable** (compound à assignation-en-tête = policy default-deny non-terminal ; le dir n'était jamais créé par cette commande). La régression #218 est **purement la terminalité**.

Table MPC (3 états × 2 formes) : avant #218 = survivable/survivable ; en service = fatal/fatal ; 1er correctif = **fatal**/survivable ; ce correctif = **survivable**/survivable.

## Le fix (létalité seule, `for_lethality`-gated)

`tier1.py` : `_is_transitive_ce_scratch_mkdir_target(command, dest)` — vrai si `dest` est un `$VAR` dont la **dernière** affectation même-commande (`_last_assignment_value`, LAST-WINS) s'enracine transitivement (`_value_roots_at_scratch`, profondeur bornée) dans un scratch reconnu : littéral uid-tolérant (`_is_uid_tolerant_tmp_scratch`), `${TMPDIR:-/tmp}/…` (`_is_tmpdir_default_scratch`), ou une autre variable reconnue + suffixe (sans `..`). `permissions.py` : une seule ligne comportementale, `for_lethality`-gatée, insérée avant le veto cpp#218 (miroir cpp#209) → `continue`. Le deny RESTE ; seul `_denial_is_terminal` bascule. **Admission byte-identique** (no-op sur tout appel `for_lethality=False`).

## Hors portée

Le négatif **direct** `X=/tmp/ok; X=$HOME/evil; mkdir "$X"` reste admis+survivable : l'axe A d'**admission** (préexistant, 80c4238) n'est pas last-wins. Défaut préexistant, resserrement séparé = **cpp#224** (spawn après merge). Non traité ici pour rester létalité-seule / admission byte-identique.

## Acceptance criteria

- [x] **AC1 — préambule canonique survivable, toujours refusé.** Replay `ce-canon.sh` (assignation + `$(id -u)` + umask/mkdir/chmod + RUN_ID + `mkdir "$RUN_DIR"`) → `_denial_is_terminal` **False**, `PermissionResultDeny` `interrupt=False`. → `TestCeScratchCanonicalPreambleLethality::test_canonical_preamble_is_survivable_but_still_refused`.
- [x] **AC2 — admission byte-identique.** `_destination_veto_reason(canon, cwd, for_lethality=False)` **non-None** (refus inchangé). → `test_admission_byte_identical_for_canonical`.
- [x] **AC3 — variante pilote survivable.** `ce-pilot4.sh` → non-terminal. → replay.
- [x] **AC4 (négatifs, deux sens) — restent fatals+refusés.** réassignation last-wins hors scratch (`SR=/tmp/ok; SR=$HOME/evil; RUN=$SR/x`), `$HOME/x`, `~/x`, var non-affectée, `$(whoami)`/`$(rm -rf /)`, `..`-suffixe, cycle. → `test_transitive_reassign_out_of_scratch_stays_fatal` + `TestCeScratchSanctionUnit::test_transitive_*`.
- [x] **AC5 — non-régression.** cpp#143/#154 D3/#201/#209/#211/#218 inchangés ; suite complète verte. Jamais tier1.

## Fire-Disposition

- **Feu** : mika#2562 (deny terminal du préambule ce-*), loop-breaker sur l'étape compound. **Éteint** par ce correctif : le préambule redevient survivable (posture pré-#218), le pilote ne meurt plus au rangement de son scratch.
- **Vérification de sortie de feu** : MPC re-rejoue `ce-canon.sh` + `ce-pilot4.sh` sur la tête (`_denial_is_terminal` False) avant merge ; puis redeploy + restart moteur n°10 + relance #2562.
- **Résidu suivi** : cpp#224 (axe A admission last-wins). Aucune réouverture d'admission ici.

## Preuve / vérif (verbatim)

`uv run pytest` → **1502 passed** ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main-updated` → passed (docs+source).

## Ratification

Admission (1er correctif) ratifiée Vincent 2026-09-28 14:38 (2e extension après cpp#207). CETTE correction = létalité seule, pas d'admission nouvelle → pas de bearing. Édition guardrail appliquée par SSC sur autorisation + validation directes de Vincent (blocage classifieur « Security Weaken »).

## Références

- Solution : `docs/solutions/security-issues/re-admitting-the-ce-tmp-scratch-a-uid-only-substitution-whitelist.md`.
- Miroir : cpp#201/#209 (`for_lethality`-gated carve). Régression : cpp#218. Parent : cpp#211/#143.
- Suivi : cpp#224 (resserrement axe A). PR : #223.
