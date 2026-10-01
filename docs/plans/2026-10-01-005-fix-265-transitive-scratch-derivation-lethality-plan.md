---
ticket: cpp#265 / mika#1960
kind: fix
class: lethality-only (sur-refus syntaxique — un token `VAR=` dans un argument quoté lu comme réassignation last-wins, cassant le résolveur de dérivation scratch transitive mika#2562)
status: landed (helpers + résolveur + tests + docs ; câblage `permissions._denial_is_terminal` DÉJÀ présent depuis cpp#223, aucune édition guardrail requise)
---

# L'axe A suit une dérivation scratch transitive `RUN_DIR="$SCRATCH_ROOT/…"` même quand la commande se termine par `echo "RUN_DIR=$RUN_DIR"` — Plan (cpp#265)

## Goal Capsule

Pilote **819e8f6b** (implement de mika#1960) mort TERMINAL à 49 tours en entrant
dans `/ce:code-review`, sur le préambule de scratch OFFICIEL du skill, en une
ligne :

```
SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; mkdir -p "$SCRATCH_ROOT"; chmod 700 "$SCRATCH_ROOT"; RUN_ID="$(date +%Y%m%d-%H%M%S)-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' ')"; RUN_DIR="$SCRATCH_ROOT/ce-code-review/$RUN_ID"; mkdir -p "$RUN_DIR"; chmod 700 "$RUN_DIR"; echo "RUN_DIR=$RUN_DIR"; python3 .../ce-code-review/scripts/run-log.py event --run-dir "$RUN_DIR" --start scope
```

But : létalité seule, admission byte-identique — la commande reste DENY, seul
`_denial_is_terminal` bascule True→False.

## Cause racine (vérifiée à la source, HEAD `1d7c022`, `MIKA_PILOT_CONTAINED=1`, worktree git temp)

`tier3_for_lethality` = False ; `_redirect_destination_veto_reason` = None. Le
tueur est `_destination_veto_reason(…, for_lethality=True)` sur le segment
`mkdir -p "$RUN_DIR"` : `destination '$RUN_DIR' is rooted at an unresolved
variable/tilde ($/~)` (cpp#218).

La machinerie transitive mika#2562 (`_is_transitive_ce_scratch_mkdir_target` →
`_value_roots_at_scratch`) EXISTE déjà et le câblage `for_lethality` aussi
(`permissions.py:1727`). **L'hypothèse initiale du ticket est RÉFUTÉE** : le
résolveur suit déjà un suffixe portant `$RUN_ID` — ce n'est pas la cause.

La VRAIE cause, épinglée par probe : `_value_roots_at_scratch` /
`_is_transitive_ce_scratch_mkdir_target` résolvaient la valeur de `$RUN_DIR` via
`_last_assignment_value`, un scan **PLAT** (`_ANY_ASSIGNMENT_RE.finditer` sur
toute la commande, LAST-WINS). Le `echo "RUN_DIR=$RUN_DIR"` final porte le texte
entre guillemets `RUN_DIR=$RUN_DIR` ; le lookbehind `(?<![\w$])` est satisfait par
le `"`, donc la regex matche ce texte comme une réassignation PLUS TARDIVE.
LAST-WINS résout alors `RUN_DIR` vers l'auto-référence `$RUN_DIR"`, qui ne root
sur rien → `_value_roots_at_scratch` False → veto cpp#218 TERMINAL.

Probe : `_last_assignment_value(verbatim, "RUN_DIR") == '$RUN_DIR"'` ; sans le
`echo`, `'$SCRATCH_ROOT/ce-code-review/$RUN_ID'` et le résolveur renvoie True. Le
bug est un scan last-wins **aveugle au quoting**, pas la gestion du suffixe. Même
famille que cpp#258 (un artefact de parsing lu comme une évasion prouvée).

## Le fix (létalité seule, admission byte-identique)

`tier1.py` (helpers NOUVEAUX, jamais appelés par l'axe d'admission) :

- `_last_real_assignment_value(command, var)` : LAST-WINS de la dernière
  assignation RÉELLE en position de début-de-commande. Parcourt
  `_split_compound_command` et matche `_LEADING_ASSIGNMENT_RE` ancrée en tête de
  chaque segment lstrippé (+ les mots-préfixes d'assignation séparés par espace).
  `echo "RUN_DIR=$RUN_DIR"` commence par `echo`, donc son token entre guillemets
  n'est jamais compté.
- `_suffix_is_contained(value, command, depth)` : un composant de suffixe est
  contenu SSI aucun `..` littéral, et chaque `$VAR`/`${VAR}` qu'il nomme ET qui
  est assigné est lui-même contenu (récursion bornée `_TRANSITIVE_SCRATCH_MAX_DEPTH`).
  Ferme en plus la traversée indirecte `EVIL=../../etc; D="$SR/$EVIL"`.
- `_value_roots_at_scratch` / `_is_transitive_ce_scratch_mkdir_target` suivent la
  chaîne racine via `_last_real_assignment_value` et gardent le suffixe via
  `_suffix_is_contained`.

`_last_assignment_value` et `_ANY_ASSIGNMENT_RE` sont INCHANGÉS — l'axe
d'admission (`_ce_scratch_variable_names` → `_is_ce_scratch_variable_ref` →
`_is_sanctioned_tmp_scratch`) continue de les utiliser, donc l'admission est
byte-identique. `permissions.py` INCHANGÉ (le câblage `for_lethality` existait
déjà). Aucune édition guardrail, aucun blocage [Security Weaken].

## Acceptance criteria

- [x] **AC1 — survivable.** Le VERBATIM #265 (avec `echo` + `python3 … run-log.py`)
  → `_denial_is_terminal` False APRÈS (True AVANT, prouvé à la source). Plus le
  suffixe-littéral (`RUN_DIR="$SCRATCH_ROOT/ce-code-review/x"`) et la forme à var
  imbriquée (`MID="$SR/ce-code-review"; RUN_DIR="$MID/$RUN_ID"`).
- [x] **AC2 — restent TERMINAUX (chacun testé via `_denial_is_terminal`).**
  `RUN_DIR="$HOME/x"` ; `D="$SCRATCH_ROOT/../../etc"` (traversée) ;
  `mkdir -p "$UNSET/x"` et `D="$UNSET/x"` (racine non assignée) ;
  `D="$(curl …)"` (racine command-sub) ; chaîne de racine `$HOME` ; réassignation
  hors scratch (`SR=/tmp/ok; SR=$HOME/evil`) ; traversée indirecte
  `EVIL=../../etc; D="$SR/$EVIL"`. Egress inchangé.
- [x] **AC3 — admission byte-identique.** 0 diff sur l'axe d'admission
  (`_is_ce_scratch_variable_ref`, `_ce_scratch_variable_names`,
  `_last_assignment_value`) contre `main` pristine sur un large échantillon.
  `_destination_veto_reason(…, for_lethality=False)` sur le VERBATIM reste
  non-None (toujours refusé). Tier1 gate `is_tier1_auto_approve` JAMAIS touché.
  Le carve est un no-op sur `for_lethality=False`.
- [x] **AC4 — non-réouverture / bornes.** Nouveaux helpers appelés uniquement par
  le chemin transitif (lui-même gaté `for_lethality`) ; `is_tier3_dangerous`,
  `is_tier3_dangerous_for_lethality`, `TIER3_PATTERNS` inchangés. Chaîne de 50
  assignations : 2,01 ms (< 50 ms), linéaire, pas de ReDoS (leçon cpp#250). Suite
  complète verte (1648 passed), ruff/mypy clean, verify-pipeline GREEN.

## Fire-Disposition

- **Feu** : deny TERMINAL du préambule de scratch OFFICIEL de `ce-code-review`
  (mika#1960 tueur 819e8f6b) — classe systémique : TOUTE revue `/ce:code-review`
  lancée par un pilote émet cette ligne, donc tout pilote `ce-code-review` meurt.
  **Éteint** : la dérivation `RUN_DIR="$SCRATCH_ROOT/…/$RUN_ID"` enracinée
  transitivement dans un scratch `/tmp` reconnu n'est plus un danger prouvé sur le
  chemin létalité, même quand la commande mentionne `$RUN_DIR` dans un argument
  quoté ; le refus reste, seule la mort disparaît.
- **Vérif de sortie** : helpers `tier1.py` + tests GREEN
  (`TestCeScratchSanctionUnit` +6, `TestCpp265TransitiveScratchDerivationWithEcho`
  +3) ; probe `_denial_is_terminal` AVANT/APRÈS sur verbatim + positifs + chaque
  négatif ; diff admission 0 contre `main` ; ruff/mypy clean ; suite complète
  (1648) verte ; verify-pipeline GREEN. Édition appliquée directement (tier1.py
  seul) — pas de blocage guardrail, pas de fenêtre manuelle requise.
- **Résidu (nommé)** : (1) le contrat mika#2562 « suffix var NON assignée =
  survivable » est CONSERVÉ volontairement (expansion vide sous le scratch,
  inoffensive) ; la négative initiale du ticket « suffix var non assignée →
  terminal » n'est pas adoptée (modèle mental erroné + régresserait
  `test_transitive_run_dir_roots_at_scratch`). (2) Une valeur de suffixe
  command-sub (`$RUN_ID=$(date…)`) est acceptée sans inspecter sa valeur
  d'exécution — sa sortie reste un composant SOUS le scratch ; seul `..` littéral
  (direct ou via var assignée) traverse et est rejeté. (3) Une valeur bare
  (non-quotée) avec `$(…)` tronque sur `(` (même charset que l'ancien scan) →
  fail-closed (reste terminal), hors scope ; le préambule réel quote toutes ses
  valeurs.

## Références

- Solution : `docs/solutions/security-issues/a-var-assignment-inside-a-quoted-argument-is-not-a-reassignment.md`.
- Siblings : cpp#223/c0b0c08 (re-admission + carve transitif mika#2562), cpp#224
  (axe A admission last-wins), cpp#258 (opérande non évaluable ≠ évasion prouvée),
  cpp#218/#211/#154 D3 (veto `$`/`~`-rooted anti-respelling). Doctrine cpp#205
  (défaut survivable). Code : `src/claude_pilot/tier1.py`. PR : cpp#265.
