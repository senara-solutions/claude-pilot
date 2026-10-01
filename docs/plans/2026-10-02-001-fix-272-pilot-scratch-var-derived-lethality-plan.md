---
ticket: cpp#272 / mika#2623
kind: fix
class: lethality-only (sur-refus syntaxique — la racine de scratch PRESCRITE par le dépôt, `.pilot-scratch/`, portée par une variable, n'était pas reconnue par le résolveur de scratch dérivé ; seuls `/tmp` (cpp#266) et `$(mktemp -d)` (cpp#270) l'étaient)
status: landed (helpers + résolveur étendu + câblage `permissions._destination_veto_reason` DÉJÀ présent depuis cpp#265/#268 pour mkdir ; UN ajout de clause au puits redirect ; tests + docs)
---

# Une cible `.pilot-scratch/` (ou `$PWD/.pilot-scratch/`) portée par une variable est survivable — la racine de scratch prescrite rejoint le résolveur de scratch dérivé — Plan (cpp#272)

## Goal Capsule

Pilote **c722b251** (implement de mika#2623) mort TERMINAL au tour 27 en montant
une repro git jetable **là où les consignes du dépôt le demandent** — sous
`.pilot-scratch/` dans le worktree, sans `/tmp`, sans `mktemp`, sans `rm -rf` — à
la ligne 2 :

```
set -e
D=.pilot-scratch/git-probe
mkdir -p "$D"        # ← mort ici (premier préfixe terminal)
R="$PWD/$D/repo"
mkdir -p "$R"
git init -q -b main "$R"
printf 'fn main() {}\n' > "$R/main.rs"
```

But : **létalité seule, admission byte-identique** — la commande reste DENY, seul
`_denial_is_terminal` bascule True→False. C'est **n=2** de la classe « repro git
jetable » (après `93bac846`, mika#2626).

## Cause racine (vérifiée à la source, HEAD `406983c`, `MIKA_PILOT_CONTAINED=1`, worktree git réel)

Table de rejeu (le littéral est survivable, la MÊME cible via variable est terminale) :

| commande | terminal |
|---|---|
| `mkdir -p .pilot-scratch/git-probe` | False |
| `printf x > .pilot-scratch/a.txt` | False |
| `D=.pilot-scratch/git-probe; mkdir -p "$D"` | **True** |
| `R="$PWD/.pilot-scratch/repo"; mkdir -p "$R"` | **True** |
| `D=.pilot-scratch/x; printf x > "$D/a.txt"` | **True** |
| `R="$PWD/.pilot-scratch/r"; printf x > "$R/a"` | **True** |

Le littéral `.pilot-scratch/...` passe `is_within_project` (contenu dans le
worktree) → survivable. Dès que la cible passe par une variable, le puits mkdir
tombe sur le veto cpp#218 (`$`-rooted mkdir) et le puits redirect sur le veto
cpp#154 D3 (`$`-rooted redirect), tous deux TERMINAUX. La machinerie de scratch
DÉRIVÉ de cpp#265/#268/#270 (`_live_scratch_source`, `_value_roots_at_scratch`,
la règle « lecture seule » inversée) couvre déjà ce cas — MAIS ses reconnaisseurs
de racine ne connaissent que `/tmp/…` (cpp#266) et `${TMPDIR:-/tmp}/…` (cpp#201),
pas la racine **worktree-relative `.pilot-scratch/`** ni **`$PWD/.pilot-scratch/`**
que les consignes du dépôt prescrivent. Même famille que cpp#265/#268 : une
variable dérivée d'un scratch sanctionné, racine simplement non enregistrée.

## Le fix (AC1 — étendre le MÊME résolveur, létalité seule)

`tier1.py` (reconnaisseurs NOUVEAUX, consultés uniquement via les deux prédicats
`for_lethality`) :

- `_is_pilot_scratch_rel(value)` : True SSI `value` est `.pilot-scratch` ou
  `.pilot-scratch/<chemin>` worktree-relatif, sans aucun `..`. LEXICAL. Une valeur
  avec `$` n'est PAS une racine littérale (elle repart dans la résolution
  transitive de variable).
- `_value_roots_at_scratch(value, command, depth, cwd=None)` — DEUX extensions,
  dans le MÊME résolveur :
  1. le cas de base ajoute `.pilot-scratch` à côté de `/tmp`/`${TMPDIR:-/tmp}` ;
  2. un préfixe `$PWD/` / `${PWD}/` est reconnu comme la racine du worktree (cwd
     fixé par bash, pas d'assignation requise), et la QUEUE après lui doit
     elle-même rooter sur `.pilot-scratch` — donc `$PWD/.pilot-scratch/x` ET
     `$PWD/$D/x` (D rootant sur `.pilot-scratch`) sont reconnus, tandis que
     `$PWD/foo` (pas sous `.pilot-scratch`) et `$PWD/../x` (queue `..`) ne le sont
     pas. Si la commande RÉAFFECTE `PWD`, le raccourci est sauté et `PWD` est
     résolu comme une variable ordinaire (fail-closed).
  3. **Containment fs-aware (cpp#213/#38)** : contrairement aux racines SYSTÈME
     `/tmp`/mktemp (lexicales par doctrine cpp#143), `.pilot-scratch` est
     worktree-relative ; quand un `cwd` est fourni (le chemin puits
     `for_lethality`), sa reconnaissance passe par `is_within_pilot_scratch` — la
     MÊME notion `<cwd>/.pilot-scratch`, symlink-aware et fail-closed, que le puits
     `rm` de cpp#213 utilise. `.pilot-scratch` symlink sortant → terminal ; cwd
     irrésoluble → terminal. `cwd=None` (appels unitaires directs) reste lexical.
- `_is_transitive_ce_scratch_redirect_target(command, dest, cwd=None)` : le JUMEAU
  redirect de `_is_transitive_ce_scratch_mkdir_target`. Réutilise
  `_MKTEMP_SCRATCH_TARGET_RE` (`$VAR`/`${VAR}` + queue optionnelle, guillemet
  optionnel) pour gérer une cible PORTANT un suffixe (`> "$D/a.txt"`) que le
  prédicat mkdir (bare-`$VAR` seul) ne gère pas, puis le MÊME
  `_value_roots_at_scratch` + la MÊME `_last_real_assignment_value` (règle lecture
  seule inversée). Couvre uniformément `/tmp`/mktemp/`.pilot-scratch`.

`permissions.py` (câblage, `for_lethality`-gated) :

- puits `bash-mkdir` : la clause transitive cpp#265 existante passe désormais
  `cwd` (`_is_transitive_ce_scratch_mkdir_target(command, dest, cwd)`).
- puits `bash-redirect`/`bash-git-show-redirect` : la clause mktemp de cpp#201
  gagne un `or _is_transitive_ce_scratch_redirect_target(command, dest, cwd)`.

La règle « lecture seule » de cpp#270 (`_var_has_nonread_occurrence` via
`_live_scratch_source`) et la dérivation transitive s'appliquent INCHANGÉES.

## AC

- [x] **AC1 — le résolveur de cpp#270 reconnaît la racine `.pilot-scratch/`.**
  `_value_roots_at_scratch` accepte `.pilot-scratch/<sans ..>` (worktree-relatif)
  et `"$PWD/.pilot-scratch/<…>"` / `${PWD}/.pilot-scratch/<…>`. Les puits `mkdir`,
  `chmod` (déjà survivable — non-puits de veto) et la redirection `>` vers ces
  variables deviennent survivables.
- [x] **AC2 — le verbatim c722b251 devient survivable.** Les deux `mkdir` via
  variable et le `printf > "$R/main.rs"` deviennent non-létaux ; `git init` n'est
  pas un verbe de veto. Probe `_denial_is_terminal` = False sur le verbatim joint.
- [x] **AC3 — négatifs restent TERMINAUX (chacun via `_denial_is_terminal`).**
  `D=.pilot-scratch/../../etc` (traversée), `R="$PWD/../x"` (hors worktree),
  `D=.pilot-scratch/x; D=/etc` (réassignation — règle lecture seule cpp#270), et
  TOUTES les formes de réassignation des gates cpp#270 (`export D=/etc`,
  `{ D=/etc; }`, `: ${D:=/etc}`, `let`, `read`, `for`, `source`/`.`), une queue
  `..` sur le redirect, un symlink sortant sous `.pilot-scratch`, et les racines
  `$HOME`/`/etc`/`$(curl)`. Plus : `$PWD/foo` (pas sous `.pilot-scratch`) et
  `PWD=/etc; "$PWD/.pilot-scratch/x"` (PWD réaffecté, fail-closed).
- [x] **AC4 — gate MPC.** Admission byte-identique : 0 diff
  (`is_tier1_auto_approve` / `is_tier3_dangerous` /
  `_destination_veto_reason(for_lethality=False)` /
  `_redirect_destination_veto_reason`) sur un échantillon large (28 commandes),
  `main` `406983c` vs branche. Toute la machinerie ajoutée n'est atteinte que sous
  `for_lethality` (par construction).

## Invariants

- LÉTALITÉ SEULE. tier1 gate (`is_tier1_auto_approve`) + egress INTOUCHÉS ; carve
  `for_lethality`-only. < 50 ms (linéaire ; mesuré ~0,6 ms sur le verbatim,
  chaîne transitive de 50 niveaux bornée `_TRANSITIVE_SCRATCH_MAX_DEPTH`). MÊME
  résolveur — aucun fork d'un résolveur parallèle.

## Fire-Disposition

- **Feu** : deny TERMINAL d'un `mkdir -p "$D"` / `printf > "$D/x"` où `D` dérive
  de la racine `.pilot-scratch/` PRESCRITE par le dépôt (pilote c722b251 mika#2623
  mort tour 27) — classe de dérivation transitive de cpp#265/#268, racine
  worktree-relative non enregistrée.
- **Éteint** : une cible `mkdir`/`chmod`/redirect portée par une variable que la
  MÊME commande garde comme scratch `.pilot-scratch` vivant (règle lecture seule,
  containment fs-aware) n'est plus un danger prouvé sur le chemin létalité ; le
  refus reste, seule la mort disparaît.
- **Vérif de sortie** : helpers `tier1.py` + clauses `permissions.py` ; tests
  GREEN (`TestCeScratchSanctionUnit` étendu : `_is_pilot_scratch_rel`, mkdir/redirect
  transitifs `.pilot-scratch`, négatifs lexicaux, règle lecture seule par forme,
  PWD réaffecté fail-closed, containment symlink-aware ;
  `test_cpp272_pilot_scratch_var_is_survivable` (7 positifs dont verbatim) +
  `test_cpp272_negatives_stay_terminal` (11 négatifs) +
  `test_cpp272_outbound_symlink_and_unresolvable_cwd_stay_terminal` +
  `test_cpp272_admission_is_byte_identical_only_lethality_flips`) ; 29 tests
  cpp272/pilot_scratch, 0 skip ; probe `_denial_is_terminal` AVANT/APRÈS sur
  verbatim + chaque positif + chaque négatif (deux puits) ; diff admission 0 contre
  `main` `406983c` (28 commandes) ; ruff/mypy clean ; suite (1692) verte ;
  verify-pipeline GREEN. Édition appliquée directement — pas de blocage [Security
  Weaken], pas de fenêtre manuelle requise.
- **Résidu (nommé)** : (1) le puits mkdir transitif reste bare-`$VAR` (cpp#265) ;
  un `mkdir -p "$R/sous-dossier"` (suffixe au puits mkdir) n'est pas couvert
  (hors scope, fail-closed terminal). (2) Un composant symlink-ENFANT sous
  `.pilot-scratch` traversé via variable n'est pas résolu (la valeur lexicale ne le
  porte pas) — même limite lexicale que les carves `/tmp`/mktemp ; seule la racine
  `.pilot-scratch` est tenue au containment fs-aware. (3) Étendre le puits redirect
  au résolveur partagé rend aussi survivables les redirects `/tmp`/mktemp via
  variable-avec-suffixe (cohérent avec cpp#265, létalité-seule, admission intacte) —
  un alignement, pas un élargissement de l'admission.

## Références

- Solution : `docs/solutions/security-issues/the-prescribed-pilot-scratch-root-is-a-derived-scratch-root-not-a-terminal-denial.md`.
- Siblings : cpp#213 (carve rm `.pilot-scratch` — source de la notion
  `is_within_pilot_scratch`), cpp#201 (carve redirect SOURCE mktemp), cpp#265/#266
  (dérivation scratch transitive, LAST-WINS), cpp#268/#270 (puits rm/mkdir sur
  variable dérivée + règle lecture seule inversée). Doctrine cpp#205 (défaut
  survivable), cpp#143 (jamais résoudre pour GRANT — sauf le containment
  worktree-relative, hérité de cpp#38/#213), cpp#250 (ReDoS).
- Code : `src/claude_pilot/tier1.py`, `src/claude_pilot/permissions.py`. PR : cpp#272.
