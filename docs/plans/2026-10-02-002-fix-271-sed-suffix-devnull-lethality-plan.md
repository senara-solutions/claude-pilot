---
ticket: cpp#271 (mika#2624)
kind: fix
class: default-survivable lethality (admission inchangée)
status: ready-for-qa
---

# `sed -i<SUFFIXE> … /dev/null` n'est pas un refus terminal — l'intersection cpp#203 x cpp#255 — Plan

Reprend `docs/solutions/security-issues/a-sed-i-suffix-to-dev-null-is-the-cpp203-cpp255-intersection.md`. Intersection directe de cpp#203 (`/dev/null`, `-i` nu) et cpp#253/#255 (suffixe, cible worktree). Famille cpp#205. Priorité basse (n=1).

## Goal Capsule

cpp#203 a rendu SURVIVABLE un `sed -i` refusé quand la cible est le puits inerte `/dev/null` — mais SEULEMENT pour la forme `-i` nue (`_SED_I_DEVNULL_RE` exige un blanc immédiatement après le drapeau). cpp#253/#255 a rendu survivables les formes à suffixe (`-i.bak`, `-i~`) — mais SEULEMENT pour des cibles confinées au worktree (`/dev/null`, chemin absolu, est rejeté par `is_within_project`). Aucun des deux ne couvre l'autre axe : `sed -i.bak … /dev/null` tombe entre les deux et reste TERMINAL. **Mort** : pilote `94770602` (implement de mika#2624), 2026-10-01T19:20:31Z, 66 tours, 19,52 USD, sur un script de 3 lignes dont le tueur est `sed -i.bak 's|…|XX|' /dev/null 2>/dev/null; true` — le pilote sondait une regex, n'écrivait rien. **But** : reconnaître `/dev/null` comme cible inerte de `sed -i` quelle que soit la forme du suffixe, PLUS la garde que le suffixe ne devienne pas un vecteur d'écriture. **Létalité seule**, admission octet-identique, gate tier1 jamais touché, axe egress jamais touché, cpp#203 et cpp#255 inchangés.

## Le facteur isolé (rejoué au source HEAD 406983c)

