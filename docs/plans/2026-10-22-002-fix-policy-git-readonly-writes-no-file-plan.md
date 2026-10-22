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

- **Objectif :** une commande git admise par la règle `bash-git-readonly` n'écrit aucun fichier. Une commande `git log`, `git show` ou `git diff` qui porte l'option `--output` retombe dans le refus par défaut, comme tier1 la traite déjà.
- **Moyen :** un prédicat unique `git_option_writes_a_file` dans `src/claude_pilot/tier1.py`, lu sur les mots après retrait des quotes, consulté comme veto dans `permissions._bash_allow_is_chain_safe`.
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** toute lecture git ordinaire qui passerait d'admise à refusée est signalée, pas avalée.

## Product Contract

### Problem Frame

`bash-git-readonly` (`src/claude_pilot/policies/permissions.yaml`) est un préfixe : `^git\s+(status|log|diff|show|…)`. Il ne lit rien après la sous-commande. Or `git log`, `git show` et `git diff` acceptent `--output=<fichier>` et `--output <fichier>`, qui font écrire leur sortie par git dans ce fichier.

tier1 refuse déjà ces formes (`_GIT_DENIED_GLOBAL_FLAG_RE`, `_GIT_READONLY_DENIED_TOKENS`). Mais un refus de tier1 passe la main à la policy, et `_bash_allow_is_chain_safe` ne rejoue pas les gardes d'options de tier1 sur un allow. Mesuré sur `caf7e82` à travers `create_permission_handler` avec la policy embarquée : les quatre formes `diff`/`log`/`show` × `=`/espace donnent tier1 refusé, policy `allow/bash-git-readonly`, handler `allow`.

Sondé sur git 2.53 : ces trois sous-commandes n'acceptent que `--output=F` et `--output F`. Aucune abréviation n'est acceptée (`--outpu`, `--outp`, `--out`, `--ou`, `--o` sont rejetés par git), ni `--output-directory`, ni `-o`. Les orthographes entre quotes ou échappées sont déjà refusées sur le chemin policy par `git_argument_value_hidden` (clause e).

### Requirements

- R1. Un mot `git` suivi, n'importe où après lui dans le même segment, d'un argument dont le texte après retrait des quotes vaut `--output` ou commence par `--output=`, rend la commande non honorable sur un allow de la policy.
- R2. Le veto est indépendant du `rule_id`, comme le veto git voisin : aucune règle qui précède `bash-git-readonly` ne le court-circuite.
- R3. `--output-indicator-new`, `--output-indicator-old` et `--output-indicator-context` ne sont pas concernés : ils ne nomment pas de fichier.
- R4. Refus = non-admission. Aucun nouveau chemin d'admission. La létalité n'est pas modifiée.

### Scope Boundaries

- Préservé : toutes les lectures git ordinaires gardent leur décision, y compris la redirection sanctionnée `git show <rev>:<chemin> > <chemin-relatif>`.
- Choix assumé : `--output` est refusé aussi après `--`, où git le lirait comme un chemin. Sur-refus sans usage connu, au profit d'un prédicat sans cas particulier.
- Choix assumé : le veto vaut pour toute sous-commande git, pas seulement les trois. tier1 refuse déjà `--output` sur toute commande git ; la policy s'aligne.
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. Réutiliser `_lex_shell_words` : le texte de chaque mot est celui que git reçoit après retrait des quotes et des échappements.
- KTD2. Un segment que le lexeur ne sait pas suivre est laissé à `git_argument_value_hidden`, qui le refuse déjà quand il contient `git`. Le dupliquer ici créerait un mutant équivalent.
- KTD3. tier1 n'est pas modifié : il refuse déjà la forme littérale. Le rejeu par le handler le vérifie au lieu de le supposer.

## Implementation Units

### U1. Prédicat (`src/claude_pilot/tier1.py`)

- `git_option_writes_a_file(command) -> bool` : segments par `_split_compound_command`, mots par `_lex_shell_words`, pour chaque mot `git`, R1 sur les mots suivants.

### U2. Veto policy (`src/claude_pilot/permissions.py`)

- Dans `_bash_allow_is_chain_safe`, juste après le veto `git_argument_value_hidden` : `if git_option_writes_a_file(command): return False`.

### Test scenarios

- Négatifs, à travers le handler : `diff`, `log`, `show` × `--output=F`, `--output F` ; dans un compound ; après un global option ; entre quotes (déjà refusé, gardé comme témoin).
- Positifs : lectures ordinaires (`git diff --stat`, `git log --oneline -5`, `git show HEAD:f`), `--output-indicator-new=+`, redirection sanctionnée `git show <rev>:<f> > <f>`, prédicat faux sur une commande non git.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Parité sur corpus réel : tous les appels Bash des transcripts pilotes, ancien (`caf7e82`) contre neuf, handler complet. Attendu : 0 bascule refusé → admis ; chaque bascule admis → refusé porte `--output`.
- Rouge-avant terme par terme : la clause `== "--output"`, la clause `startswith("--output=")`, le filtre sur le mot `git` et le branchement du veto, chacun neutralisé seul, rougissent leurs tests ; commit vert d'abord.

## Definition of Done

PR sur `fix/policy-une-sous-commande-git-en-lecture`, sans ticket public, titre et corps sobres, arrêt avant push pour relecture. Pas de merge.
