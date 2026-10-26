---
title: "Une écriture sed `w FILE` se juge au moment où GNU ouvre le fichier, pas seulement à sa grammaire"
date: 2026-10-26
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: medium
symptoms:
  - "`sed -n 'w /etc/evil' f`, `sed 's/a/b/w /etc/x' f` et `sed 'W /etc/x' f` étaient refusées mais non terminales, alors que `sed -i` sur le même chemin terminait la session"
  - "la première version du lecteur de destinations sed, écrite sur la grammaire « évidente », laissait survivables `b x w F`, `s/a/b/ w F`, `/[/]/w F`, `-e 'w F' -e Z` et `-e 'w F' --bogus`, et rendait terminal `s/[/]/w/`, qui n'écrit rien"
root_cause: missing_validation
resolution_type: code_fix
related_components:
  - permissions
tags: [permissions, bash, sed, sed-w, lethality, terminal-deny, destination-veto, worktree-containment, gnu-sed, differential-test, corpus-replay, for-lethality, heredoc, claude-pilot-230, claude-pilot-229]
---

# Une écriture sed `w FILE` se juge au moment où GNU ouvre le fichier

## Problem

sed écrit des fichiers autrement que par `-i` : la commande `w FILE`, la commande `W FILE` et le drapeau `w FILE` d'un `s///` ouvrent FILE en écriture, n'importe où sur le disque. Avant cpp#230, ces commandes étaient refusées (sed n'est admis que sous deux formes fermées qui ne peuvent pas porter de `w`) mais **non terminales**, alors que `sed -i` sur le même chemin hors worktree terminait la session pilote.

## Symptoms

Rejeu sur `main` @ ec63e2d, cwd = le worktree :

| commande | `_denial_is_terminal` |
|---|---|
| `sed -n 'w /etc/evil' crates/guards.rs` | False |
| `sed 'w /tmp/x' f` | False |
| `sed -e 's/a/b/w /etc/x' f` | False |
| `sed --expression='s/a/b/w /etc/x' f` | False |
| `sed 'W /etc/x' f` | False |
| `sed -i 's/a/b/' /etc/x` | True |

## What Didn't Work

