---
title: "tier1 n'admet plus que les vraies lectures git - Plan"
type: fix
date: 2026-10-22
class: admission (refus de plus, aucune admission de plus) — tier1 ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# tier1 n'admet plus que les vraies lectures git - Plan

## Goal Capsule

- **Objectif :** une sous-commande git de lecture que tier1 auto-approuve est une vraie lecture, comme sur le chemin policy depuis la liste fermée de `bash-git-readonly`.
- **Moyen :** tier1 consulte la même liste fermée, `git_read_overreaches`, réutilisée telle quelle, sur chaque segment de la commande.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** toute bascule admis → refusé du corpus réel est nommée et pesée. Une bascule refusé → admis arrête le travail.

## Product Contract

### Problem Frame

La liste fermée des formes de lecture (`tier1.git_read_overreaches`) ne vaut aujourd'hui que sur un allow de la policy. tier1 ne la consulte pas. Ce qu'il admet comme sûr ne passe donc pas par elle, qu'il décide seul ou à l'intérieur d'une chaîne jugée par la policy.

Vérifié sur `dfcf5f5` par rejeu à travers `create_permission_handler`, hors et sous confinement : tier1 admet des formes de `branch`, `worktree`, `fetch`, `diff`, `log` et `show` que la liste fermée refuse.

### Requirements

- R1. **Toute admission de tier1.** `is_safe_bash_command` refuse une commande dont un segment, découpé comme bash l'exécute, est une forme que `git_read_overreaches` refuse. C'est vrai sur tous les chemins qui passent par lui : le chemin simple de tier1, la boucle par segment de `_bash_allow_is_chain_safe`, le chemin composé sous confinement et la sonde de revue (`BASE=`), hors et sous confinement.
- R2. **Même liste.** Le prédicat de la policy est appelé, pas copié. Sa table n'est pas modifiée.
- R3. **Aucune admission nouvelle.** Létalité inchangée : `_denial_is_terminal` n'est pas touché.

Ce que la règle resserre, pour ses sous-commandes et sur tous les chemins de tier1 :

- `branch` en mode liste seulement ;
- `worktree list` seulement ;
- `fetch` sans destination, sans mise à jour forcée, avec les seules options de la liste ;
- `log`, `diff` et `show` sans diff externe, et `diff` sans chemin hors du dépôt.

### Scope Boundaries

- Préservé : la liste fermée ne vise que les sous-commandes de lecture de `bash-git-readonly`. Les autres gardent leur décision.
- La table des formes admises reste celle de `bash-git-readonly`. L'élargir pour une bascule serait une admission nouvelle sur le chemin policy.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. **Un seul veto, à l'entrée de `is_safe_bash_command`**, à côté de `git_argument_value_hidden`, `git_option_writes_a_file` et `git_option_names_a_program`. Tous les chemins d'admission de tier1 passent par cette fonction : `is_tier1_auto_approve` l'appelle, et `_bash_allow_is_chain_safe` aussi, pour chaque segment. Un veto placé côté policy ne couvrirait pas le chemin simple. C'est la leçon déjà notée pour `git_option_names_a_program` : un garde qui ne vit que sur un chemin n'est jamais atteint quand l'autre admet le premier.
- KTD2. **Le veto juge les segments tels que bash les exécute** : `_split_compound_command`, qui respecte les quotes, comme `git_option_names_a_program`. Il ne réutilise pas les découpages propres au chemin composé ou à la sonde. Ce sont des filtres d'admission, et ils ne garantissent pas que chaque fragment est un mot que git reçoit.
- KTD3. **Pas de veto supplémentaire dans `is_safe_git_command` ni dans `_is_safe_git_readonly_sub`.** Ils ne sont atteints que par `is_safe_bash_command`, après le veto. Un second site serait un terme qu'aucun test ne peut faire rougir.
- KTD4. **Les bascules du corpus sont acceptées, pas compensées.** Aucune ne lit. Chacune est pesée dans le Verification Contract, avec ce que le pilote perd et ce qui lui reste.

## Implementation Units

### U1. Veto de lecture dans tier1 (`src/claude_pilot/tier1.py`)

