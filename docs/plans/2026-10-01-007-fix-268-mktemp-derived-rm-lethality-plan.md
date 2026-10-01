---
ticket: cpp#268 / mika#2626
kind: fix
class: lethality-only (sur-refus létal — `rm -rf "$VAR"` où VAR est une dérivation `$(mktemp -d)` du MÊME commande, puits non couvert par le carve source cpp#201)
status: landed (helpers tier1 + câblage `permissions._denial_is_terminal` + tests + docs ; édition appliquée directement, aucun blocage [Security Weaken]) ; gate-KO `bdc43ff` CLÔTURÉ (couverture des FORMES de réassignation) PUIS re-gate-KO `f44b43d` CLÔTURÉ en INVERSANT la garde en fail-closed (règle « lecture seule, sinon terminal » — `_live_scratch_source`, partagée ; même trou refermé sur le puits mkdir/chmod cpp#266, axe-A direct dérouté sous `for_lethality`)
---

# `rm -rf "$VAR"` sur une variable issue de `$(mktemp -d)` du même commande n'est pas un refus terminal — Plan (cpp#268)

## Goal Capsule

Groom **93bac846** (mika#2626) mort TERMINAL au tour 12 en reproduisant un défaut
git dans un dépôt jetable. La dernière ligne est le tueur :

```
cd /tmp 2>/dev/null; B=$(mktemp -d); C=$(mktemp -d); git -C "$B" init --bare -q; git clone -q "file://$B" "$C" … ; rm -rf "$B" "$C"
```

Le refus est **légitime** (chaîne `;`, multi-ligne, `cd` hors worktree) et RESTE.
But : létalité seule, admission byte-identique — la commande reste DENY, seul
`_denial_is_terminal` bascule True→False. `B`/`C` viennent de `$(mktemp -d)` : un
répertoire neuf sous `$TMPDIR`/`/tmp` par construction. L'axe A connaissait la
SOURCE mktemp (carve redirect cpp#201) mais pas le PUITS `rm -rf`.

## Cause racine (vérifiée à la source, HEAD `098dee3`, `MIKA_PILOT_CONTAINED=1`, worktree git temp)

`_denial_is_terminal(verbatim)` = True. `is_tier3_dangerous_for_lethality(verbatim)`
= True (le verbe `rm -rf`). Les 12 segments de `_split_compound_command` : seul le
dernier, `rm -rf "$B" "$C"`, est le danger prouvé ; le reste (11 lignes git) n'est
PAS `tier3_for_lethality` — prouvé par probe : `is_tier3_dangerous_for_lethality`
sur la jointure des 11 premiers segments = False. Donc en retirant le segment `rm`,
le reste est survivable — exactement le mécanisme cpp#213 (`rm_confined_to_pilot_
scratch`). Il manquait un prédicat d'opérande « variable live mktemp-scratch ».

## Le fix (létalité seule, admission byte-identique)

`tier1.py` (helpers NOUVEAUX, jamais appelés par l'axe d'admission) :

- `_live_scratch_source(command, var)` (PARTAGÉ, re-gate-KO `f44b43d` — règle
  INVERSÉE) : unique point des deux carves — le puits `rm` mktemp (cpp#268) ET le
  puits mkdir/chmod scratch transitif (cpp#265/#266). (1) `_last_establishing_
  assignment` trouve la DERNIÈRE assignation `NAME=value` début-de-commande de
  `var` (reconnaissance POSITIVE de la source — `("mktemp", <subst>)` via le groupe
  `mk` entier de `_LEADING_ASSIGN_OP_RE`, ou `("value", <texte>)`, préfixe mot-clé
  `export`/`readonly`/`local`/`declare`/`typeset` [-flags] via
  `_DECL_KEYWORD_PREFIX_RE` ; un sous-shell `(…)` ne se propage pas et n'établit
  jamais). (2) `_var_reassigned_nonread_after` scanne la région APRÈS cette source
  jusqu'au puits : le nom ne peut y apparaître QU'EN LECTURE — une expansion nue
  `$B`/`${B}`/`"$B"` avec un opérateur NON-affectant (`:-` `:+` `:?` `#` `##` `%`
  `%%` `/` `^` `,` `:off:len`). TOUTE autre occurrence (`B=`, `export B=`, `B+=`,
  groupe d'accolades `{ B=…; }`, affectation-par-défaut `${B:=…}`/`${B=…}`,
  `read`/`IFS= read`/`mapfile`/`readarray`/`getopts B`, `let B=…`/`((B=…))`/
  `$((B=…))`, `printf -v B`, `unset B`, `for B in …`, indirection dynamique
  `"$NAME=…"`, `eval`, ou le nom en position de commande) est FAIL-CLOSED →
  non-scratch → terminal. Le nom est reconnu à une frontière de mot / `${` / `$`
  (donc `$BAR` ≠ `$B`, le `C` d'un flag `-C` n'est pas une occurrence), et un nom
  dans un littéral quoté non-expansé (`read -p "type B"`, `echo "B=/etc"`) n'en est
  pas une. Linéaire, pas de quantificateur imbriqué (cpp#250 — pas de ReDoS).
  « On arrête d'énumérer les écritures ; seules les lectures survivent. »
- `_var_is_live_mktemp_scratch(command, var)` : délègue à `_live_scratch_source` et
  renvoie True ssi la source établissante est `("mktemp", …)` ET le nom est en
  lecture seule jusqu'au puits. Toute réassignation hors scratch — énumérée ou non
  — rend le puits terminal. `_last_real_assignment_value` ne pouvait pas servir de
  scanner mktemp : son charset tronquait `$(…)` sur `(` — d'où le `mk` gardé entier.
- `_rm_operand_is_mktemp_scratch(command, operand)` : réutilise
  `_MKTEMP_SCRATCH_TARGET_RE` (ancré, pas de `..` dans la queue → `"$X/../.."`
  reste terminal) + `_var_is_live_mktemp_scratch`.
- `rm_targets_mktemp_scratch(command)` : jumeau structurel de
  `rm_confined_to_pilot_scratch` — retire chaque segment `rm`/`rmdir` dont TOUS les
  opérandes sont des variables live mktemp-scratch, re-vérifie le reste avec
  `is_tier3_dangerous_for_lethality` inchangé.

`permissions.py` : import `rm_targets_mktemp_scratch` + une clause
`and not rm_targets_mktemp_scratch(command)` dans le `and`-chain de
`_denial_is_terminal` (après `rm_confined_to_pilot_scratch`), court-circuitée par le
`is_tier3_dangerous_for_lethality` déjà en tête. Les vetos redirect/destination
sous le carve tournent sur la commande COMPLÈTE (un `rm` carvé qui redirige aussi
hors worktree est ré-armé là). `is_tier3_dangerous`, `is_tier1_auto_approve`,
`TIER3_PATTERNS`, l'égress et les règles YAML INCHANGÉS.

## Acceptance criteria

- [x] **AC1 — source reconnue, jamais réaffectée.** `X=$(mktemp -d)`,
  `mktemp -d -p /tmp`, `mktemp -d /tmp/x.XXXX`, forme backtick, reconnus comme
  dérivation de scratch via `_var_is_live_mktemp_scratch` (LAST-WINS). Unité
  `_var_is_live_mktemp_scratch` : `X=$(mktemp -d)`→True, `…; X=/`→False,
  `X=/; X=$(mktemp -d)`→True, `echo "X=$(mktemp -d)"`→False, mauvaise var→False.
- [x] **AC2 — puits survivable.** `rm -rf "$X"` dont tous les opérandes sont de
  telles variables → `_denial_is_terminal` False APRÈS (True AVANT, prouvé à la
  source). Le VERBATIM 93bac846 devient survivable. Opérandes multiples
  `rm -rf "$B" "$C"`, `rmdir "$X"`, queue sûre `"$X/sub"` carvés.
- [x] **AC3 — négatifs restent TERMINAUX (chacun via `_denial_is_terminal`).**
  `rm -rf "$HOME"` ; `rm -rf "$X/../.."` (traversée) ;
  `X=$(mktemp -d); X=/; rm -rf "$X"` (réassigné — LAST-WINS) ;
  `X=$(mktemp -d); X=$(curl …); rm -rf "$X"` (subst non-mktemp) ;
  `rm -rf "$UNSET"` (non assigné) ; `rm -rf "$(curl …)"` (command-sub, pas var) ;
  `rm -rf "$X" /etc` (mélange) ; `… && git reset --hard` (verbe chaîné) ;
  `rm -rf /`. `B=""`→`rm -rf ""` inerte (reste terminal, inoffensif).
- [x] **AC5 — gate-KO `bdc43ff` : réassignation hors scratch par TOUTE forme reste
  TERMINALE** (le trou : la garde ne voyait que `NAME=` nu). CHAQUE cas vu ROUGE
  avant le fix, maintenant terminal via `_denial_is_terminal`, sur le puits `rm` :
  `export B=/` ; `readonly B=/` ; `local B=/` ; `declare B=/` ; `typeset B=/` ;
  `declare -x B=/` ; `B+=x` ; `read B` ; `read -r B` ; `read a b B` ;
  `for B in /etc /; do :; done` ; sous-shell `(B=/tmp/ok); rm -rf "$B"` ; `unset B`.
  Formes porteuses de valeur : la VALEUR est extraite et passée au test scratch
  existant (pas de terminalisation en bloc sur le mot-clé) — d'où `export
  X=$(mktemp -d); rm -rf "$X"` et `declare -x X=$(mktemp -d); rm -rf "$X"` RESTENT
  survivables. Formes à valeur inconnaissable (`+=`/`read`/`for`/sous-shell) :
  fail-closed → terminal. MÊME trou refermé sur le puits mkdir/chmod cpp#266 (voir
  section dédiée).
- [x] **AC4 — gate MPC.** Admission byte-identique : 0 diff
  (`is_tier3_dangerous` / `is_tier1_auto_approve` / décision policy) contre `main`
  pristine (`75b5c36`, tête pré-fix) sur un échantillon large (45 commandes, dont
  toutes les formes de réassignation visées). Un seul écart de létalité par forme
  visée. Chaîne de 50 réassignations : < 1 ms/call (< 50 ms), linéaire, pas de
  ReDoS (leçon cpp#250). Suite complète verte (1664 passed, +4 méthodes de test),
  ruff/mypy clean, verify-pipeline GREEN.

## Clôture du trou partagé — couverture des formes de réassignation (cpp#268 + cpp#266)

Le gate MPC (`bdc43ff`) a mesuré que la garde de réassignation (LAST-WINS sur les
assignations début-de-commande) ne reconnaissait qu'une assignation NUE `NAME=`.
Une réassignation de la variable scratch HORS scratch par toute AUTRE forme était
invisible, et le puits restait survivable à tort :

- cpp#268 (puits `rm`) : `X=$(mktemp -d); export X=/; rm -rf "$X"`.
- cpp#266 (puits mkdir/chmod, DÉJÀ SERVI sur `main` `098dee3`) :
  `SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; export SCRATCH_ROOT=/etc;
  mkdir -p "$SCRATCH_ROOT/x"` — idem `declare SCRATCH_ROOT=/etc`.

Les DEUX chemins partageaient exactement le même mécanisme de résolution LAST-WINS
(cpp#268 via `_var_is_live_mktemp_scratch`, cpp#266 via `_last_real_assignment_value`
→ `_value_roots_at_scratch` → `_is_transitive_ce_scratch_mkdir_target`). Premier
correctif : un scanner partagé `_last_var_write` ÉNUMÉRANT les formes d'écriture.

## Re-clôture en fail-closed — règle INVERSÉE « lecture seule » (re-gate `f44b43d`)

Énumérer les écritures est un jeu de taupe : le re-gate MPC (`f44b43d`) a prouvé
QUATRE formes encore invisibles au scanner énumérant — `{ B=/etc; }` (groupe
d'accolades), `IFS= read -r B` (une affectation-préfixe masquant le `read`),
`let B=1` / `((B=1))` (arithmétique), `: ${B:=/etc}` (expansion
affectation-par-défaut) — sur les DEUX puits (`rm` cpp#268 et mkdir/chmod cpp#266).
La correction INVERSE la garde, une fois, dans la nouvelle fonction partagée
`_live_scratch_source` : (1) trouver la DERNIÈRE assignation établissant le scratch
(source POSITIVE conservée) ; (2) scanner la région jusqu'au puits — le nom ne peut
y apparaître QU'EN LECTURE (expansion nue à opérateur non-affectant) ; TOUTE autre
occurrence, énumérée ou non, est fail-closed → terminal. `_var_is_live_mktemp_
scratch` et `_last_real_assignment_value` délèguent toutes deux à cette fonction,
donc la fermeture s'applique identiquement aux deux puits. Un sous-shell `(…)` est
sauté (non propagé) ; `export -n B`/`readonly B` (attribut sans `=`) reste une
lecture ; les positifs (verbatim 93bac846, `export X=$(mktemp -d)`, préambule
ce-code-review en `declare`, `"$X/sub"`) satisfont « lecture seule » et survivent.

Pour le puits mkdir cpp#266, l'axe-A DIRECT `_is_sanctioned_tmp_scratch` →
`_is_ce_scratch_variable_ref` (résolveur à plat) court-circuitait le carve transitif
inversé. L'axe-A est donc DÉROUTÉ vers le carve transitif inversé UNIQUEMENT sous
`for_lethality=True` (via un paramètre `for_lethality` passé à
`_is_sanctioned_tmp_scratch`) ; sur le chemin d'admission/refus (`for_lethality=
False`) l'axe-A plat est INCHANGÉ. LÉTALITÉ SEULE sur les deux chemins : l'axe
d'admission (`_ce_scratch_variable_names` / `_last_assignment_value` à plat) est
INCHANGÉ (byte-identique vérifié, 0 écart sur un échantillon large).

## Fire-Disposition

- **Feu** : deny TERMINAL d'un nettoyage `rm -rf "$B" "$C"` sur des `$(mktemp -d)`
  du même commande (groom 93bac846 mika#2626 mort, 3,50 USD, PIPELINE_INCOMPLETE)
  — classe de dérivation transitive de cpp#265/#266, puits `rm` non couvert.
- **Éteint** : un `rm`/`rmdir` dont TOUS les opérandes sont des variables que le
  MÊME commande garde comme scratch `$(mktemp -d)` vivant n'est plus un danger
  prouvé sur le chemin létalité ; le refus reste, seule la mort disparaît.
- **Vérif de sortie** : helpers `tier1.py` + clause `permissions.py` ; tests GREEN
  (`TestCpp268RmTargetsMktempScratch` + `test_helper_reassignment_forms_last_wins` ;
  `test_cpp268_rm_on_mktemp_scratch_is_survivable_but_still_refused` étendu
  (positifs `export`/`declare` mktemp) +
  `test_cpp268_reassignment_out_of_scratch_any_form_is_terminal` +
  `test_cpp266_mkdir_reassignment_out_of_scratch_is_terminal` +
  `test_transitive_reassignment_out_via_keyword_form` ;
  `test_cpp268_admission_is_byte_identical_only_lethality_flips`) ; probe
  `_denial_is_terminal` AVANT/APRÈS sur verbatim + chaque positif + chaque négatif
  (dont chaque forme de réassignation, deux puits) ; diff admission 0 contre `main`
  `75b5c36` (45 commandes) ; ruff/mypy clean ; suite (1664) verte ; verify-pipeline
  GREEN. Édition appliquée directement — pas de blocage [Security Weaken], pas de
  fenêtre manuelle requise.
- **Résidu (nommé)** : (1) `_mktemp_scratch_variable_names` (cpp#201, non
  last-wins) N'est PAS réutilisé comme prédicat — il manquerait la réassignation ;
  `_var_is_live_mktemp_scratch` est la version last-wins qui SUBSUME l'appartenance
  cpp#201 pour ce puits. (2) Une valeur bare non-quotée avec `$(…)` autre que
  mktemp se relit tronquée mais est traitée comme non-mktemp (fail-closed, reste
  terminal) ; une valeur `read`/`for`/`+=`/sous-shell est inconnaissable → fail-closed
  terminal (jamais faux-survivable). (3) La chaîne transitive `B=$C` où `C` est mktemp n'est PAS suivie
  (hors scope ticket) → fail-closed terminal, jamais faux-positif. (4) Verbe
  `rm`/`rmdir` seulement ; un `/bin/rm` path-qualifié reste terminal (contrat
  `_rm_segment_operands` cpp#213).

## Références

- Solution : `docs/solutions/security-issues/an-rm-on-a-mktemp-derived-variable-is-a-scratch-sink-not-a-terminal-denial.md`.
- Siblings : cpp#201 (carve redirect SOURCE mktemp), cpp#213 (carve rm
  `.pilot-scratch` — jumeau structurel), cpp#265/#266 (dérivation scratch
  transitive, LAST-WINS). Doctrine cpp#205 (défaut survivable), cpp#250 (ReDoS).
- Code : `src/claude_pilot/tier1.py`, `src/claude_pilot/permissions.py`. PR : cpp#268.
