---
ticket: cpp#279 / mika#2631
kind: fix
class: lethality-only (sur-refus syntaxique — une racine de scratch DÉRIVÉE portée par une variable est suivie par le puits redirect AVEC suffixe, mais par le puits `mkdir` seulement NUE et par `cp` pas du tout ; même racine, puits incohérents entre eux)
status: landed (résolveur partagé `_is_transitive_ce_scratch_sink_target` + puits `mkdir`/`cp`-dest câblés sur le chemin `for_lethality`, `chmod`/`touch` déjà survivables ; tests + docs) — COMPLÉTION de la carve cpp#272/#273, PAS une extension. GATE-KO cpp#280 corrigé : pour `mv`, la source est une ÉCRITURE (supprimée), donc la carve de destination exige en plus que toutes les sources soient contenues (worktree ou racine de scratch) — sinon TERMINAL ; `cp` inchangé
---

# Chaque puits d'écriture suit une racine de scratch dérivée AVEC un sous-chemin — complétion de la carve cpp#272/#273 — Plan (cpp#279)

## Goal Capsule

Pilote **4617da8f** (implement de mika#2631) mort TERMINAL au tour 36 (rescue
PR#2637) en recopiant une arborescence **sous `.pilot-scratch/`**, là où les
consignes du dépôt le demandent, à la ligne 2 :

```
set -u
FAL=.pilot-scratch/falsify
mkdir -p "$FAL/skills/bundled/_shared/tests"   # ← mort ici (premier préfixe terminal)
cp skills/bundled/_shared/dispatch-lib.sh "$FAL/skills/bundled/_shared/"
…
```

But : **létalité seule, admission byte-identique** — la commande reste DENY, seul
`_denial_is_terminal` bascule True→False. C'est le résidu que cpp#272 avait
explicitement nommé (« le puits mkdir transitif reste bare-`$VAR` ; un
`mkdir -p "$R/sous-dossier"` (suffixe au puits mkdir) n'est pas couvert »).

## Cause racine (vérifiée à la source, servi `1e8e28b`, worktree avec `.pilot-scratch/`)

Même racine dérivée, puits INCOHÉRENTS :

| commande | terminal sur `1e8e28b` |
|---|---|
| `D=.pilot-scratch/git-probe; mkdir -p "$D"` | False (nue) |
| `D=.pilot-scratch/x; printf x > "$D/a.txt"` | False (redirect suit le suffixe) |
| `D=.pilot-scratch/f; mkdir -p "$D/a"` | **True** (mkdir ne suit QUE la forme nue) |
| `D=.pilot-scratch/f; mkdir -p "$D/a/b"` | **True** |
| `FAL=.pilot-scratch/f; cp x.sh "$FAL/"` | **True** (cp pas carvé du tout pour scratch dérivé) |
| `cp x.sh .pilot-scratch/f/` (littéral) | False |

Le puits redirect passe par `_is_transitive_ce_scratch_redirect_target`, qui
utilise `_MKTEMP_SCRATCH_TARGET_RE` (`$VAR`/`${VAR}` + queue optionnelle) : il
suit la variable AVEC suffixe. Le puits mkdir passe par
`_is_transitive_ce_scratch_mkdir_target`, qui utilise `_CE_SCRATCH_VARREF_RE`
(bare-`$VAR` only) : un `$D/a` ne matche pas → véto cpp#218 (`$`-rooted mkdir) →
TERMINAL. `cp`/`mv` n'avait AUCUNE carve scratch dérivé (seulement la carve
mktemp-var cpp#209) → véto cpp#211 (`$`-rooted cp/mv) → TERMINAL dès la forme nue.

## Le fix (AC1 — MÊME résolveur, un seul, aucun fork)

`tier1.py` — un prédicat PARTAGÉ suivi par TOUS les puits :

- `_is_transitive_ce_scratch_sink_target(command, dest, cwd)` : le corps de
  l'ancien `_is_transitive_ce_scratch_redirect_target` — `_MKTEMP_SCRATCH_TARGET_RE`
  (strip de la queue `/<sous-chemin>`), rejet `..` dans la queue, résolution de la
  variable par `_last_real_assignment_value` → `_live_scratch_source` (règle
  lecture-seule inversée), racine testée par `_value_roots_at_scratch`.
- `_is_transitive_ce_scratch_redirect_target` → délègue (comportement inchangé).
- `_is_transitive_ce_scratch_mkdir_target` → UNION : la branche bare
  `_CE_SCRATCH_VARREF_RE` RETENUE (pour l'artefact subshell `$D)` que le charset
  suffixe n'admet pas) OU le prédicat partagé (pour `$D/a`).

`permissions.py` (clauses `for_lethality` uniquement) :

- puits `bash-mkdir` : OR `_is_transitive_ce_scratch_mkdir_target` (maintenant
  suffixe-aware) OU `_is_mktemp_scratch_redirect_target` (mktemp-var, le MÊME OR
  que cp/redirect ont déjà — rend `$(mktemp -d)` cohérent à travers les puits).
- puits `bash-cp-mv` : NOUVELLE clause `_is_transitive_ce_scratch_sink_target` sur
  la DESTINATION (dernier opérande / `-t DIR`), AVANT le véto `$`/`~` cpp#211. La
  carve mktemp-var cpp#209 reste. `cp`/`mv` partagent le write-kind `bash-cp-mv`.
- **cpp#280 (GATE-KO)** : pour `cp` la destination EST l'axe de containment (la
  source est une lecture) ; pour `mv` NON — `mv` SUPPRIME sa source, donc la source
  est elle-même une ÉCRITURE. `mv /etc/passwd "$D/"` DÉPLACE un fichier système et
  doit rester TERMINAL. Les DEUX clauses carve cp/mv (mktemp cpp#209 + dérivée
  cpp#279) sont gardées par `_mv_has_escaping_source(command, seg, cwd)` : pour `mv`
  SEULEMENT, elles ne s'activent que si CHAQUE source est contenue — enracinée à un
  scratch reconnu (MÊME résolveurs que la destination) OU opérande propre relatif au
  worktree (ni absolu, ni `$`/`~`, sans composant `..`). Test LEXICAL (pas
  `is_within_project`, qui échoue fermé pour tout chemin dans un worktree démoli — la
  fenêtre d'incident cpp#209 — et terminaliserait à tort `mv crates/x.rs "$D/"`). `cp`
  est byte-identique à cpp#279 (le garde est un no-op hors `mv`). Verbe distingué par
  le mot de commande en tête (`_LEADING_CMD_RE`). Nouvel extracteur de sources
  `_extract_cp_mv_sources` (gère `-t DIR`/`--target-directory=`).
- puits `chmod`/`touch` : AUCUN changement — ils ne sont PAS des write-kinds
  (`_segment_write_kind` → None), donc DÉJÀ survivables pour toute destination. Les
  câbler comme nouveaux puits les rendrait terminaux hors-scratch — un
  élargissement de périmètre, exclu par la BORNE (voir plus bas).

## AC

- [x] **AC1 — chaque puits d'écriture accepte `"$VAR/<sous-chemin>"`.** `mkdir -p`,
  `cp` (destination), `>`-redirect suivent une racine de scratch dérivée
  (`.pilot-scratch/…`, `$PWD/.pilot-scratch/…`, `$(mktemp -d)`, `/tmp/…`) avec une
  queue sans `..`, via le MÊME résolveur. `chmod`/`touch` déjà survivables.
- [x] **AC2 — le verbatim 4617da8f devient survivable.** Comme UNE commande Bash
  (le `FAL=` est en scope), toutes les lignes (`mkdir`/`cp`/`chmod`/`touch`/`printf >`)
  enracinées à `$FAL` deviennent non-létales ; `_denial_is_terminal` = False. Aucune
  ligne ne reste terminale (`chmod -R` serait terminal par le verbe cpp#205, mais le
  verbatim utilise `chmod` nu).
- [x] **AC3 — négatifs restent TERMINAUX.** `"$D/../../etc"` (traversée dans le
  sous-chemin), `cp x "$D/a" /etc/` (multi-opérande, destination FINALE = /etc hors
  scratch), toutes les réassignations-out (`export D=/etc`, `: ${D:=/etc}`,
  `{ D=/etc; }`, `source`/`.`, `let`, `read`, `for`, indirection), racines
  `$HOME`/`/etc`/`$(curl)`, un `.pilot-scratch` symlink sortant, un créateur de lien
  `ln`/`link`/`cp -s` (cpp#273, fail-closed conservé). DÉCISION documentée :
  `cp /etc/passwd "$D/"` — la source est hors-arbre mais la DESTINATION est
  in-scratch ; la destination est l'axe de containment ⇒ SURVIVABLE (copier DANS le
  scratch ; la lecture de /etc/passwd est une lecture, non une brèche d'écriture).
  Aucune écriture réelle ne s'échappe. **cpp#280 — la distinction cp/mv** :
  `mv /etc/passwd "$D/"` est TERMINAL (contrairement à `cp`), car `mv` supprime sa
  source → c'est une écriture. NÉGATIFS mv ajoutés (VU ROUGE avant, VERT après) :
  `mv /etc/passwd "$D/"`, `mv /etc/x "$D/y"`, `mv a /etc/passwd "$D/"` (une source
  hors-arbre), `mv ~/.ssh "$D/"`, `mv ../x "$D/"`, `mv "$HOME/x" "$D/"`, et les mêmes
  à travers les racines mktemp/`/tmp`/`$PWD`. POSITIFS mv (survivables) :
  `mv a "$D/x"`, `mv "$D/a" "$D/b"`, `mv src/x.rs "$D/"`, `mv .pilot-scratch/a "$D/b"`.
- [x] **AC4 — gate MPC : corpus par puits × forme.** Chaque puits
  {`mkdir -p`, `cp` dest, `mv` dest (source contenue, cpp#280), `chmod`, `touch`, `>`}
  croisé avec chaque forme {`"$D"`, `"$D/x"`, `"$D/x/y"`, `"${D}/x"`} pour 4 racines
  dérivées → TOUS survivables (le puits `mv` l'est UNIQUEMENT quand la source est
  contenue ; une source hors-enveloppe reste terminale, voir AC3). Admission
  byte-identique : 0 diff
  (`is_tier1_auto_approve` / `is_tier3_dangerous` /
  `_destination_veto_reason(for_lethality=False)`), 336 commandes, servi `1e8e28b`
  vs branche.

## Invariants

- LÉTALITÉ SEULE. tier1 gate (`is_tier1_auto_approve`) + egress INTOUCHÉS ; carve
  `for_lethality`-only. < 50 ms (linéaire ; mesuré 0,53 ms pire cas chaîne
  transitive profonde). MÊME résolveur partagé — aucun fork.

## Fire-Disposition

- **Feu** : deny TERMINAL d'un `mkdir -p "$D/a"` / `cp x.sh "$FAL/"` où la racine
  dérivée est suivie d'un sous-chemin (pilote 4617da8f mika#2631 mort tour 36) —
  résidu nommé par cpp#272, puits incohérents entre eux sur la même racine.
- **Éteint** : chaque puits d'écriture suit désormais `"$VAR/<sous-chemin>"`
  (sans `..`) vers une racine de scratch dérivée via le MÊME résolveur ; le refus
  reste, seule la mort disparaît.
- **Vérif de sortie** : helper partagé `tier1.py` + clauses `permissions.py` ; tests
  GREEN (`test_cpp279_derived_scratch_subpath_all_sinks_survivable` corpus puits×forme,
  `test_cpp279_negatives_stay_terminal`, `test_cpp279_ac3_cp_out_of_tree_source_into_scratch_is_survivable`,
  `test_cpp279_verbatim_4617_is_survivable`,
  `test_cpp279_admission_is_byte_identical_only_lethality_flips` ;
  cpp#280 `test_cpp280_mv_escaping_source_stays_terminal` (négatifs mv, VU ROUGE→VERT),
  `test_cpp280_mv_contained_source_survivable` (positifs mv),
  `test_cpp280_cp_out_of_tree_source_unchanged_survivable` (cp inchangé),
  `test_cpp280_mv_admission_is_byte_identical`,
  `test_cpp280_extract_cp_mv_sources_forms`,
  `test_cpp280_mv_has_escaping_source_is_verb_scoped` ; unités tier1
  `test_cpp279_mkdir_follows_var_with_subpath`,
  `test_cpp279_mkdir_bare_subshell_artifact_still_recognized`,
  `test_cpp279_sink_predicate_handles_derived_roots`) ; probe `_denial_is_terminal`
  AVANT/APRÈS sur le corpus (tous survivables), verbatim, chaque négatif ; diff
  admission 0 contre servi `1e8e28b` (336 commandes) ; ruff/mypy clean ; suite verte ;
  verify-pipeline GREEN. Édition appliquée directement — pas de blocage [Security
  Weaken].
- **Résidu (nommé)** : (1) l'artefact subshell `$D/a)` (queue `)` + sous-chemin)
  n'est pas couvert par le charset `_MKTEMP_SCRATCH_TARGET_RE` (même limite que le
  puits redirect depuis cpp#272 ; la forme bare `$D)` reste couverte). (2)
  `chmod -R`/`chown -R` restent terminaux par le verbe cpp#205, indépendamment de la
  destination (hors scope — axe verbe, pas axe destination).

## BORNE respectée

Complétion de la carve `.pilot-scratch/mktemp//tmp` DÉJÀ ratifiée (cpp#272/#273) :
même racine, sous-chemin/puits manquant. AUCUN élargissement de périmètre au-delà
d'une racine de scratch dérivée. `chmod`/`touch` NON câblés comme nouveaux puits
(ce serait admettre un puits que la carve n'a jamais couvert d'une façon qui n'est
pas « même racine, sous-chemin/puits manquant » — question d'ADMISSION, pas de
létalité). Rien au-delà de cpp#279.

## Références

- Solution : `docs/solutions/security-issues/a-derived-scratch-root-is-followed-with-a-sub-path-across-every-write-sink.md`.
- Siblings : cpp#272 (racine `.pilot-scratch/` au résolveur dérivé — ce plan ferme
  son résidu nommé), cpp#273 (garde `ln`/`link`/`cp -s` fail-closed — conservée),
  cpp#209 (carve cp mktemp-var), cpp#265/#266 (dérivation transitive LAST-WINS),
  cpp#270/#268 (règle lecture-seule inversée). Doctrine cpp#38/#213 (containment
  worktree-relatif), cpp#143 (ne jamais résoudre pour ACCORDER), cpp#205 (survivable
  par défaut), cpp#250 (ReDoS).
- Ticket : cpp#279 / mika#2631 (pilote 4617da8f, mort tour 36, rescue PR#2637).
