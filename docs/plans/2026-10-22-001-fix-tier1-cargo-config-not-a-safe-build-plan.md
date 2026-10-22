---
title: "Un cargo qui porte --config n'est plus un build sûr"
date: 2026-10-22
kind: fix
class: admission (refus de plus, aucune admission de plus) — tier1 + veto de la policy ; létalité inchangée
artifact_contract: ce-unified-plan/v1
execution: code
product_contract_source: ce-plan-bootstrap
---

# Un cargo qui porte `--config` n'est plus un build sûr — Plan

## Goal Capsule

- **Objectif :** une commande `cargo` qui porte `--config` n'est plus admise automatiquement, ni par tier1, ni par la policy en aval. Elle tombe au refus par défaut. Le fichier de configuration de cargo dans le worktree n'est plus écrit sans passer par la policy.
- **Moyen :** un prédicat unique `cargo_carries_config` dans `src/claude_pilot/tier1.py`, consulté par tier1 et comme veto dans `permissions._bash_allow_is_chain_safe` ; les chemins `.cargo/config` et `.cargo/config.toml` sortent de l'auto-approbation `Write`/`Edit` et une commande Bash qui peut écrire dans un répertoire `.cargo` n'est plus admise (`bash_names_cargo_dir`).
- **Autorité :** porte du classifieur. Le merge revient à Vincent.
- **Arrêt :** toute commande cargo ordinaire qui passerait d'admise à refusée est signalée, pas avalée.

## Product Contract

### Problem Frame

`is_safe_build_command` ne lit que le premier mot après `cargo` (`_CARGO_RE`, `SAFE_CARGO_SUBCOMMANDS`). Tout ce qui suit la sous-commande est ignoré, `--config` compris. Or `--config` change la configuration de cargo pour ce lancement, y compris l'environnement des processus que cargo démarre. Le préfixe `NAME=valeur` devant `cargo` est fermé par une liste close (cpp#242) ; `--config` rouvre l'axe que cette liste ferme.

Mesuré hors ligne sur `caf7e82` (script de sonde, policy embarquée) :

- tier1 admet `cargo <sous-commande> … --config <clé>=<valeur>` sous les formes `--config x` et `--config=x`, seules ou dans un compound ;
- la règle policy `bash-cargo` (préfixe terminé par `\b`) admet les mêmes formes, et aussi celles précédées du préfixe d'environnement fermé de cpp#242, que tier1 refuse déjà. Fermer tier1 seul ne changerait donc rien de bout en bout ;
- `cargo --config … <sous-commande>` (avant la sous-commande) est déjà refusé par les deux couches, par accident d'ancre ;
- cargo n'accepte pas d'abréviation de `--config` (`--conf` est rejeté par cargo lui-même).

Voie fichier, vérifiée : `Write`/`Edit` sur `.cargo/config.toml` dans le worktree sont auto-approuvés par tier1 (`is_within_project` seul, sans liste de plan de contrôle), et un `cargo test` nu tient compte de ce fichier (crate de sonde neutre, variable inerte). Côté Bash, aucune garde de destination ne nommait `.cargo`.

### Requirements

- R1. Un mot `cargo` (texte après retrait des quotes, nom de base `cargo`) suivi d'un argument `--config` ou `--config=…`, n'importe où après lui, rend la commande non sûre.
- R2. Même effet pour un argument dont la valeur réelle n'est pas lisible dans le texte et pourrait être `--config` : un mot qui commence par une expansion, un mot en `-` dont le nom d'option (avant `=`) contient une expansion ou un glob, un glob en tête de mot, une expansion d'accolades.
- R3. Un segment qui contient `cargo` et que le lexeur ne sait pas suivre (quote ou `${` non fermé) est non sûr.
- R4. Le même prédicat est consulté par `is_safe_build_command` (seule voie tier1 qui admet cargo : `command cargo`, `xargs cargo` et un wrapper `time` sont déjà refusés, mesuré) et comme veto du chemin policy dans `_bash_allow_is_chain_safe`.
- R5. tier1 n'auto-approuve plus `Write`/`Edit` d'un chemin `(^|/).cargo/config(.toml)?` — jugé sur le chemin littéral normalisé et sur le chemin résolu. Aucune règle policy ne couvre ces outils : refus par défaut.
- R6. *(révisé après revue)* Une commande Bash dont un mot peut désigner un composant de chemin `.cargo` n'est plus admise : prédicat `bash_names_cargo_dir`, consulté par `is_safe_git_command` (tier1) et comme veto dans `_bash_allow_is_chain_safe` (y compris sur la première ligne d'un heredoc, sans lire son corps). Mots découpés sur `/` et `=` après retrait des quotes ; une accolade vaut « n'importe quoi » ; un glob hors quotes compte s'il épelle le point initial. Une entrée « fichier » dans `_CONTROL_PLANE_PATTERNS`, essayée d'abord, ne couvrait pas les écritures qui visent le répertoire sans nommer le fichier, ni les répertoires ancêtres hors worktree ; elle est retirée au profit du prédicat.
- R7. Refus = non-admission. Aucun nouveau chemin d'admission. La létalité n'est pas modifiée.

### Scope Boundaries

