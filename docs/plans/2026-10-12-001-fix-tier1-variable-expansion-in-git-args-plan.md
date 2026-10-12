---
title: "Le classifieur voit à travers l'évaluation shell dans les arguments git"
date: 2026-10-12
ticket: (ticket cpp à créer — non créé, voir corps de PR)
kind: fix
class: admission (refus de plus, aucune admission de plus) — tier1 + veto de la policy ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Le classifieur voit à travers l'évaluation shell dans les arguments git — Plan

## Goal Capsule

- **Objectif :** une commande git dont un argument n'a pas de valeur lisible dans le texte n'est plus admise automatiquement, ni par tier1, ni par la policy en aval.
- **Moyen :** un prédicat unique `git_argument_value_hidden` dans `src/claude_pilot/tier1.py`, consulté aux points d'admission git de tier1 et comme veto dans `permissions._bash_allow_is_chain_safe`.
- **Autorité :** décision Prime/Vincent du 2026-10-12 (bouchon local, passe AVANT le correctif F1 de mika#1686). Ratification de Vincent requise avant fusion : c'est une porte du classifieur.
- **Arrêt :** toute forme légitime fréquente qui passerait d'admise à refusée est signalée, pas avalée.

## Product Contract

### Problem Frame

Les gardes git du classifieur (`_MAIN_MASTER_RE`, `_FORCE_FLAG_RE`, `_GIT_DENIED_GLOBAL_FLAG_RE`, `_GIT_BRANCH_MUTANT_TOKENS`, `TIER3_PATTERNS`) lisent le texte brut de la commande. Le shell, lui, évalue ce texte avant que git ne le voie : expansions de variable, expansions à opérateur, paramètres spéciaux, retrait de quotes et d'antislash, globbing. Une valeur que la garde refuserait en littéral peut donc atteindre git sans jamais apparaître dans le texte lu par la garde.

Mesuré sur `main` 1581d74 avec le vrai `create_permission_handler`, la policy embarquée et `MIKA_PILOT_CONTAINED=1` : des push dont le refspec ou la remote est une expansion sont admis alors que le littéral équivalent est refusé ; une expansion à opérateur peut porter un drapeau refusé (`--output`) ; des assignations cachées (`printf -v`, `${X:=…}`, `$_`) alimentent un `$X` simple ; l'épissage de quotes contourne les mêmes gardes sans aucune variable.

Constat clé : quand tier1 refuse, la policy en aval (`bash-git-push`, `bash-git-readonly`) réadmet ces formes, car `_bash_allow_is_chain_safe` n'y oppose que `is_tier3_dangerous`, qui lit lui aussi le texte brut. Fermer tier1 seul ne changerait rien de bout en bout.

### Requirements

- R1. Toute sous-commande git qui écrit (hors lecture seule) refuse toute expansion `$` hors quotes simples, y compris dans la sous-commande elle-même.
- R2. Tout argument git refuse une expansion qui n'est pas une référence simple `$NAME` / `${NAME}` (NAME identifiant régulier, différent de `_`) : opérateurs, indirection, longueur, paramètres spéciaux et positionnels, `$(`, `$'`, `$"`.
- R3. Une assignation cachée dans la commande (`printf -v…` en position de commande, `${NAME=…}` / `${NAME:=…}`) refuse la commande entière.
- R4. `git push` refuse un glob ou une accolade non quotés, et ses gardes main/master et force s'appliquent aussi au texte après retrait des quotes. Dans tier1, les gardes force / drapeaux globaux / mutants de `branch` s'appliquent aussi au texte dé-quoté pour toutes les sous-commandes.
- R5. Le même prédicat est un veto du chemin policy (`_bash_allow_is_chain_safe`), évalué après la rédaction des substitutions allowlistées.
- R6. Refus = non-admission. Aucun nouveau chemin d'admission ; létalité inchangée (le refus reste non terminal).

### Scope Boundaries

- Préservé : `$NAME` / `${NAME}` simples dans les lectures git (`log`, `diff`, `show`, `rev-parse`, …) — forme F1 de mika#1686 ; messages de commit entre quotes sans `$` ; `$(git branch --show-current)` et les autres substitutions déjà allowlistées sur le chemin policy.
- Hors périmètre, résiduel déclaré : une variable d'environnement héritée du dispatcher et lue telle quelle dans une lecture git. Sa valeur n'est pas fixée par le pilote.
- Hors périmètre : le chemin `per_spawn` (bashlex, opt-in, mika#1708) et l'élargissement des règles YAML.

## Planning Contract

### Key Technical Decisions

- KTD1. Un mini-lexeur (`_lex_shell_words`) plutôt que `shlex` : il faut distinguer, mot par mot, ce qui est hors quotes simples (expansions), hors toute quote (glob) et le texte après retrait des quotes. `shlex` perd cette information. Une quote ou une accolade non fermée rend `None`, et `None` vaut refus.
- KTD2. Un seul prédicat public, consulté à deux couches. Tier1 et la policy ne peuvent pas diverger sur ce qu'est une valeur cachée.
- KTD3. Lecture seule = `SAFE_GIT_READONLY_SUBCOMMANDS` moins `branch`, qui porte des drapeaux mutants (`-D`, `-m`). Une expansion simple reste admise en lecture parce que R3 ferme les assignations cachées : sa valeur est alors soit héritée, soit assignée en clair dans la commande.
- KTD4. Le veto policy passe après la rédaction `$(…)` → `_SUB_` : les substitutions déjà sanctionnées restent un mot littéral, sans régression.

## Implementation Units

### U1. Lexeur et prédicat (`src/claude_pilot/tier1.py`)

- **Approche :** `_lex_shell_words(segment)` ; `_hidden_assignment(command)` (R3) ; `git_argument_value_hidden(command)` qui découpe en segments, repère chaque mot `git` non quoté et applique R1, R2 et R4.
- **Branchement tier1 :** `is_safe_git_command` (R1, R2, R4 et gardes dé-quotées), `_is_safe_git_readonly_sub`, `is_safe_bash_command` (R3 sur la commande entière, avant le chemin readonly-compound).
- **Tests :** `tests/test_tier1_git_value_hidden.py`.

### U2. Veto policy (`src/claude_pilot/permissions.py`)

- **Approche :** dans `_bash_allow_is_chain_safe`, après la rédaction des substitutions et avant les exceptions sanctionnées, `if git_argument_value_hidden(command): return False`.
- **Tests :** même fichier, au niveau handler (`create_permission_handler`, émetteurs neutralisés, aucun appel réseau ni GitHub).

### Test scenarios

- Négatifs, tier1 et handler : push par variable sous chaque forme (`$X`, `${X}`, `"$X"`, `HEAD:$X`, autre nom, remote par variable, force par variable) ; chaque opérateur (`:+`, `:-`, `/`, `#`, `!`, longueur) ; `$_`, `$1`, `$@` ; `printf -v` / `-vX` / `"-v"` suivi d'une utilisation ; `${X:=…}` dans un autre segment ; épissage de quotes et d'antislash sur main et sur force ; glob dans un push ; `checkout` / `branch` par variable.
- Positifs, à décision inchangée : `git status`, `git log --oneline -5`, `git diff $BASE HEAD`, `git diff ${BASE}`, `git push -u origin fix/foo`, `git push origin HEAD`, `git commit -m "fix [ci] thing?"`, `printf '%s\n' a`, le compound readonly contenu, `git push -u origin $(git branch --show-current)` sur le chemin policy.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Mutations terme par terme (R1, R2, R3, R4, chaque site de branchement), chacune vue ROUGE ; commit vert d'abord.
- Parité : corpus réel des transcripts pilotes et corpus de refus de mika#1686, ancien (1581d74) contre neuf via le handler. Chaque bascule admise → refusée est listée et justifiée ; aucune bascule refusée → admise n'est tolérée.

## Definition of Done

Commits locaux sur `fix/tier1-variable-expansion-in-git-args` ; `parity.md`, `mutations.md`, le corps de PR et le texte du ticket sont rédigés hors dépôt. Aucun push, aucune PR, aucun ticket.