**Goal :** R1, R2, R3.
**Files :** `src/claude_pilot/tier1.py`, `tests/test_tier1_git_reads_only.py`.
**Approach :**
1. `is_safe_bash_command` : après `git_option_names_a_program`, refuser quand `git_read_overreaches` est vrai pour un segment de `_split_compound_command(command)`. Le découpeur ne rend une liste vide que pour une commande sans segment, qui ne contient aucun mot `git` : aucun repli n'est nécessaire.
2. Mettre à jour le commentaire de section de `git_read_overreaches`, qui dit aujourd'hui que le prédicat n'est consulté que sur un allow de la policy.

**Patterns to follow :** `git_option_names_a_program` (boucle sur `_split_compound_command(command) or [command]`, appel dans `is_safe_bash_command`) ; les tests via le handler de `tests/test_policy_git_readonly_reads_only.py`.

**Test scenarios :**
- Négatifs, à travers le handler complet, hors et sous confinement. Chaque forme vérifiée est testée seule, puis en chaîne `&&` derrière `git status`, en pipe vers `head` et avec `2>/dev/null` :
  - `branch` : création avec et sans point de départ, copie, suivi amont posé ou retiré, description, création avec ou sans suivi ;
  - `worktree` : `add`, `remove`, `prune`, `move`, `lock`, `unlock`, `repair` ;
  - `fetch` : destination de refspec, `+` forcé, écriture du suivi amont, option hors de la liste fermée ;
  - `diff` : hors index, chemin hors du dépôt avant et après `--` ;
  - `log`, `diff` et `show` avec un diff externe.
- Sous confinement, à travers le chemin composé : une forme de chaque famille, dont une dont un argument quoté contient un séparateur.
- Sonde de revue (`BASE=` puis lecture de `BASE`), hors et sous confinement : `diff` et `log` avec un diff externe.
- Directs sur le prédicat d'entrée : `is_safe_bash_command` refuse une forme de chaque famille, hors et sous confinement.
- Positifs, mêmes chemins, qui restent admis : `git branch --show-current`, `git branch -a`, `git branch --list 'fix/*'`, `git worktree list --porcelain`, `git fetch origin main`, `git fetch --prune origin`, `git diff --stat origin/main`, `git log --oneline -5`, `git show HEAD:README.md`, `git log '--grep=a|b' --oneline`, la sonde de revue ordinaire (`git diff --stat $BASE`) et le compound `ce-work` sous confinement.

**Execution note :** commencer par le commit vert, puis mesurer le rouge-avant terme par terme. Le site d'appel neutralisé et la boucle réduite à la commande entière doivent chacun faire rougir au moins un test. Un mutant identité de contrôle doit rester vert.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Rouge-avant terme par terme sur le commit vert, comme dans l'Execution note.
- Parité sur corpus réel, handler complet, hors et sous confinement, `dfcf5f5` contre le commit vert. La létalité est distinguée (refus terminal ou non). Mesure préalable sur prototype : 35 925 commandes distinctes, 0 refusé → admis, et 11 admis → refusé, identiques hors et sous confinement, toutes non terminales :
  - **2 créations de branche locale.** C'est une écriture de ref. Le pilote garde son repère en notant le SHA (`git rev-parse HEAD`).
  - **4 `fetch` avec une destination explicite**, toutes sous `refs/remotes/`. Aucune branche locale n'était visée. Le substitut admis est `git fetch origin <branche>`, puis `origin/<branche>` ou `FETCH_HEAD` (par exemple pour une ref de PR).
  - **5 `worktree add` d'un arbre de comparaison à la base.** C'est un geste ordinaire de pilote, et il est pesé ici. D'après les transcripts, les cinq servaient à lancer des tests sur la base, pour un témoin rouge-avant. Une lecture (`git show <ref>:<chemin>`, `git diff <ref>`) ne remplace pas ce geste. Il n'est plus auto-approuvé par tier1, et le plan l'accepte : créer un arbre de travail écrit dans l'administration du dépôt, ce n'est pas une lecture. Un des cinq a d'ailleurs laissé l'arbre suivi dans la branche du pilote, et il a fallu un commit de nettoyage. Le refus est non terminal, donc le pilote n'en meurt pas.
- La parité est rejouée sur l'implémentation finale, et non sur le prototype.

## Definition of Done

Commit local sur `fix/tier1-n-admet-plus-que-les-vraies-lectures`, partie de `origin/main` (`dfcf5f5`, où la liste fermée est déjà mergée). Pas de ticket public. Titre, commits et corps sobres. Arrêt avant le push pour l'audit, et pas de merge.