- Préservé : toutes les commandes cargo ordinaires (`build`, `test`, `check`, `clippy`, `fmt`, `doc`, `tree`, `metadata`, `--version`, drapeaux usuels, `--` vers le binaire de test, préfixe fermé de cpp#242) gardent leur décision. `.cargo/audit.toml` (fichier de cargo-audit, versionné dans mika) n'est pas touché.
- Choix assumé : `--config` est refusé même après `--`, où il irait au binaire de test et non à cargo. Sur-refus sans usage connu, au profit d'un prédicat sans cas particulier.
- Choix assumé : une écriture Bash vers `.cargo/audit.toml` est refusée aussi (le prédicat vise le répertoire) ; `Write`/`Edit` sur ce fichier restent admis.
- Choix assumé : une expansion (`$NAME`) est lue à sa graphie littérale. Toute destination d'écriture qui en porte une est déjà vetoée par `_destination_veto_reason`, et toute sous-commande git qui écrit la refuse (`git_argument_value_hidden`). La lire comme « n'importe quoi » refusait 46 commandes réelles (boucles, `$?`).
- Des résiduels hors de ce périmètre sont suivis en privé.

## Planning Contract

### Key Technical Decisions

- KTD1. Réutiliser `_lex_shell_words` (cpp#286) : il donne le texte après retrait des quotes, les expansions, les globs et les accolades mot par mot. `--"config"`, `\--config` ou `'--config=x'` sont donc lus comme cargo les reçoit.
- KTD2. Un seul prédicat public, deux couches, comme `git_argument_value_hidden` : tier1 et la policy ne peuvent pas diverger. Le veto policy est indépendant du `rule_id` : une règle qui précède `bash-cargo` dans l'ordre ne le court-circuite pas.
- KTD3. Fermer la voie fichier par le chemin, pas par le contenu : refuser l'écriture de `.cargo/config*` couvre `[env]` et toutes les autres clés de configuration, sans analyser du TOML.
- KTD4. Le prédicat de chemin vit dans `tier1.py` (`permissions.py` importe déjà `tier1`) ; le chemin (Write/Edit) et le répertoire (Bash) sont deux prédicats voisins du même fichier.

## Implementation Units

### U1. Prédicat cargo (`src/claude_pilot/tier1.py`)

- `cargo_carries_config(command) -> bool` : découpe en segments (`_split_compound_command`), lexe chaque segment, repère chaque mot de nom de base `cargo`, applique R1–R3 aux mots suivants.
- Branchement : `is_safe_build_command` (branche cargo). Un appel de plus dans `is_safe_bash_command` a été écarté : aucune autre voie tier1 n'admet cargo, il ne serait couvert par aucun test capable de rougir seul.

### U2. Veto policy (`src/claude_pilot/permissions.py`)

- Dans `_bash_allow_is_chain_safe`, à côté du veto git : `if cargo_carries_config(command): return False`.

### U3. Chemins de configuration cargo

- `tier1.py` : `_CARGO_CONFIG_PATH_RE` et `is_cargo_config_path(file_path, cwd)` (littéral normalisé + résolu, relatif au worktree) ; `is_tier1_auto_approve` refuse `Write`/`Edit` sur ces chemins.
- *(révisé)* `tier1.py` : `bash_names_cargo_dir(command)`, branché dans `is_safe_git_command` ; `permissions.py` : veto dans `_bash_allow_is_chain_safe`, avant l'exception heredoc et après le veto `--config`.

### Test scenarios

- Négatifs (tier1 et chemin policy) : `--config <v>`, `--config=<v>`, avant et après la sous-commande, avec et sans le préfixe `env.`, avec le préfixe fermé de cpp#242, dans un compound, entre quotes ou échappé, nom d'option par expansion, glob ou accolades ; segment non lexable.
- Négatifs fichier : `Write`/`Edit` sur `.cargo/config.toml`, `.cargo/config`, `sous/dossier/.cargo/config.toml`, chemin absolu dans le worktree ; destination Bash `cp f .cargo/config.toml` vetoée.
- Positifs : corpus de cpp#242 (admis et refusés) à décision inchangée ; commandes cargo ordinaires ; `Write` sur `.cargo/audit.toml` et sur un fichier ordinaire toujours admis.

## Verification Contract

- Suite complète, `ruff` et `mypy` verts.
- Table avant/après produite par le script de sonde hors ligne sur le corpus de cpp#242 et les formes `--config` : aucune bascule refusée → admise ; chaque bascule admise → refusée est une forme `--config`.
- Parité sur corpus réel : tous les appels Bash/Write/Edit des transcripts pilotes, ancien contre neuf. Mesuré : 39 352 appels distincts, dont 3 621 commandes cargo ; 2 bascules admis → refusé, les deux seules commandes réelles portant `--config` (`build.incremental`, dont le préfixe admis `CARGO_INCREMENTAL=` donne l'équivalent) ; 0 bascule refusé → admis.
- Rouge-avant terme par terme : chaque clause du prédicat et chaque branchement (`is_safe_build_command`, veto policy, `Write`/`Edit`, chemin résolu, `bash_names_cargo_dir` et ses branchements tier1 git et veto policy) retiré seul, les négatifs correspondants vus rouges ; commit vert d'abord.

## Definition of Done

PR sur `fix/un-cargo-qui-porte-config-n-est-plus-un`, sans ticket public, titre et corps sobres. Pas de merge.
