---
title: "cpp#278 AC3 dans les deux sens : chaque motif létal est nommé dans le hint ou exempté - Plan"
type: test
date: 2026-10-24
class: hint et registre seulement — aucune décision d'admission ni de létalité ne change
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# cpp#278 AC3 dans les deux sens - Plan

## Goal Capsule

- **Objectif :** qu'un motif létal ajouté à tier1 sans décision sur le hint fasse rougir la suite, en nommant le motif.
- **Moyen :** un test du sens manquant, puis le classement des 18 motifs hors registre (registre ou exemption avec raison).
- **Arrêt :** si une admission ou une létalité bouge sur le corpus, le travail s'arrête (AC4).

## Pourquoi

#281 a livré AC1 et AC2. `test_terminal_forms_hint_derived_from_tier1` vérifie que chaque entrée du registre est nommée dans le hint. Il ne vérifie pas que chaque motif létal relève du registre. MPC l'a mesuré le 2026-10-24 : un motif `perl -e` ajouté à `_TIER3_VERB_PATTERNS_FOR_LETHALITY` laisse les trois tests de dérive verts, et 18 des 23 motifs létaux sont hors du registre.

## Ce que la mesure a montré avant d'écrire

Sur 36 907 commandes Bash distinctes des transcripts de pilotes (`~/.mika/data/pilot-transcripts/*.jsonl`), passées par le vrai handler sur `origin/main` @ 8562cb1 :

| motif hors registre | appels qui le portent | appels tués |
|---|---|---|
| `git push .*--force` | 47 | 47 |
| `git push .*-\w*f` | 5 | 5 |
| `git reset --hard` | 16 | 16 |
| `git branch .*-\w*D` | 18 | 2 |
| `<(` | 13 | 6 |
| `DELETE FROM` | 1 | 1 |
| `>(` | 1 | 0 |
| `truncate` | 6 | 0 |
| les 10 autres | 0 | 0 |

`git push --force-with-lease` est terminal (`--force\b` le prend). Un `grep "DELETE FROM"` l'est aussi, même entre guillemets. Ce sont des formes qu'un pilote rencontre, et le hint ne les nommait pas.

## Product Contract

- R1. Test d'abord, dans son propre commit : chaque motif de l'union létale est soit une entrée verb-lethal du registre, soit dans `_TERMINAL_FORM_EXEMPTIONS` avec une raison. Le message nomme chaque motif non décidé.
- R2. Registre : famille git (5 motifs), substitution de processus (2), SQL (2), `sed --in-place` (rejoint `sed -i`). Une puce par famille. Le rendu émet chaque puce distincte une seule fois.
- R3. Exemptions avec raison : `cargo publish`, `gh label delete|edit`, `chmod -R`, `chown -R`, `dd`, `mkfs`, `truncate`, la fork bomb.
- R4. AC4 : les motifs sont nommés (constantes) sans changer ni leur regex, ni leurs flags, ni l'ordre de `TIER3_PATTERNS`. Le rejeu du corpus est byte-identique avant/après.
- R5. Chaque promesse nouvelle du hint est vérifiée par un test : un témoin par motif, terminal par `_denial_is_terminal`. Le hint ne promet aucun carve-out (revue P1 : une première version promettait « sauf un `<(…)` en lecture seule », alors que `diff <(sort a) <(sort b)` est terminal). Le substitut git `git push -u origin HEAD` est admis.

## Implementation Units

- U1. `tests/test_tier1.py` : `test_every_lethal_pattern_is_hinted_or_exempted` (commit seul, rouge sur main).
- U2. `src/claude_pilot/tier1.py` : constantes nommées, quatre puces de famille, `_TERMINAL_FORM_EXEMPTIONS`, rendu dédoublonné.
- U3. `tests/test_tier1.py` : témoins par motif, témoin procsub terminal, substitut git, cohérence des exemptions, ligne `pip install` figée (ajout du 2026-10-02).

## Verification

- Rouge du test sur main, puis rouge de la mutation `perl -e` sur une copie, puis vert.
- Rejeu AC4 : `ac4_replay.py` (scratchpad de la session) passe chaque commande du corpus (littéraux des tests + transcripts) par `create_permission_handler`, `is_tier1_auto_approve`, `is_tier3_dangerous`, `is_tier3_dangerous_for_lethality` et `_denial_is_terminal`, sur deux cwd. La sortie de la base et celle du correctif doivent être byte-identiques. Contrôle positif : la copie mutée doit diverger.
- `uv run --frozen --all-extras python -m pytest -q`, `ruff check`, `mypy src`, `scripts/verify-pipeline.sh origin/main`.
