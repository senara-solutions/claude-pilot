---
title: "fix(permissions): le callback refuse les écritures gh de la liste L, et gh pr edit n'agit que sur la PR de la branche (mika#3030, partie claude-pilot)"
type: fix
status: done
date: 2026-11-03
origin: senara-solutions/mika#3030 (sections Site et Hors vol)
---

# Partie claude-pilot de mika#3030

## Contexte

mika#3030 retire de chaque worktree de pilote l'allowlist copiée de l'opérateur
(`gh pr:*`, `gh issue:*`, `gh repo:*`). Une fois ce retrait fait, ces commandes
reviennent devant `can_use_tool`, et c'est la politique du pilote qui décide.
Cette partie fixe ce que le callback décide pour elles, et l'épingle par des
tests qui passent par le vrai callback (`create_permission_handler`).

## Mesure (b1a03ca, callback réel, politique embarquée, cwd = répertoire neuf)

| commande | Tier 1 | politique | callback |
|---|---|---|---|
| `gh issue create …` | non | deny `gh-issue-create-deny` | **refus** |
| `gh issue close 1` | non | deny (défaut) | refus |
| `gh issue edit 1 --add-label ready` | **oui** | deny (défaut) | **ADMIS** |
| `gh issue comment 1 --body x` | **oui** | deny (défaut) | **ADMIS** |
| `gh pr merge 1` | non | deny (défaut) | refus |
| `gh pr review 1 --approve` | non | deny (défaut) | refus |
| `gh pr close 1` | non | deny (défaut) | refus |
| `gh repo delete x` | non | deny (défaut) | refus |
| `gh label create foo` | non | deny (défaut) | refus |
| `gh pr edit --body-file pr.md` | non | deny (défaut) | refus |
| `gh pr ready` | non | deny (défaut) | refus |

La table « Mesure » de mika#3030 dit « deny (défaut) » pour `gh issue edit` et
`gh issue comment`. C'est vrai de la politique seule, mais la Tier 1 passe
avant : `SAFE_GH_SUBCOMMANDS["issue"]` (`tier1.py:5509`) contient `edit` et
`comment`, et le callback les admet. Retirer l'allowlist côté mika ne suffit
donc pas pour ces deux commandes.

Formes relevées dans les 1 071 transcripts (occurrences textuelles, pas
seulement des `tool_use`, donc un ordre de grandeur) : `gh pr edit` est surtout
appelé avec un numéro (`gh pr edit <N> --repo …`, `gh pr edit <N> --add-label`),
et `gh pr ready` surtout sans sélecteur.

## Décisions

### D1. `gh issue edit` et `gh issue comment` : deny

Retrait de `edit` et `comment` de `SAFE_GH_SUBCOMMANDS["issue"]`, et une règle
deny explicite pour chacune (`gh-issue-edit-deny`, `gh-issue-comment-deny`).
Raison : un pilote travaille sur sa branche et sa PR, et le ticket est l'objet du
groom et de l'opérateur. `gh issue edit --add-label` peut déclencher le moteur
(un label `ready` lance un vol), et un commentaire de ticket est un écrit public
qui fait aussi tourner le moteur. Aucun prompt de skill mika ne demande ces
gestes au pilote : les `gh issue edit` de `dispatch-lib.sh` sont faits par le
script, hors `can_use_tool`. C'est un durcissement. Le refus est non terminal :
le pilote reçoit le refus en `tool_result` et continue.

### D2. `gh pr edit` : allow, limité à la PR de la branche

Règle `bash-gh-pr-edit-own-branch`, ancrée `^…$`. Elle admet `gh pr edit` sans
sélecteur positionnel, donc sur la PR de la branche courante, avec une liste
fermée de drapeaux qui prennent tous une valeur : `--title`/`-t`, `--body`/`-b`,
`--body-file`/`-F`, `--add-label`, `--remove-label`, `--add-reviewer`. Chaque
drapeau consomme exactement une valeur, donc un numéro, une URL ou une branche
en trop ne trouve pas de place et la règle ne matche pas. Sont exclus
`--repo`/`-R` (autre dépôt), `--base`/`-B` (reciblage), les assignés, projets et
jalons. Les valeurs sont entre guillemets doubles, entre guillemets simples, ou
nues sans métacaractère ni `~` en tête.
La valeur de `--body-file`/`-F` est plus étroite, parce que le fichier est
publié dans le corps de la PR : `-` (stdin), ou un chemin nu relatif ou sous
`/tmp/`, sans `..` ni `~`. La substitution (`$(`, backtick) et les chaînes restent
refusées par `_bash_allow_is_chain_safe`, qui s'applique à toute règle allow.

