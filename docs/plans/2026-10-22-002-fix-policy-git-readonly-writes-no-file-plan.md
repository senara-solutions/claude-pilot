---
title: "Une sous-commande git en lecture seule n'écrit plus de fichier - Plan"
type: fix
date: 2026-10-22
class: admission (refus de plus, aucune admission de plus) — veto de la policy ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Une sous-commande git en lecture seule n'écrit plus de fichier - Plan

## Goal Capsule

- **Objectif :** une commande git admise comme lecture n'écrit aucun fichier. Une commande `git log`, `git show` ou `git diff` qui porte l'option `--output`, ou un mot que bash peut étendre en elle, retombe dans le refus par défaut.
- **Moyen :** un prédicat unique `git_option_writes_a_file` dans `src/claude_pilot/tier1.py`, lu sur les mots après retrait des quotes, consulté par tier1 et comme veto dans `permissions._bash_allow_is_chain_safe`.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** toute lecture git ordinaire qui passerait d'admise à refusée est signalée, pas avalée.

## Product Contract

### Problem Frame

`bash-git-readonly` (`src/claude_pilot/policies/permissions.yaml`) est un préfixe : `^git\s+(status|log|diff|show|…)`. Il ne lit rien après la sous-commande. Or `git log`, `git show` et `git diff` acceptent `--output=<fichier>` et `--output <fichier>`, qui font écrire leur sortie par git dans ce fichier.

tier1 refuse déjà ces formes (`_GIT_DENIED_GLOBAL_FLAG_RE`, `_GIT_READONLY_DENIED_TOKENS`). Mais un refus de tier1 passe la main à la policy, et `_bash_allow_is_chain_safe` ne rejoue pas les gardes d'options de tier1 sur un allow. Mesuré sur `caf7e82` à travers `create_permission_handler` avec la policy embarquée : les quatre formes `diff`/`log`/`show` × `=`/espace donnent tier1 refusé, policy `allow/bash-git-readonly`, handler `allow`.

Sondé sur git 2.53 : ces trois sous-commandes n'acceptent que `--output=F` et `--output F`. Aucune abréviation n'est acceptée (`--outpu`, `--outp`, `--out`, `--ou`, `--o` sont rejetés par git), ni `--output-directory`, ni `-o`. Les orthographes entre quotes ou échappées sont déjà refusées sur le chemin policy par `git_argument_value_hidden` (clause e).

### Requirements

- R1. Un mot `git` suivi, dans le même segment et avant un `--` non quoté, d'un argument dont le texte après retrait des quotes vaut `--output` ou commence par `--output=`, rend la commande non honorable sur un allow de la policy.
- R1b. *(ajouté après revue)* Même effet pour un mot glob non quoté dont la tête littérale, coupée au premier caractère de glob, est un préfixe de `--output` : son texte ne montre pas ce que git recevra. tier1 admettait ces formes ; il consulte désormais le même prédicat, à côté de `git_argument_value_hidden`.
- R2. Le veto est indépendant du `rule_id`, comme le veto git voisin : aucune règle qui précède `bash-git-readonly` ne le court-circuite.
- R3. `--output-indicator-new`, `--output-indicator-old` et `--output-indicator-context` ne sont pas concernés : ils ne nomment pas de fichier.
- R4. Refus = non-admission. Aucun nouveau chemin d'admission. La létalité n'est pas modifiée.

### Scope Boundaries

- Préservé : toutes les lectures git ordinaires gardent leur décision, y compris la redirection sanctionnée `git show <rev>:<chemin> > <chemin-relatif>`.
- *(révisé après revue)* Après un `--` non quoté, git lit des chemins : la lecture s'y arrête, comme dans `git_argument_value_hidden`. `git diff -- *.py` reste admis. Un `--` entre quotes ne compte pas, côté sûr.
- Choix assumé : le veto vaut pour toute sous-commande git, pas seulement les trois. tier1 refuse déjà `--output` sur toute commande git ; la policy s'aligne.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. Réutiliser `_lex_shell_words` : le texte de chaque mot est celui que git reçoit après retrait des quotes et des échappements.
- KTD2. Un segment que le lexeur ne sait pas suivre est laissé à `git_argument_value_hidden`, qui le refuse déjà quand il contient `git`. Le dupliquer ici créerait un mutant équivalent.
- KTD3. *(révisé après revue)* tier1 refusait la forme littérale mais admettait la forme glob ; il consulte donc le même prédicat. Le rejeu par le handler le vérifie au lieu de le supposer.

## Implementation Units

### U1. Prédicat (`src/claude_pilot/tier1.py`)

- `git_option_writes_a_file(command) -> bool` : segments par `_split_compound_command`, mots par `_lex_shell_words`, pour chaque mot `git`, R1 sur les mots suivants.

### U2. Veto policy (`src/claude_pilot/permissions.py`)

- Dans `_bash_allow_is_chain_safe`, juste après le veto `git_argument_value_hidden` : `if git_option_writes_a_file(command): return False`.
- *(ajouté après revue)* Dans `is_safe_bash_command`, même appel juste après `git_argument_value_hidden`.

### Test scenarios

- Négatifs, à travers le handler : `diff`, `log`, `show` × `--output=F`, `--output F` ; dans un compound ; après un saut de ligne qui suit un commentaire ; `--` entre quotes ; entre quotes (déjà refusé, gardé comme témoin) ; formes glob (`?`, `[…]`, `*`).
- Positifs : lectures ordinaires (`git diff --stat`, `git log --oneline -5`, `git show HEAD:f`), `--output-indicator-new=+`, redirection sanctionnée `git show <rev>:<f> > <f>`, glob dont la tête ne peut épeler l'option (`--grep=fix*`, `src/*.py`), chemin après `--`, prédicat faux sur une commande non git.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Parité sur corpus réel : tous les appels Bash des transcripts pilotes, ancien (`caf7e82`) contre neuf, handler complet. Mesuré : 35 925 commandes distinctes, dont 6 947 contenant `git` ; 0 bascule refusé → admis ; 6 bascules admis → refusé, toutes `git diff` portant `--output` littéral vers un fichier de travail ; la clause glob ne fait basculer aucune commande réelle. Le refus est non terminal et la lecture du diff sur la sortie standard reste admise.
- Rouge-avant terme par terme : la clause `== "--output"`, la clause `startswith("--output=")`, la clause glob, le filtre sur le mot `git`, la découpe en segments, l'arrêt au `--`, l'exigence d'un `--` non quoté, le branchement tier1 et le branchement du veto, chacun neutralisé seul, rougissent leurs tests ; commit vert d'abord. Une sous-clause de tête glob sans mutant tué a été retirée.

## Definition of Done

PR sur `fix/policy-une-sous-commande-git-en-lecture`, sans ticket public, titre et corps sobres, arrêt avant push pour relecture. Pas de merge.