| commande | `_denial_is_terminal` |
|---|---|
| `sed -i 's|a|XX|' /dev/null` (cpp#203) | False |
| `sed -i.bak 's/a/b/' crates/x.rs` (cpp#255) | False |
| **`sed -i.bak 's|a|XX|' /dev/null`** | **True** ← l'intersection non couverte |

## Le fix (LÉTALITÉ SEULE — `tier1.py`, dans `sed_i_confined_to_worktree`)

- **`sed_i_confined_to_worktree`** : avant le confinement par-cible existant, un segment dont la SEULE cible-fichier réelle est `/dev/null` (tokens de redirection shell filtrés) est carvé pour tout suffixe de sauvegarde bénin. `/dev/null` est un device noyau — jamais résolu sur disque, aucun `cwd` requis. Mirroir exact de la contrainte cpp#203 (`/dev/null` doit être le seul opérande), donc une liste mixte (`… /dev/null real.rs`) est octet-identique à pré-cpp#271 : `/dev/null` n'y est PAS inerte, le confinement par-cible le rejette (absolu), terminal.
- **`_sed_i_suffix_is_benign_backup`** (nouveau) : un suffixe est bénin ssi il ne porte ni `/` ni `..`. GNU sed écrit la sauvegarde en APPONDANT le suffixe au nom (`/dev/null` + suffixe) ; un suffixe porteur de chemin (`-i../../etc/x`, `--in-place=/tmp/../etc/`) est un vecteur d'écriture → rejeté → terminal (fail-closed). Le joker `*` est déjà exclu en amont par `_sed_inplace_suffix`.
- **`_SED_REDIRECT_TOKEN_RE`** (nouveau, `^\d*[<>]`) : shlex ne modélise pas les redirections, donc `2>/dev/null`/`>out` sont mal tokenisés en opérandes positionnels. Ils sont filtrés AVANT le test de cible unique. Une vraie cible de redirection hors-worktree est ré-armée par le veto redirect/destination de `_denial_is_terminal` sur la commande COMPLÈTE après ce carve (fail-safe).
- **`_sed_i_target_operands`, `_sed_i_edit_and_backup_confined`, `_sed_i_target_confined`, `_SED_I_SUBST_UNIT_RE`** (cpp#253/#255/#245) : **INCHANGÉS**. Le confinement par-fichier et le re-check du reste façon `rm_confined_to_pilot_scratch` : INCHANGÉS. `_SED_I_DEVNULL_RE` (cpp#203) et `is_tier3_dangerous_for_lethality` : INCHANGÉS.

## Fail-safe

Fail-closed dans toutes les directions : suffixe porteur de `/` ou `..` → terminal ; cible non-`/dev/null` hors-worktree (absolu, `$HOME`, `~`, `..`, symlink sortant) → terminal (régie par cpp#255) ; `/dev/null` + 2ᵉ cible (même in-worktree) → pas la forme cible-unique → terminal (contrainte cpp#203 préservée) ; script non-substitution / `w`/`e`/`r`/`y` → `None` → terminal ; vraie redirection hors-worktree alongside `/dev/null` → ré-armée par le veto destination sur la commande complète ; verbe dangereux chaîné → terminal. Le carve ne bascule QUE terminal→survivable. Reconnaissance LINÉAIRE (leçon ReDoS cpp#250) : filtre redirect (`^\d*[<>]` ancré) + deux tests `in` pour le suffixe — O(len), aucun backtracking. Adverse 8 KB : ~4 ms (<< 50 ms).

## Hors portée

- Admission : intouchée (`is_tier3_dangerous`, `is_tier1_auto_approve`, YAML) — `sed -i.bak … /dev/null` reste DENY, le pilote bascule sur l'outil Edit.
- Le gate tier1 `is_tier1_auto_approve` : jamais touché. L'axe egress : jamais touché.
- `--in-place` long (`--in-place=.bak`, `--in-place=/tmp/../etc/`) : hors carve. `_sed_inplace_suffix` ne le reconnaît pas ET `TIER3_PATTERNS` ne le capte pas, donc il est DÉJÀ survivable au source HEAD (régie entièrement par le chemin tier3 inchangé), indépendamment de tout carve `/dev/null`. Le rendre terminal exigerait une CHANGE d'admission (ajouter `--in-place` à TIER3_PATTERNS) — hors portée létalité-seule de cpp#271. Voir « Résidu ».

## Acceptance criteria

- [x] **AC1 — formes à suffixe sur `/dev/null` survivables.** `sed -i.bak … /dev/null`, `sed -i~ … /dev/null`, `sed -i.orig`, `-ibak`, `-ni.bak` → deny NON-terminal. `--in-place=.bak … /dev/null` déjà survivable (inchangé). Régressions cpp#203 (`-i` nu) et cpp#255 (suffixe + worktree) inchangées.
- [x] **AC2 — verbatim `94770602` survivable.** Le script 3 lignes (`cd` + `sed -i.bak … /dev/null 2>/dev/null; true` + `grep`) → `_denial_is_terminal` **True sur HEAD → False après**. Le `2>/dev/null` traité (token redirect filtré).
- [x] **AC3 — négatifs restent TERMINAUX (chacun testé).** `-i.bak … /etc/passwd` ; `-i.bak … ../../x` ; suffixe porteur de chemin `-i../../etc/x … /dev/null`, `-i/tmp/x … /dev/null`, `-i.. … /dev/null` ; `/dev/null` + 2ᵉ cible (hors ou in-worktree) ; vraie redirection `… /dev/null >/etc/passwd` (ré-armée) ; script `y///`/`w` ; `&& rm -rf /etc`.
- [x] **AC4 — admission octet-identique (échantillon large).** `is_tier3_dangerous`/`is_tier1_auto_approve`/YAML inchangés (diff before/after vide sur 22 échantillons) ; un seul écart de létalité par forme visée (sole-`/dev/null` x suffixe bénin) ; end-to-end = `PermissionResultDeny` non-terminal, jamais allow.
- [x] **AC5 — non-réouverture + temps borné.** cpp#130/#154/#203/#205/#213/#229/#236/#237/#241/#245/#250/#253/#255 inchangés ; suite complète 1672 verte ; reconnaissance linéaire < 50 ms.

## Fire-Disposition

- **Feu** : classe de morts létales sur `sed -i<SUFFIXE>` dont la seule cible est le puits inerte `/dev/null` (mika#2624, pilote `94770602`, 2026-10-01T19:20:31Z). **Traité** : `sed_i_confined_to_worktree` reconnaît `/dev/null` comme cible-unique inerte pour tout suffixe bénin (ni `/` ni `..`), tokens redirect filtrés ; suffixes porteurs de chemin et vraies redirections hors-worktree restent terminaux.
- **Vérif de sortie** : probe avant/après (intersection + verbatim RED sur HEAD → GREEN ; tous les négatifs AC3) ; diff admission before/after vide ; `pytest`/`ruff`/`mypy`/`verify-pipeline` verts. QA MPC sur le code (gate AC4 : admission octet-identique, un seul écart de létalité).
- **Résidu** : `--in-place` long reste survivable au source indépendamment de ce fix (non capté par TIER3_PATTERNS). Le négatif `--in-place=/tmp/../etc/ … /dev/null` du ticket est DÉJÀ survivable sur HEAD, pas via un carve `/dev/null` mais parce que la forme longue échappe entièrement au denylist tier3 — le rendre terminal est un CHANGE d'admission hors portée cpp#271 (létalité-seule). Signalé à MPC pour décision (ticket séparé éventuel). Le câblage décision-flip (`sed_i_confined_to_worktree` dans `_denial_is_terminal`) préexiste — aucune fenêtre manuelle Vincent requise.

## Preuve / vérif (verbatim)

Probe (`_denial_is_terminal`) : intersection `sed -i.bak 's|a|XX|' /dev/null` **True sur HEAD 406983c → False après** ; verbatim `94770602` idem ; `-i~`/`-i.orig`/`-ibak`/`-ni.bak` survivables ; `--in-place=.bak` survivable (inchangé) ; cpp#203 nu + cpp#255 worktree inchangés. Négatifs : `/etc/passwd`, `../../x`, `-i../../etc/x`, `-i/tmp/x`, `-i..`, `/dev/null real.rs`, `/dev/null /etc/passwd`, `… /dev/null >/etc/passwd` (ré-armé), `y///`, `w /etc/x`, `&& rm -rf /etc` restent TERMINAUX. Admission tier3/tier1 : diff before/after vide (22 échantillons). Temps : `_denial_is_terminal`(intersection) ~0.077 ms ; adverse 8 KB ~4 ms. `uv run pytest` → **1672 passed** ; `ruff` clean ; `mypy src` clean ; `verify-pipeline.sh main` passed (avec ce plan + la solution).

## Prédicat de déclenchement

Survivable ssi, pour un segment, `_sed_i_target_operands` renvoie `(files, suffix)` (leading word `sed`, drapeau in-place reconnu par `_sed_inplace_suffix`, script = substitutions pures) ET, après filtrage des tokens redirect (`^\d*[<>]`), les cibles réelles valent exactement `["/dev/null"]` ET le suffixe est bénin (ni `/` ni `..`) ; sinon le confinement par-cible cpp#255 existant s'applique inchangé. Au moins un segment carvé ET le reste n'est plus prouvé-dangereux ET aucun veto redirect/destination sur la commande complète.

## Références

- Solution : `docs/solutions/security-issues/a-sed-i-suffix-to-dev-null-is-the-cpp203-cpp255-intersection.md`.
- Preuve : pilote `94770602` (mika#2624), 2026-10-01T19:20:31Z. Intersection : cpp#203 (`/dev/null`, `-i` nu) x cpp#253/#255 (suffixe, cible worktree). Famille : cpp#205. Leçon ReDoS : cpp#250.
