---
title: "Un drapeau de configuration a un jumeau fichier — garder le répertoire, pas le fichier"
date: 2026-10-22
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "Un `cargo` qui porte `--config` était un build sûr pour tier1 et une admission de la policy (`bash-cargo` s'arrête à la sous-commande)"
  - "Une fois le drapeau refusé, le fichier de configuration de cargo restait atteignable par des écritures qui ne le nomment jamais"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safety, write-edit]
tags: [permissions, tier1, policy, chain-safe, cargo, config, control-plane, write-destination, lexer, parity, real-corpus, mutation-testing, admission, tightening]
applies_when: "refuser une option qui change la configuration d'un outil (drapeau `--config`, `-c`, variable d'environnement) alors que l'outil lit aussi cette configuration dans un fichier découvert par répertoire"
---

# Un drapeau de configuration a un jumeau fichier — garder le répertoire, pas le fichier

## Problème

`--config` change la configuration de cargo pour un lancement, environnement des processus enfants compris. La liste close des préfixes `NAME=valeur` de cpp#242 gouverne ce qu'un pilote peut poser devant `cargo` ; `--config` posait la même chose depuis la liste d'arguments, que ni `_CARGO_RE` (tier1) ni `bash-cargo` (policy, préfixe terminé par `\b`) ne lisent. Les deux couches l'admettaient.

Le correctif du drapeau tient en un prédicat (`cargo_carries_config`, `src/claude_pilot/tier1.py:3913`) consulté par `is_safe_build_command` (`tier1.py:4386`) et en veto dans `_bash_allow_is_chain_safe` (`permissions.py:565`), sur le modèle de `git_argument_value_hidden` (voir [a-git-guard-reads-text-but-bash-evaluates-it-first.md](a-git-guard-reads-text-but-bash-evaluates-it-first.md)). Ce n'était que la moitié du travail.

## Ce qui n'était pas évident

**1. Le drapeau a un jumeau fichier, et le fichier se découvre par répertoire.** Cargo lit `.cargo/config.toml` dans son répertoire courant et dans chacun de ses ancêtres. Refuser le drapeau sans fermer le fichier ne ferme rien. `Write`/`Edit` étaient auto-approuvés sur tout chemin du worktree (`is_within_project` seul) : la première fermeture a donc visé le chemin du fichier, pour `Write`/`Edit` (`is_cargo_config_path`, `tier1.py:4927`) et par une entrée « fichier » dans `_CONTROL_PLANE_PATTERNS` pour les écritures Bash.

**2. Une liste « fichier » rate toutes les écritures qui ne nomment pas le fichier.** Trois relecteurs indépendants (correctness, adversarial, sécurité) ont rendu le même constat : l'entrée ne voyait que le chemin complet. Or une écriture peut viser le répertoire sans épeler le fichier : opérande répertoire, création du répertoire, sous-commande git qui écrit dans un chemin, valeur de drapeau. Et la découverte par ancêtres étend la surface hors du worktree, jusqu'aux racines scratch où la policy autorise l'écriture.

Les autres entrées du plan de contrôle sont ancrées `(/|$)` et couvrent le répertoire lui-même ; une entrée « fichier » perd cette propriété sans qu'on le voie. La fermeture vise désormais le **répertoire** : `bash_names_cargo_dir` (`tier1.py:3972`) refuse toute commande dont un mot peut désigner un composant de chemin `.cargo`. Il est consulté par `is_safe_git_command` (`tier1.py:3996`), comme veto du chemin policy (`permissions.py:570`) et sur la première ligne d'un heredoc (`permissions.py:530`), sans lire le corps. L'entrée « fichier » du plan de contrôle est retirée : le veto l'aurait rendue inatteignable.

**3. Choisir la sévérité d'une clause sur corpus réel, pas sur intuition.** La première version lisait toute expansion (`$NAME`) comme « n'importe quoi », par prudence. Rejouée sur le corpus réel, elle refusait 46 commandes légitimes : boucles `for f in …; do … "$f"`, `$?`, `${PIPESTATUS[0]}`. Or toute destination d'écriture qui porte une expansion est déjà vetoée par `_destination_veto_reason`, et toute sous-commande git qui écrit la refuse (`git_argument_value_hidden`). Une expansion est donc lue à sa graphie littérale. Même rejeu, même leçon pour un glob : un `.*` **entre quotes** (un script `sed`) n'est pas un glob. Seul un glob hors quotes (`_ShellWord.glob`) compte, et seulement s'il épelle le point initial.

Rejeu final, ancien (`caf7e82`) contre neuf : 39 352 appels Bash/Write/Edit distincts tirés des transcripts pilotes, dont 3 621 commandes cargo. Deux bascules admis → refusé, les deux seules commandes réelles qui portaient `--config` (un réglage de compilation incrémentale, que le préfixe admis `CARGO_INCREMENTAL=` reproduit). Aucune bascule refusé → admis. Recette : charger l'ancien et le neuf par `PYTHONPATH`, sans `python -I` (ce drapeau ignore `PYTHONPATH` et fait mesurer deux fois le même code).

## Solution

- `cargo_carries_config` : après retrait des quotes et des continuations de ligne, un mot `cargo` (nom de base) suivi d'un argument `--config`/`--config=…`, ou d'un argument qui pourrait le devenir (commence par une expansion ; nom d'option porteur d'une expansion ou d'un glob ; glob de tête ; accolade) ; segment non lexable → refus.
- `is_cargo_config_path` : `Write`/`Edit` sur `.cargo/config` ou `.cargo/config.toml`, à toute profondeur, jugés sur le chemin littéral normalisé **et** résolu (lien symbolique). Ils tombent en policy, qui n'a aucune règle pour ces outils : refus par défaut.
- `bash_names_cargo_dir` : voir point 2.
- Rouge-avant terme par terme : 17 mutations, une par clause et par site de branchement, chacune vue rouge (`tests/test_tier1_cargo_config.py`).

## Prévention

- Avant de refuser une option de configuration, chercher son jumeau fichier et **toutes** les voies d'écriture vers le répertoire qui le contient. Un drapeau, une variable d'environnement et un fichier découvert par répertoire sont trois portes vers la même chose (voir [an-env-assignment-prefix-before-cargo-is-a-build-variable-not-egress.md](an-env-assignment-prefix-before-cargo-is-a-build-variable-not-egress.md)).
- Une garde de destination qui nomme un fichier, et pas son répertoire, ne voit aucune écriture qui vise le répertoire. C'est le même mécanisme que celui de [narrowing-a-blanket-guard-means-re-arming-what-it-covered-by-accident.md](narrowing-a-blanket-guard-means-re-arming-what-it-covered-by-accident.md) : une forme plus étroite perd ce que la large couvrait par accident.
- Une découverte par ancêtres fait d'une racine scratch hors worktree une surface de configuration.
- Mesurer chaque clause « prudente » sur le corpus réel avant de la garder : un faux refus coûte un tour de pilote à chaque fois.

Des résiduels hors de ce périmètre sont suivis en privé.
