---
title: "bash-git-readonly n'admet plus que de vraies lectures - Plan"
type: fix
date: 2026-10-22
class: admission (refus de plus, aucune admission de plus) — policy et tier1 ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# bash-git-readonly n'admet plus que de vraies lectures - Plan

## Goal Capsule

- **Objectif :** une commande admise par `bash-git-readonly` est une vraie lecture. Elle n'écrit rien, n'exécute aucun programme, et ne touche ni aux refs locales, ni à la configuration, ni aux worktrees.
- **Moyen :** une liste fermée de formes admises par sous-commande, consultée sur le chemin policy ; un prédicat unique pour la classe « exécute un programme », consulté par tier1 et par la policy ; une borne de mot sur la sous-commande de la règle.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** toute lecture git ordinaire qui passerait d'admise à refusée est signalée et expliquée, pas avalée.

## Product Contract

### Problem Frame

`bash-git-readonly` (`src/claude_pilot/policies/permissions.yaml`) admet ses sous-commandes par préfixe : `^git\s+(status|log|diff|show|branch|rev-parse|fetch|ls-remote|worktree|merge-base)`. La règle ne lit ni la fin du mot de sous-commande, ni ses options, ni ses arguments. `_bash_allow_is_chain_safe` ne rejoue pas les gardes d'options de tier1 sur un allow de la policy. Une sous-commande « de lecture » qui porte une option ou une forme d'écriture était donc honorée.

Vérifié sur `caf7e82`, sur git 2.53, dans des dépôts jetables, puis rejoué par `create_permission_handler` avec la policy embarquée :

- **Exécute un programme.** git exécute la valeur des options de transport qui nomment le programme distant (`fetch`, `ls-remote`, `pull`, `push`). La règle les admettait sous ses sous-commandes, et le garde de tier1 n'était pas rejoué sur le chemin policy.
- **Écrit une ref, la configuration ou un worktree.** `branch` hors de son mode liste (création, copie, renommage, suppression, réécriture forcée, suivi amont, description) ; `worktree` hors de `list` ; `fetch` avec une destination de refspec, une mise à jour forcée, ou l'écriture du suivi amont.
- **Lit hors du dépôt, ou lance un diff externe.** `diff` en mode hors index ; `diff`, `log` et `show` avec le diff externe explicite.
- **Préfixe sans borne.** Le motif admet tout mot qui commence par une des sous-commandes, y compris d'autres commandes git.

### Requirements

- R1. **Exécute un programme (les deux couches).** Un mot `git` suivi, dans le même segment et avant un `--` non quoté, d'un mot qui peut épeler, dans une orthographe que git accepte, une option dont git exécute la valeur (`--upload-pack`, `--receive-pack`, `--exec`), y compris un glob non quoté, n'est admis ni par tier1 (`is_safe_bash_command`) ni sur un allow de la policy (`_bash_allow_is_chain_safe`), indépendamment du `rule_id`.
- R2. **Liste fermée (chemin policy).** Un segment que tier1 refuse et que la policy admet, dont le premier mot est `git` et dont la sous-commande est l'une de la règle, n'est honoré que s'il est une forme admise :
  - `status`, `rev-parse`, `merge-base` : lectures quelles que soient leurs options ; pas d'entrée.
  - `log`, `diff`, `show` : aucun argument avant un `--` non quoté n'est `--ext-diff` ou `--no-index`, ni un glob non quoté qui peut les épeler. Ces sous-commandes n'ont pas d'autre mode que la lecture ; leurs centaines d'options d'affichage ne sont pas énumérées.
  - `diff`, en plus *(ajouté après revue)* : aucun argument n'est un chemin hors du dépôt (absolu, sous `~`, ou avec un composant `..`), avant ou après `--`. git passe seul en mode hors index dans ce cas (vérifié).
  - Un mot de sous-commande quoté ou échappé n'est pas honoré *(ajouté après revue)* : la sous-commande jugée doit être le mot même que git reçoit.
  - `branch` : options dans une liste fermée d'options d'affichage ; un argument positionnel n'est admis qu'en mode liste.
  - `worktree` : `list` seulement, avec ses options d'affichage.
  - `fetch` : options dans une liste fermée ; dépôt sans syntaxe d'assistant de transport ; refspecs source seule, sans `+` ni `:`, sans glob non quoté.
  - `ls-remote` : options dans une liste fermée ; dépôt sans syntaxe d'assistant de transport ; motifs libres.
- R3. **Borne de mot.** Le motif YAML n'admet la sous-commande que comme mot entier.
- R4. Aucune nouvelle admission. Létalité inchangée : le prédicat ne refuse que sur le chemin d'admission, `_denial_is_terminal` n'est pas touché.

### Scope Boundaries