**Écrire le lecteur à partir de la grammaire « évidente ».** La première version de `sed_write_destinations` (deuxième commit de la branche de cpp#230, avant la revue de code) suivait une grammaire raisonnable de sed : labels finis par `;`, regex fermée au premier délimiteur non échappé, drapeaux de `s` collés au délimiteur, et un script qui ne s'analyse pas traité comme « aucune destination prouvée ». Ses 66 tests unitaires passaient, et la table avait été vérifiée contre GNU sed 4.10, mais seulement sur les formes qu'elle contenait. La revue de code a trouvé cinq écarts, tous confirmés par un validateur indépendant qui a exécuté GNU sed 4.10 :

| forme | GNU 4.10 | lecteur v1 |
|---|---|---|
| `sed -n 'b x w F' f` | crée F (le label finit sur un blanc) | `[]` |
| `sed 's/a/b/ w F' f` | crée F (blancs admis avant les drapeaux) | `[]` |
| `sed -n '/[/]/w F' f` | crée F (`[/]` est une classe) | `[]` |
| `sed 's/[/]/w/' f` | n'écrit rien, remplace `/` par `w` | `['/']`, donc **terminal** |
| `sed -e 'w F' -e Z f` | crée F, puis échoue sur `Z` | `[]` |
| `sed -e 'w F' --bogus f` | crée F, puis rejette l'option | `[]` |

Les quatre premiers écarts sont de grammaire. Les deux derniers ne le sont pas, et ce sont eux qui portent la leçon.

**Traiter « le script est en erreur » comme « il n'écrit rien ».** C'est l'intuition naturelle (sed refuse le script, donc il ne tourne pas), et elle est fausse. GNU sed ouvre (crée, tronque) chaque fichier `w` **pendant la compilation**, commande par commande, avant de rencontrer l'erreur qui suit. Mesuré : `sed -n 'b end;w br;:end' in` échoue sur le label manquant et crée quand même `br;:end`. Il en va de même entre options : getopt lit chaque `-e`, le compile tout de suite, puis rejette une option inconnue plus loin (`sed -e 'w F' --bogus f` crée F). Un `--sandbox` ne refuse que les `w` qui viennent **après** lui (`sed -e 'w F' --sandbox f` crée F, `sed --sandbox -e 'w F' f` non).

## Solution

Trois pièces, toutes du côté de la létalité :

- `tier1.sed_write_destinations` (`src/claude_pilot/tier1.py:6512`) lit les destinations comme GNU sed 4.x. Une marche d'options complète traite les groupes (`-ne`), le `-i` qui prend la suite comme suffixe (`-ie`), les préfixes longs (`--expr=`) et les redirections shell glissées dans les jetons. Vient ensuite une lecture linéaire du script joint par sauts de ligne, sans regex à retour arrière (cpp#250) : labels finis par `;` ou un blanc (`_sed_label_end`, `:6416`), classes entre crochets lues d'un bloc dans les regex seulement (`_sed_bracket_end`, `:6288`), blancs admis avant et entre les drapeaux de `s`.
- **Le lecteur rend ce qui a été compilé avant l'arrêt.** `_sed_script_write_targets` (`:6424`) rend les destinations déjà lues quand l'analyse échoue. La marche d'options rend celles des `-e` déjà lus quand elle rencontre une option rejetée, `--help`, `--version` ou `--sandbox` (fermeture `compiled()`, `:6532`).
- `permissions._sed_write_veto_reason` (`src/claude_pilot/permissions.py:1566`) juge chaque destination comme les autres destinations d'écriture. `/dev/stdout`, `/dev/stderr` et `/dev/null` sont exemptés. Une racine `$` ou `~` est vetoée. Viennent ensuite `is_within_project` puis le plan de contrôle. `/tmp` n'a pas de carve, comme pour `sed -i`. Le veto est appelé en tête de `_destination_veto_reason` sous `if for_lethality:` (`:1664`), donc l'admission reste identique par construction. Les lignes de corps de heredoc sont vidées avant lecture (`_blank_heredoc_bodies`, `tier1.py:6593`), parce que `_split_compound_command` coupe sur les sauts de ligne et qu'un message de commit citant `sed 'w /etc/x' f` se lirait sinon comme une commande.

## Why This Works

Une destination d'écriture n'est pas une propriété du script valide : c'est l'effet de bord de sa **compilation**. Un lecteur qui raisonne seulement sur la validité du script se trompe dans les deux sens. Il manque les fichiers ouverts avant une erreur, et il invente des destinations quand il coupe une regex au mauvais endroit. Suivre l'ordre d'ouverture de GNU (commande par commande, option par option) fait correspondre « destination lue » et « fichier que sed crée ».

La porte `for_lethality` garde la question de l'admission hors de portée. Une erreur du lecteur ne peut que laisser survivable une écriture toujours refusée, ou rendre terminal un refus qui n'écrit rien. Elle ne peut jamais admettre une commande.

## Prevention

- **Différentiel contre le vrai binaire, pas contre sa propre lecture de la doc.** La table de `test_cpp230_sed_write_destinations` (`tests/test_tier1.py`) est vérifiée en exécutant chaque ligne avec GNU sed dans un dossier jetable. Les destinations absolues y sont réécrites en noms locaux, et on compare les fichiers créés à la liste attendue. Mesure de session du 2026-10-26 : 89 lignes concordent. Le seul écart est `/usr/bin/sed`, absent de l'hôte Gentoo (sed y est `/bin/sed`). Toute nouvelle forme ajoutée à la table passe par ce différentiel avant d'être tenue pour vraie.
- **Pour chaque branche « échec » d'un lecteur, se demander ce que l'interpréteur a déjà fait avant d'échouer.** « Inanalysable donc survivable » (doctrine cpp#205) ne vaut que pour ce que le lecteur n'a pas vu. Ce qu'il a lu avant l'échec est prouvé.
- **Rejouer le corpus pilote avant et après.** Mesure de session du 2026-10-26 : 7 788 commandes réelles mentionnant sed (926 transcripts sous `~/.mika/data/pilot-transcripts/`) : 0 bascule de létalité dans un sens comme dans l'autre, 0 changement de veto côté refus. C'est la preuve qu'aucun pilote réel n'est touché, et que la porte `for_lethality` tient sur une population, pas seulement sur des cas choisis.
- **Épingler les deux sens dans le même test.** Formes terminales et survivables cohabitent dans `tests/test_permissions.py` (`_CPP230_TERMINAL`, `_CPP230_SURVIVABLE`). Chaque correction de revue a son cas, rouge sur la première version du lecteur et vert après.
- **Résidus déclarés, épinglés quand c'est possible.** Un script dans un fichier (`-f`) est inconnaissable. Un mot shell qui cache le texte à shlex (`$'\x77 /etc/x'`, `"$C /etc/x"`) aussi. sed préfixé (`env sed`, `xargs sed`, `x=$(sed …)`) n'est pas lu. Sur le corpus, 48 segments sur 8 052 ne s'analysent pas, tous des adresses calculées par le shell (`$((l-8))`, `$(grep -n …)`), aucun avec `w`.
