---
title: "cpp#300 : `git push -…f` et `git branch -…D` lisent des jetons d'option, côté létalité - Plan"
type: fix
date: 2026-10-24
class: létalité seulement — aucune décision d'admission ne change
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# cpp#300 : drapeaux git ancrés sur des jetons d'option - Plan

## Goal Capsule

- **Objectif :** qu'un nom de branche en `-…f` ou un `echo "---AHEAD---"` voisin ne tue plus la session.
- **Moyen :** deux jumeaux létalité des motifs `git push -…f` / `git branch -…D`, ancrés sur un jeton d'option du segment de la commande git.
- **Arrêt :** si une décision d'admission bouge sur le corpus, ou si une ligne hors des deux formes change.

## Pourquoi

Le spawn de #278 a mesuré, sur 36 907 commandes réelles des pilotes (`~/.mika/data/pilot-transcripts/`), 5 sessions tuées par ces deux motifs alors qu'aucune ne poussait de force ni ne supprimait de branche :

- `git\s+push\s+.*-\w*f\b` prend le `-of` ou le `-rif` d'un nom de branche (3 morts : un `git push --dry-run origin …-terms-of-service-is`, deux `git push -u origin …`).
- `git\s+branch\s+.*-\w*D\b` prend le `-AHEAD` d'un `echo "---AHEAD---"` placé après `git branch --show-current` (2 morts).

La recherche s'applique à la commande entière, pas au segment : rien n'arrête `.*` au `;` ou au `&&` suivant, ni au milieu d'un argument positionnel.

## Ce que la mesure a montré avant d'écrire

Rejeu de base sur `origin/main` @ 0fd1551 (corpus : littéraux de `tests/` figés + transcripts + sondes, 41 445 commandes, deux cwd) :

| forme | état sur main |
|---|---|
| `git push -f`, `--force`, `-uf origin x`, `git branch -D x`, `-dD x` | refusées, terminales |
| `git push origin +main` | refusée, **non terminale** |
| `git push -fu origin x` | **admise** (le motif exige `f` en fin de groupe) |

Les deux dernières sont hors périmètre : les changer serait un autre changement de létalité ou d'admission. Elles sont marquées `xfail(strict=True)` et signalées.

Première version du correctif : ancrer les motifs eux-mêmes dans `TIER3_PATTERNS`. Le rejeu a montré 6 commandes passant de Deny à **Allow** (dont `git push -u origin <branche>`), parce que ces objets servent aussi à l'admission. Auto-approuver un push vers une branche nommée est une décision séparée. Le correctif final laisse l'admission sur les motifs non ancrés et ne remplace que leur entrée dans l'ensemble létal.

## Product Contract

- R1. Tests d'abord, commit seul : AC1 (5 verbatims non terminaux, rouges sur main), AC2 (`-f`, `--force`, `-uf`, `-D`, `-dD`, `-f` après la refspec, `"-f"` restent terminaux).
- R2. `_GIT_PUSH_F_OPTION_PATTERN` / `_GIT_BRANCH_D_OPTION_PATTERN` : le drapeau commence un mot (après un blanc, éventuellement derrière `$`, un guillemet et un antislash : `"-f"`, `$'-f'`, `\-f`), en finit un (avant un guillemet facultatif puis un blanc, un séparateur, `)`, un backtick ou la fin), et précède le prochain `;`/`|`/`&`/saut de ligne hors guillemets ; un span entre guillemets ou une paire `\X` se consomme d'un bloc, et le `&` d'une redirection (`2>&1`, `&>`) n'est pas un séparateur. Les alternatives du parcours sont disjointes (temps linéaire). Le drapeau lui-même reste `-\w*f` / `-\w*D`. (Révisé après la revue adversariale, qui a trouvé dans la première version `\-f`, `$'-f'`, le backtick, un `;` entre guillemets et un backtracking exponentiel sur `>&>&…`.)
- R3. `_TIER3_VERB_PATTERNS_FOR_LETHALITY` remplace ces deux entrées par leur jumeau, dans le même ordre. `TIER3_PATTERNS` (admission) n'a aucun diff de motif.
- R4. Le registre du hint référence les jumeaux (le test de dérive exige l'identité avec l'ensemble létal). La puce git dit maintenant qu'un nom de branche en `-…f` est refusé sans tuer, et garde `git push -u origin HEAD`.
- R5. AC3 : rejeu avant/après, seules les lignes des deux formes changent, `result`/`tier1`/`t3`/`message` ne bougent jamais, sha256 du reste identique.

## Implementation Units

- U1. `tests/test_tier1.py` : AC1, AC2, deux `xfail` stricts (commit seul).
- U2. `src/claude_pilot/tier1.py` : jumeaux, table `_LETHALITY_OPTION_TWINS`, registre, puce git.
- U3. `tests/test_tier1.py` : témoins du registre sur les jumeaux, test de la puce git (refus non terminal + substitut admis).

## Verification

- Rouge des tests sur main, puis mutations : jumeaux ramenés aux anciens motifs (AC1 rougit), drapeau `-\w*f` affaibli en `-\w+f` (AC2 rougit).
- Rejeu AC3 avec `ac3_replay.py` / `ac3_diff.py` (scratchpad de session, dérivés de `ac4_replay.py` de #278).
- `uv run --frozen --all-extras python -m pytest -q`, `ruff check`, `mypy src`, `scripts/verify-pipeline.sh origin/main`.
