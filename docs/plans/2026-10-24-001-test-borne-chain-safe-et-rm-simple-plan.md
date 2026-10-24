---
title: "Les deux tests qui manquent à cpp#250 AC2 et cpp#237 AC2 - Plan"
type: test
date: 2026-10-24
class: tests seulement — aucun changement de src/, aucune décision ne change
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Les deux tests qui manquent à cpp#250 AC2 et cpp#237 AC2 - Plan

## Goal Capsule

- **Objectif :** figer par un test les deux parts d'AC déjà livrées par #251 et #249, que rien ne protège encore.
- **Moyen :** étendre deux tests existants, sans toucher à `src/`.
- **Arrêt :** si un test révèle que main se trompe, le travail s'arrête et le défaut est rapporté, sans correctif.

## Product Contract

### Requirements

- R1. **cpp#250 AC2.** Le test de borne couvre aussi `_bash_allow_is_chain_safe` et la commande **entière** du constat (`…; wc -l .pilot-scratch/probe-block.txt`), avec des variantes à long chemin. Le seuil reste 50 ms.
- R2. **cpp#237 AC2.** Les négatifs de la boucle d'attente portent aussi le cas `rm` simple (`rm .pilot-scratch/x`), à côté de `rm -rf`, et il reste terminal.
- R3. Chaque test mord. Une mutation en mémoire de `src/` le fait rougir, et `src/` n'est jamais commité modifié.

## Ce que la mesure a montré avant d'écrire

**Sur l'axe `_bash_allow_is_chain_safe`, la commande du constat ne mord pas.** Avec l'ancienne regex de `_SAFE_SED_PRINT_RE` (d'avant 216033d) remise en mémoire, cet axe rend la commande du pilote en 0,76 ms. Avec un chemin de 200 caractères suivi de `>`, il rend en 1,61 ms. La raison : `_bash_allow_is_chain_safe` passe par `is_tier1_auto_approve`, où `is_tier3_dangerous(command)` est vrai dès qu'il y a un `>`. Le contrôle s'arrête alors avant `_is_safe_sub_command`, qui seul appelle la regex. Sur cet axe, une forme qui atteint la regex est un chemin suivi d'un caractère hors classe qui n'est pas une redirection, par exemple `crates/…/mod.rs:1139`. Avec l'ancienne regex, l'axe ne rend pas en 8 s.

Le test garde donc les deux formes du constat, la verbatim et sa variante à long chemin, sur les quatre axes. Il y ajoute la variante `:LINE`, qui est la seule à faire mordre la part `_bash_allow_is_chain_safe`. Cette variante figure aussi en version à long chemin.

**Pour cpp#237, `rm` est terminal par absence.** Aucun jeu de refus ne nomme `rm`. Il manque à l'allowlist `_WAITLOOP_READONLY_CMDS`, donc `is_readonly_waitloop_script` est faux et la létalité de `sh -c` reste. La mutation fidèle est donc d'**ajouter** `rm` à ce jeu : le cas devient survivable et le test rougit.

## Implementation Units

- U1. `tests/test_tier1.py` : deux commandes entières (`_REDOS_VERBATIM_FULL`, `_REDOS_LONG_PATH_FULL`), deux variantes `:LINE` (`_REDOS_CHAIN_SAFE_REACH`, `_REDOS_CHAIN_SAFE_REACH_LONG`), et `_bash_allow_is_chain_safe` avec la politique embarquée dans `test_sed_print_only_redos_axes_are_bounded_time`.
- U2. `tests/test_permissions.py` : le cas `rm .pilot-scratch/x` dans les négatifs AC2 de `test_cpp237_readonly_waitloop_is_survivable_but_still_refused`, placé avant `rm -rf`.

## Verification

- Mutation R1 : l'ancienne regex est remise dans `tier1._SAFE_SED_PRINT_RE` par une copie du test (monkeypatch). La part `_bash_allow_is_chain_safe` doit dépasser 50 ms. On a la sortie mot pour mot.
- Mutation R2 : `rm` est ajouté à `_WAITLOOP_READONLY_CMDS` sur une copie du test (monkeypatch). Le cas `rm` simple doit rougir.
- `uv run --frozen --all-extras python -m pytest -q`, `ruff check`, `ruff format --check`, `mypy src`.
