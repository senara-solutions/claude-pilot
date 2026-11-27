---
title: "feat(permissions): extraction de plage en ajout seul vers une bibliothèque de fixtures de mika#3178 (cpp#328)"
type: feat
status: active
date: 2026-11-27
issue: senara-solutions/claude-pilot#328
---

# Extraction de plage en ajout seul vers une bibliothèque de fixtures (cpp#328)

## Pourquoi

cpp#325 (PR #327, 46d5a3f) a ouvert la règle `bash-sed-range-extract-3178` et
une exception au plan de contrôle cpp#42, bornées aux cibles
`skills/bundled/_shared/tests/test_dispatch_lib_[a-z0-9_]+\.sh`. Le vol 2 de
mika#3178 (PR mika#3186) a mesuré qu'une chaîne de helpers de la porte de groom
traverse trois familles : extraite seule, la famille 5 rend 12 `rc=127`. Le
ticket mika#3178 range un helper partagé dans une bibliothèque, et le pilote a
voulu remplir `skills/bundled/_shared/tests/lib-dispatch-lib-groom-gate-fixtures.sh`
par `sed -n '2824,2855p' … >>`. Le plan de contrôle l'a refusé en veto
terminal, à raison : ce nom sort du motif de cpp#325. La décision du
2026-11-27 (corps de cpp#328) élargit la règle et l'exception à cette seconde
forme de cible, et à elle seule.

## Ce qui change

1. `src/claude_pilot/policies/permissions.yaml` : règle sœur
   `bash-sed-range-extract-3178-fixtures`, juste après
   `bash-sed-range-extract-3178`. Même motif que la règle de cpp#325, sauf deux
   points : la redirection est `>>` littéral (pas `>>?`), et la cible est
   `skills/bundled/_shared/tests/lib-dispatch-lib-[a-z0-9_-]+-fixtures\.sh`.
   Une règle sœur plutôt qu'une règle élargie : la distinction `>`/`>>` reste
   portée par un motif à elle, et la règle de cpp#325 ne change pas d'un octet
   (AC4).
2. `src/claude_pilot/permissions.py`, `_bash_allow_is_chain_safe` : le
   `rule_id` neuf est honoré au même endroit que celui de cpp#325.
3. `src/claude_pilot/permissions.py`, plan de contrôle : l'exception passe
   d'un triplet unique à une table de paires appariées
   `(rule_id, forme du segment, ancre du chemin)`, à côté de
   `_CONTROL_PLANE_PATTERNS`. `_is_cpp325_control_plane_exception` garde son
   nom et ses quatre conditions ; elle cherche l'entrée du `rule_id` passé, et
   n'applique que la forme et l'ancre de CETTE entrée. Les paires ne se
   croisent pas : `>` vers un nom `-fixtures` sous le `rule_id` de cpp#325
   échoue sur la forme de cpp#325, et `>>` vers `test_dispatch_lib_*` sous le
   `rule_id` neuf échoue sur la forme neuve.

## Critères

- AC1 : la forme admise passe le handler de production, sans destination-veto.
- AC2 : dans le même test, les négatifs du ticket, plus les négatifs d'AC2 de
  cpp#325 rejoués sur la nouvelle cible.
- AC3 : le test d'AC1 seul est commité d'abord et constaté rouge sur `main`.
- AC4 : `tests/test_cpp325_sed_range_extract.py` vert sans modification.
- AC5 : rejeu hors ligne, handler de production, ancien contre neuf.
- AC6 : `reason` et commentaires nomment cpp#42, cpp#328, cpp#325, mika#3178
  et le retrait avec claude-pilot#326. Le corps de #326 n'est pas édité ici :
  son amendement revient à l'opérateur.
- AC7 : ancre et couplage dans `permissions.py`, à côté de la liste cpp#42.

## Ce qui n'est pas ouvert

`>` vers un nom `-fixtures`, toute autre forme Bash vers ce nom, toute autre
source, tout autre nom sous `tests/` (dont `lib-dispatch-lib-suite.sh` et
`lib-mika-bin.sh`), `sed -i`.
