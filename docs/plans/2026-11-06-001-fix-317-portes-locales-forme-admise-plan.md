---
title: "fix(permissions): la consigne et le message de refus nomment la forme admise des deux portes locales (#317)"
type: fix
status: done
date: 2026-11-06
origin: senara-solutions/claude-pilot#317
---

# Portes locales : nommer la forme admise (#317)

## Contexte

Le pilote lance `verify-pipeline` et `verify-bundled-skills` sous trois formes
que le classifieur refuse, alors qu'une forme admise existe pour chacune. Le
ticket mesure 116, 3 et 114 refus. Le défaut tient à la forme, pas à une règle
manquante. Le remède ne change donc pas ce qui est admis : il dit au modèle la
forme admise, avant (consigne) et après (message de refus).

## Rejeu sur main (e67ec32, dépôt git réel où `scripts/verify-pipeline.sh` est suivi)

| commande | Tier 1 | politique |
|---|---|---|
| `scripts/verify-pipeline.sh origin/main` | non | deny (défaut) |
| `PLAN_FILE=… bash scripts/verify-pipeline.sh origin/main` | non | deny (défaut) |
| `make verify-bundled-skills 2>&1 \| tail -5` | non | deny (défaut) |
| `bash scripts/verify-pipeline.sh origin/main` | oui | |
| `./scripts/verify-pipeline.sh origin/main` | oui | |
| `make verify-bundled-skills` | oui | |
| `cargo run -q --bin verify-bundled-skills` | non | allow `bash-cargo` |

## Décisions

### D1. Consigne (AC1)

Une section « Local gates » dans `_DENIED_BASH_PATTERNS_HINT_BODY`, après les
motifs refusés et avant les outils sans effet en mode headless. Elle nomme les
deux portes, leur forme admise, et les trois formes refusées : le chemin nu, le
préfixe `NOM=valeur` et le suffixe après la cible make. Le script trouve le plan
lui-même dans le diff, donc le préfixe `PLAN_FILE=` n'apporte rien.

### D2. Message de refus (AC2)

`tier1.local_gate_deny_hint(command)` parcourt les segments de
`_split_compound_command` et rend le texte de la forme admise quand un segment a
l'une des trois formes refusées :

- `scripts/verify-pipeline.sh` en tête de segment, ou précédé d'un ou plusieurs
  `NOM=valeur` (avec ou sans `bash`, `sh` ou `./`) ;
- `make verify-bundled-skills` suivi d'un jeton.

Dans `permissions.py`, sur le chemin du refus par défaut (`rule_id` absent) et
seulement si le message est encore `pd.reason`, ce texte s'ajoute à
`pd.reason`. La décision et `interrupt` sont calculés avant et ne lisent pas ce
texte : même refus, même létalité. Un refus par règle explicite garde son
message, et le message de cpp#201 (veto de destination) garde la priorité.

Les textes de forme admise sont des constantes de `tier1.py`, et un test vérifie
qu'ils figurent dans la consigne, pour que les deux canaux ne divergent pas.

### D3. Aucune extension d'admission (AC3)

Aucun prédicat de Tier 1 ni aucune règle de politique ne change. Un test rejoue,
dans un dépôt git réel, les trois formes refusées par le vrai callback (refus,
non terminal) et les formes admises (admises). Les deux regex nouvelles ne
servent qu'au message ; un test borne leur temps sur une entrée hostile.

### D4. Sonde (AC4)

Hors code. Elle se fait sur les 14 jours qui suivent le deploy, sur le ticket.

## Hors périmètre

- Toute extension d'admission (chemin nu, préfixe d'environnement, suffixe
  après make) : gate Vincent.
- Les plans de mika qui écrivent la forme nue.
- Aucun ticket ouvert (gel jusqu'au 01/12).

## Acceptance criteria

- AC1 : la consigne nomme les deux portes, leur forme admise et les trois formes
  refusées.
- AC2 : un refus par défaut sur chacune des trois formes porte la forme admise ;
  décision et létalité inchangées.
- AC3 : formes refusées toujours refusées, formes admises toujours admises, dans
  le même test.
