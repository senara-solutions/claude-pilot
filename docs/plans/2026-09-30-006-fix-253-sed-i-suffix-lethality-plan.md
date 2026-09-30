---
ticket: cpp#253 (mika#2601)
kind: fix
class: default-survivable lethality (admission inchangée)
status: ready-for-qa
---

# `sed -i` avec suffixe de sauvegarde confiné comme `-i` nu — Plan

Reprend `docs/solutions/security-issues/a-sed-i-backup-suffix-is-confined-like-bare-i.md`. Complément direct de cpp#229/#245 `sed_i_confined_to_worktree`. Famille cpp#205. GO gate MPC reçu (ratifié).

## Goal Capsule

cpp#229/#245 (mika#2565) ont rendu SURVIVABLE un `sed -i` refusé quand l'édition en place vise des fichiers relatifs confinés au worktree, script = substitutions pures (mono ou multi-`;`/multi-`-e`). Mais le reconnaisseur de drapeau `-[A-Za-z]*i[A-Za-z]*` ne matchait que des clusters **tout-lettres** : la forme GNU à **suffixe de sauvegarde** (`-i.bak`, `-i.orig`) — le `.` n'est pas une lettre — n'était pas reconnue comme in-place, `_sed_i_target_operands` renvoyait `None`, et le refus retombait sur le motif létal. **Mort** : pilote #2601 (`f2edcf8f`), 2026-09-30T14:28:33.718Z, sur `sed -i.bak 's/^const PRODUCTION_ATTEMPTS: u32 = 3;$/…= 1;/' crates/mika-agent/src/task_engine/mod.rs && grep …`. Or `-i.bak` écrit `mod.rs` (cible relative confinée) et sa sauvegarde `mod.rs.bak` **à côté, dans le worktree** — aucune écriture hors-worktree. **But** : étendre la reconnaissance #229/#245 aux formes `-iSUFFIX`/`-i.SUFFIX`, avec les mêmes règles (#245 substitutions pures), PLUS la sauvegarde `<cible><SUFFIX>` elle aussi confinée. **Létalité seule**, admission octet-identique, gate tier1 jamais touché, axe egress jamais touché.

## Le fix (LÉTALITÉ SEULE — `tier1.py`)