- Préservé : les lectures ordinaires gardent leur décision. Les redirections ne sont pas jugées par la liste fermée ; elles restent jugées par les gardes existants du segment.
- `fetch` met à jour les refs de suivi distant : c'est son usage de lecture dans la boucle, et il reste admis sous sa forme ordinaire.
- La liste fermée vaut sur le chemin policy. Les admissions git de tier1 sont inchangées, sauf R1.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. **Liste fermée de formes plutôt que liste noire d'options**, là où le mode de lecture est petit (`branch`, `worktree`, `fetch`, `ls-remote`). Pour `log`, `diff` et `show`, qui n'ont qu'un mode de lecture et des centaines d'options d'affichage, la forme admise est « tout sauf les options vérifiées qui sortent de la lecture » ; énumérer leurs options d'affichage créerait des refus sans objet et dériverait avec git. Seule l'orthographe exacte de ces deux options est acceptée par git (vérifié) ; le glob est couvert.
- KTD2. **R1 en un prédicat pour les deux couches**, à côté de `git_argument_value_hidden`. Leçon des deux changements voisins : un garde qui ne vit que sur le chemin policy n'est jamais atteint quand tier1 admet le premier.
- KTD3. **R2 indépendant du `rule_id`** : il est keyé sur la sous-commande du segment, pas sur la règle qui l'a admis, pour qu'aucune règle antérieure ne le court-circuite.
- KTD4. **Lecture sur les mots après retrait des quotes** (`_lex_shell_words`), après masquage des redirections du segment (`_mask_quoted_redirect_chars` puis `_REDIRECT_RE`) : la liste fermée juge les arguments que git reçoit, pas les opérateurs de redirection.
- KTD5. Un segment que le lexeur ne sait pas suivre est laissé à `git_argument_value_hidden`, qui le refuse déjà sur les deux chemins quand il contient `git`. Le refuser ici créerait un mutant équivalent.
- KTD6. *(révisé après mutation)* `status`, `rev-parse` et `merge-base` n'ont pas d'entrée dans la table : git y rejette les deux options refusées, et leur entrée ne changeait aucune décision.
- KTD8. *(révisé après revue)* Sur une sous-commande de transport, R1 lit toute l'invocation, comme quand une option globale précède la sous-commande. Ailleurs, la lecture s'arrête au premier `--` non quoté.
- KTD9. `--output` sur `log`, `diff` et `show` reste au prédicat dédié déjà sur `main`, consulté sur les deux chemins indépendamment du `rule_id` ; il n'est pas répété ici. Vérifié après rebase.
- KTD7. La liste fermée ne décide que les segments que tier1 refuse (la boucle par segment accepte d'abord un segment sûr pour tier1). Ses tests passent par le handler quand c'est le cas, et par la couche policy seule, tier1 neutralisé, pour le reste.

## Implementation Units

### U1. Prédicats (`src/claude_pilot/tier1.py`)

- `git_option_names_a_program(command) -> bool` (R1).
- `git_read_overreaches(segment) -> bool` (R2), avec la table des formes admises.
- Branchement de R1 dans `is_safe_bash_command`, juste après `git_argument_value_hidden`.

### U2. Policy (`src/claude_pilot/permissions.py`, `policies/permissions.yaml`)

- `_bash_allow_is_chain_safe` : R1 juste après le veto `git_argument_value_hidden` ; R2 dans la boucle par segment, sur un segment que tier1 refuse et que la policy admet.
- Borne de mot sur le motif de `bash-git-readonly` (R3).

### Test scenarios (`tests/test_policy_git_readonly_reads_only.py`)

- Négatifs, à travers le handler complet, hors et sous confinement : chaque forme vérifiée de R1 et R2, seule et dans un compound.
- Positifs : les lectures ordinaires gardent leur décision, dont les formes réelles du corpus passées par la policy (`ls-remote` avec ses options, `rev-parse` sur l'amont, `fetch` d'une branche, redirections vers `/dev/null` ou `2>&1`).
- Prédicats : chaque clause et chaque branchement a un test qui rougit quand il est neutralisé seul.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Rouge-avant terme par terme sur le commit vert : chaque clause des prédicats, chaque entrée de table, la borne de mot, et chaque site de branchement. Mesuré : 55 mutants, tous rouges ; un mutant identique reste vert (contrôle). Trois termes survivants ont été retirés plutôt que gardés.
- Parité sur corpus réel : tous les appels Bash des transcripts pilotes, ancien (`origin/main` au rebase) contre neuf, handler complet, hors et sous confinement. Mesuré : 35 925 commandes distinctes, dont 6 947 contenant `git` ; 0 bascule refusé → admis ; 4 bascules admis → refusé, identiques hors et sous confinement : deux déplacements forcés d'une branche existante, deux retraits forcés d'un worktree. Ce sont des écritures de ref et de worktree, pas des lectures. Le refus est non terminal.

## Definition of Done

Commit local sur `fix/bash-git-readonly-n-admet-plus-que-de-vraies`, sans ticket public, titre et corps sobres, arrêt avant push pour relecture. Pas de merge.