### D3. `gh pr ready` : option (a), tranchée par samidarko le 2026-11-03 à 20:25Z

Le ticket demande une règle allow limitée à la PR de la branche. Le contrat
draft de mika (mika#1613, mika#1682, mika#2624 ; `self-dev-webhook-ci/system_prompt.md:61`)
dit le contraire : « Do NOT call `gh pr ready` on any pull request. Taking a PR
out of draft is never this turn's decision: a draft may be a hold somebody
placed on purpose ». Une PR tenue en draft par `gh pr ready --undo` est
justement une retenue avant merge. Un pilote qui la remet prête la lève, et le
moteur peut fusionner une PR verte en environ 90 s (cpp#276).

Options :

- **(a) Retenue.** Règle explicite `bash-gh-pr-ready-undo-own-branch` qui
  admet seulement `gh pr ready --undo` sans sélecteur (mettre sa propre PR en
  draft, le sens sûr), et règle explicite `gh-pr-ready-deny` pour tout autre
  `gh pr ready`. Témoins : `--undo` admis, `gh pr ready` refusé, `gh pr ready 12
  --undo` refusé.
- **(b)** Ce que demande le ticket : allow de `gh pr ready` sans sélecteur.
  Contredit le contrat draft.

### D4. Les sept autres commandes de L

Une règle deny explicite chacune n'est pas nécessaire : le défaut les refuse
déjà. Les tests les épinglent par le callback. Seul `gh issue create` cite une
règle, `gh-issue-create-deny`, comme le demande le ticket.

## Tests (`tests/test_permissions.py`, classe nouvelle)

1. Pour chaque commande de L : le callback rend `PermissionResultDeny`, non
   terminal. Pour `gh issue create`, la règle citée est `gh-issue-create-deny`
   (via `evaluate`, et le refus du callback).
2. `gh pr edit` : témoins positifs (`--body-file pr.md`, `-F /tmp/pr.md`,
   `--title "x" --add-label ready`…) admis ; témoins négatifs refusés : un
   numéro avant ou après les drapeaux, une URL, `--repo`, `--base`, un
   `--body-file` sous `~`, absolu hors `/tmp/` ou avec `..`, `$(id)`, une
   chaîne, et `gh pr edit` seul.
3. `gh pr ready` : `--undo` admis ; `gh pr ready`, `gh pr ready 12`,
   `gh pr ready 12 --undo` et `--repo` refusés par `gh-pr-ready-deny`.
4. Tier 1 : `is_tier1_auto_approve` rend False pour `gh issue edit` et
   `gh issue comment`, et reste True pour `gh issue view`/`list`.
5. Temps borné de la nouvelle regex sur une entrée hostile, sous
   `_REDOS_BUDGET_S`.

## Restes (pas de ticket, gel jusqu'au 01/12)

- Filtre de chemin de `--body-file` limité à `gh pr edit` ; extension à
  `gh pr create` hors ticket.

## Hors périmètre

- Le côté mika (`dispatch-lib.sh`, AC1, AC2, AC4, AC5) : fait par la boucle.
- AC2 côté mika écrivait `gh pr ready` dans l'allow des réglages du worktree,
  ce qui aurait encore court-circuité le callback. samidarko l'a retiré du
  corps de mika#3030 le 2026-11-03.
- Aucun ticket ouvert (gel jusqu'au 01/12).

## Risque

Ces règles changent ce que le pilote peut faire. D1 et D4 durcissent. D2 ajoute
un allow au niveau politique, mais plus étroit que l'allow `gh pr:*` qu'il
remplace en pratique. D3 (a) n'admet que le sens sûr.