- **`_sed_inplace_suffix`** (remplace `_SED_INPLACE_FLAG_RE`) : scan **LINÉAIRE** (pas de regex à backtracking). Un token est in-place ssi il commence par `-`, contient un `i` ; le cluster avant le **premier** `i` doit être des lettres ASCII (autres drapeaux courts sans argument, `-n`/`-r`/…, charset pré-#253 préservé) ; le SUFFIXE = tout ce qui suit ce `i` (GNU : `-i` consomme le reste de l'argument). Renvoie le suffixe (`""` pour `-i` nu), ou `None` (fail-closed). Un `*` dans le suffixe (joker GNU : chaque `*` remplacé par le nom de fichier → peut projeter la sauvegarde n'importe où) → `None`.
- **`_sed_i_target_operands`** : renvoie désormais `(files, suffix)` au lieu de `files`. Le suffixe (celui du drapeau in-place vu) est propagé au confinement. Reste inchangé : séparation script/fichiers via shlex (#245), `-f`/`--file` → `None`, chaque script validé par `_sed_i_script_all_safe_subs`.
- **`_sed_i_edit_and_backup_confined`** (nouveau) : la cible ET — pour les formes à suffixe — la sauvegarde `cible + suffixe` (GNU : suffixe non-`*` = append) passent toutes deux `_sed_i_target_confined` (`is_within_project`, cwd/fs-aware, symlink-résolvant ; rejette absolu, `..`, `$`, `~`). Un suffixe qui sortirait la sauvegarde du worktree (`.bak/../../etc/x`) est rejeté par `is_within_project`. `-i` nu (suffixe vide) → seule la cible est vérifiée.
- **`_SED_I_SUBST_UNIT_RE` / `_sed_i_script_all_safe_subs`** (#245) : **INCHANGÉS**. Confinement par fichier et re-check du reste façon `rm_confined_to_pilot_scratch` : **INCHANGÉS**.

## Fail-safe

Fail-closed dans toutes les directions : suffixe `*` (joker) → non reconnu → terminal ; sauvegarde projetée hors worktree (`..`) → terminal ; cible hors worktree (absolu, `$HOME`, `~`, `..`, symlink sortant) → terminal ; script non-substitution / `w`/`W`/`s///w`/`e`/`r` → `None` → terminal ; liste mixte, verbe dangereux hors quotes, cwd irrésoluble → terminal. Le carve ne bascule QUE terminal→survivable. Reconnaissance LINÉAIRE (leçon ReDoS cpp#250) : le scan `find`/`isalpha` est O(n) ; la regex lazy initialement essayée était O(n²) sur entrée adverse — rejetée.

## Hors portée

- Admission : intouchée (`is_tier3_dangerous`, `is_tier1_auto_approve`, YAML) — `sed -i.bak` reste DENY, le pilote bascule sur l'outil Edit.
- Le gate tier1 `is_tier1_auto_approve` : jamais touché. L'axe egress : jamais touché.
- `--in-place` long : hors carve (pas capté par `TIER3_PATTERNS`, déjà survivable), inchangé.

## Acceptance criteria

- [x] **AC1 — formes à suffixe survivables.** Verbatim #2601 ; `sed -i.bak 's/a/b/' path/x` isolé ; `sed -i.orig …` ; multi-`-e` `-i.bak -e … -e …` (#245) ; `-ibak` (sans point) → deny NON-terminal. Régression `-i` nu (#229) survivable inchangée.
- [x] **AC2 — négatifs restent TERMINAUX.** `sed -i.bak` cible `/etc/hosts` (hors worktree) ; script avec `w /etc/x` / `s///w file` / `r /etc/passwd` ; `-i.bak … ; rm -rf /` ; cible `..` sortant ; suffixe projetant la sauvegarde dehors ; suffixe joker `*` ; `$HOME`/`~` ; liste mixte ; `&& git reset --hard` ; symlink sortant.
- [x] **AC3 — admission octet-identique.** `is_tier3_dangerous`/`is_tier1_auto_approve`/YAML inchangés ; end-to-end = `PermissionResultDeny` non-terminal, jamais allow.
- [x] **AC4 — non-réouverture.** cpp#154/#203/#205/#213/#229/#236/#237/#241/#245/#250 inchangés (particulièrement le confinement #229 et le multi-expression #245). Suite complète verte.
- [x] **AC5 — reconnaissance bornée < 50 ms.** Le reconnaisseur sur entrée pathologique (leçon ReDoS cpp#250) reste linéaire ; test `test_recognizer_is_bounded_time`.

## Fire-Disposition

- **Feu** : classe de morts létales sur `sed -i` à **suffixe de sauvegarde** confiné au worktree (mika#2601, `f2edcf8f`, 2026-09-30T14:28:33.718Z). **Traité** : reconnaissance #229/#245 étendue aux formes `-iSUFFIX`/`-i.SUFFIX` via un scan linéaire ; confinement étendu à la sauvegarde `<cible><SUFFIX>` ; suffixe joker et projections hors-worktree restent terminaux.
- **Vérif de sortie** : probe avant/après (verbatim RED sur HEAD → GREEN ; AC1 + tous les négatifs AC2) ; test de temps borné < 50 ms ; `pytest`/`ruff`/`mypy`/`verify-pipeline` verts. QA MPC sur le code (GO gate ratifié reçu). Redeploy + restart moteur ensuite (hors ce repo).
- **Résidu** : aucun. `--in-place` long reste hors carve (fail-closed intentionnel, déjà survivable côté admission). Le câblage décision-flip (`sed_i_confined_to_worktree` dans `_denial_is_terminal`) préexiste (#229/#245) — aucune fenêtre manuelle Vincent requise pour ce fix.

## Preuve / vérif (verbatim)

Probe (source `sed_i_confined_to_worktree` + `_denial_is_terminal`) : verbatim #2601 `_denial_is_terminal` **True sur HEAD 216033d** → **False après** ; 6 positifs AC1 survivables ; 11 négatifs AC2 restent terminaux ; admission tier3=True/tier1=False inchangée. Temps borné : reconnaisseur linéaire (n=16000 : ~0.001 ms ; la regex lazy rejetée était ~1382 ms). `uv run pytest` → **1567 passed** ; `uv run ruff check .` → clean ; `uv run mypy src` → clean ; `./scripts/verify-pipeline.sh main` → passed.

## Prédicat de déclenchement

Survivable ssi, pour CHAQUE segment `_split_compound_command`, `_sed_i_target_operands` renvoie `(files, suffix)` non vide (leading word = `sed` exact, drapeau in-place reconnu par `_sed_inplace_suffix` — cluster de lettres + premier `i` + suffixe sans `*`, pas de `-f`/`--file`, chaque script = substitutions pures via `_sed_i_script_all_safe_subs`) ET chaque fichier passe `_sed_i_edit_and_backup_confined` (cible relative non `$`/`~` `is_within_project`, ET pour un suffixe non vide la sauvegarde `cible+suffixe` aussi), au moins un segment carvé, ET le reste n'est plus prouvé-dangereux.

## Références

- Solution : `docs/solutions/security-issues/a-sed-i-backup-suffix-is-confined-like-bare-i.md`.
- Preuve : pilote #2601 (`f2edcf8f`), 2026-09-30T14:28:33.718Z. Complément direct : cpp#229 (#229), cpp#245 (#245). Famille : cpp#205. Leçon ReDoS : cpp#250.
